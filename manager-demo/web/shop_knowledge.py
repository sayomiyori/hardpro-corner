"""Verified storefront policies and equipment explanations."""

import json
import re
from functools import lru_cache
from pathlib import Path

from manager_knowledge import definition_answer, normalized, whole_terms

CONTACT_LINK = {"label": "Связаться с магазином", "href": "/shop?legal=seller"}
OFFER_LINK = {"label": "Доставка и оплата", "href": "/shop?legal=offer#delivery"}
RETURNS_LINK = {"label": "Обмен и возврат", "href": "/shop?legal=returns"}
CATALOG_LINK = {"label": "Каталог", "href": "/shop"}
CONSULT = re.compile(
    r"\b(?:что|чем|зачем|означа\w*|сколько грамм\w*|стоит ли|хватает ли|"
    r"одинаково|требу\w*|заменя\w*|допуст\w*|гарантир\w*|отлича\w*|защища\w*)\b"
    r"|можно ли.{0,30}(?:считать|использовать|спарр)"
)
ORDER_STATUS = re.compile(r"\b(?:статус\w*|провер\w*|отслед\w*|где|номер)\b.{0,36}\b(?:заказ\w*|посылк\w*|отправлени\w*)"
                          r"|\b(?:заказ\w*|посылк\w*)\b.{0,30}\b(?:статус\w*|отслед\w*)")


@lru_cache(maxsize=1)
def data():
    return json.loads(Path(__file__).with_name("shop-knowledge.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def search_aliases():
    aliases = data()["search_aliases"]
    return whole_terms(aliases), aliases


def expand_shop_terms(question):
    pattern, aliases = search_aliases()
    return pattern.sub(lambda match: aliases[match[0]], normalized(question))


@lru_cache(maxsize=1)
def equipment_rules():
    return [(re.compile(card["match"]), tuple(re.compile(p) for p in card.get("all", ())), card["answer"])
            for card in data()["equipment"]]


def equipment_answer(question):
    phrase = normalized(question)
    if not CONSULT.search(phrase):
        return None
    definition = definition_answer(question)
    if definition and re.search(r"\bграмм\w*\b", phrase):
        return re.sub(r"(?m)^\[[^\n]*\][(][^\n]*[)]$", "", definition).strip()
    for pattern, conditions, answer in equipment_rules():
        if pattern.search(phrase) and all(condition.search(phrase) for condition in conditions):
            return answer
    if definition:
        return re.sub(r"(?m)^\[[^\n]*\][(][^\n]*[)]$", "", definition).strip()
    return None


def scope_answer(phrase):
    if re.search(r"\b(?:cvv|cvc|номер\w* (?:банковской )?карт\w*)\b", phrase):
        return "Не передавайте в чат номер карты, CVV или другие платёжные данные. Безопасный способ оплаты подтвердит магазин.", [CONTACT_LINK]
    if re.search(r"\bчуж\w*\s+заказ\w*|\bзаказ\w* других покупател\w*", phrase):
        return "Чужие заказы и личные данные покупателей недоступны в этом чате.", []
    if re.search(r"\b(?:личн\w+ номер\w* телефон\w*|домашн\w+ адрес\w*|мой адрес|адрес доставки|мой (?:личный )?телефон)\b", phrase):
        return "Не публикуйте в чате телефон и адрес доставки. Если они нужны для заказа, передайте их сотруднику магазина через официальный канал.", [CONTACT_LINK]
    if ORDER_STATUS.search(phrase):
        return "Я не могу проверить конкретный заказ в этом чате. Свяжитесь с магазином через официальный контакт и уточните статус у менеджера.", [CONTACT_LINK]
    if re.search(r"\b(?:закупочн\w*|марж\w*|внутренн\w* сумм\w*|внутренн\w* продаж\w*|выручк\w*)\b", phrase):
        return "Внутренние продажи, закупочные цены и маржа недоступны покупательскому чату. Могу проверить опубликованную розничную цену.", []
    if re.search(r"\b(?:служебн\w* настройк\w*|внутренн\w* инструкци\w*)\b", phrase):
        return "Служебные настройки и внутренние инструкции недоступны покупателям. Могу помочь с товарами и публичными условиями магазина.", []
    if re.search(r"\b(?:промокод\w*|выда\w* скидк\w*)\b", phrase):
        return "Я не выдаю личные промокоды и не назначаю скидки. Действующие предложения можно уточнить у магазина.", [CONTACT_LINK]
    if re.search(r"\b(?:брониру\w*|заброниру\w*|брон\w*)\b", phrase):
        return "В чате нельзя забронировать товар. Возможность отложить нужный вариант подтвердит менеджер магазина.", [CONTACT_LINK]
    change = r"(?:менять|поменя\w*|изменить|исправить|отредактировать)"
    if re.search(rf"\b{change}\b.{{0,32}}\bцен\w*|\bцен\w*\b.{{0,55}}\b{change}\b", phrase):
        return "Я не могу менять цены в карточках. Если заметили ошибку, передайте ссылку на товар менеджеру.", [CONTACT_LINK]
    return None


def policy_answer(phrase):
    shop = data()["store"]
    if re.search(r"\b(?:возврат\w*|вернуть|обменять|обмен\b)", phrase):
        return shop["returns"], [RETURNS_LINK]
    if re.search(r"\b(?:наличн\w*|наличными)\b", phrase):
        return shop["cash"], [OFFER_LINK, CONTACT_LINK]
    if re.search(r"\b(?:счет\w*|безналич\w*|юридическ\w* (?:лиц\w*|организаци\w*))\b", phrase):
        return shop["invoice"], [OFFER_LINK, CONTACT_LINK]
    if re.search(r"\b(?:оформ\w*.{0,28}заказ\w*|заказ\w*.{0,28}оформ\w*)\b", phrase):
        prefix = "Заказ в переписке я не оформляю. " if re.search(r"\b(?:сам|чат\w*|переписк\w*)\b", phrase) else ""
        return prefix + shop["order"], [CATALOG_LINK, CONTACT_LINK]
    if re.search(r"\b(?:достав\w*|курьер\w*|перевозчик\w*)\b", phrase):
        details = shop["delivery"]
        if re.search(r"\bсегодня\b", phrase):
            details += " " + shop["delivery_today"]
        return details, [OFFER_LINK, CONTACT_LINK]
    if re.search(r"\b(?:самовывоз\w*|забрать\w*)\b", phrase):
        return shop["pickup"], [CONTACT_LINK]
    if re.search(r"\bоплат\w*\b", phrase):
        return "Доступный способ оплаты подтверждает менеджер. На этом сайте онлайн-оплата недоступна.", [OFFER_LINK]
    return None
