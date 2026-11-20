"""Storefront answers from a public feed and fixed handlers."""
import re
import unicodedata

from archive_catalog import COLORS, SIZES, words
from site_catalog import STORES

HANDOFF = "/shop?legal=seller"
HELP = "Помогу с ассортиментом, текущими ценами, комплектами и адресами HARD-PRO. Назовите товар или вид спорта."
PRIVATE = "Уберите личные данные и отправьте вопрос ещё раз. По конкретному заказу свяжитесь с магазином."
HEALTH = "Обратитесь к врачу за оценкой травмы или симптомов. Подбирать экипировку для лечения или возвращения к тренировкам по переписке небезопасно."
HEALTH_PATTERN = re.compile(
    r"\b(?:травм\w*|перелом\w*|вывих\w*|растяжени\w*|сотрясени\w*|диагноз\w*|операци\w*|реабилитаци\w*|"
    r"болит|болят|боль\b|болях|болями|обострен\w*|опух\w*|онемел\w*|ушиб\w*|грыж\w*|артрит\w*|сколиоз\w*|"
    r"беременн\w*|поврежден\w*|повредил\w*|сломал\w*|инсульт\w*|астм\w*|эпилепси\w*|симптом\w*|порва\w*|лечени\w*|лечит\w*|врач\w*|связк\w*)"
)
PERSONAL = re.compile(
    r"@|(?:\+?\d[\d .()\-]{8,}\d)|\b(?:снилс|паспорт\w*|cvv|cvc|мой телефон|мой адрес|меня зовут|"
    r"мой заказ|номер (?:заказа|карты)|адрес доставки|позвоните мне|перезвоните|моя почта)\b"
)
INSTRUCTIONS = re.compile(
    r"\b(?:игнориру\w*|забудь|промпт\w*|prompt|инструкци\w*|jailbreak|админ\w*|парол\w*|токен\w*|"
    r"системн\w*\s+(?:сообщени\w*|правил\w*)|роль\s+(?:бота|системы)|выполни\s+команд\w*)\b"
)
CONTACT = re.compile(r"\b(?:контакт\w*|адрес\w*|телефон\w*|позвон\w*|номер магазина|где (?:ваш\w* |у вас )?(?:находит\w*|магазин\w*|вы\b)|"
                     r"как (?:найти|добраться|доехать)|(?:час\w*|режим|график) работы|самовывоз\w*|куда (?:подъехать|приехать))")
HISTORY = re.compile(r"истори\w*\s+цен|динамик\w*\s+цен|цен\w*\s+(?:раньше|была|менялась)|(?:была|менялась|измен\w*)\s+цен|(?:подешевел|подорожал)\w*")
KIT = re.compile(r"\b(?:комплект\w*|набор\w*|экипировк\w*|одежд\w*)\b")
ITEMS = {
    "Перчатки": r"\bперчат\w*",
    "Бинты": r"\bбинт\w*",
    "Капа": r"\bкап(?:а|ы|у|е|ой|ами|ой|)\b",
    "Шлем": r"\bшлем\w*",
    "Защита голени": r"защит\w* голен|щитк\w*|накладк\w* на голень",
    "Шорты": r"\bшорт\w*",
    "Рашгард": r"\bрашгард\w*",
    "Кимоно": r"\b(?:кимоно|ги)\b",
    "Пояс": r"\bпояс\w*",
    "Футболка": r"\bфутболк\w*",
    "Легинсы": r"\b(?:лосин|леггинс|легинс)\w*",
}
SPORTS = (
    (r"мма|\bmma\b", "ММА", ("Перчатки", "Рашгард", "Шорты", "Капа")),
    (r"муай|тайск\w*\s+бокс|кикбокс", "ударных тренировок", ("Перчатки", "Бинты", "Защита голени", "Капа")),
    (r"бокс", "бокса", ("Перчатки", "Бинты", "Капа")),
    (r"джиу|грэпплинг|грепплинг|\bbjj\b", "грэпплинга", ("Рашгард", "Шорты")),
    (r"фитнес|зал\b", "фитнеса", ("Футболка", "Шорты")),
)
FILLER = words("есть какие какой можно купить в наличии сколько всего осталось покажи подскажи пожалуйста "
               "хочу ищу нужны нужна нужен нужныe найдите найди подберите подобрать предложи варианты магазин "
               "склад товар цену цена цены цен стоимость стоит размер вес цвет черный белый синий красный "
               "доступно севастополь симферополь севастополе симферополе руб рублей раньше история динамика "
               "менялась изменилась была изменение была подешевел подорожал еще данный а это него нее")


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


def blocked(text):
    phrase = normalize(text)
    if HEALTH_PATTERN.search(phrase):
        return HEALTH, []
    if PERSONAL.search(phrase):
        return PRIVATE, [{"label":"Связаться с магазином", "href":HANDOFF}]
    if INSTRUCTIONS.search(phrase) or any(unicodedata.category(c)=="Cf" for c in text):
        return HELP, []
    return None


def rub(cents):
    rubles, kopecks = divmod(cents, 100)
    value = f"{rubles:,}".replace(",", " ")
    return f"{value},{kopecks:02d} ₽" if kopecks else f"{value} ₽"


def link(row, exact=False):
    label = row["товар"] + (f" · {row['вариант']}" if exact else "")
    href = "/shop/product?id=" + row["код"][1:]
    if exact and row["id"]:
        href += "&sku=" + row["id"]
    return {"label":label,"href":href}


def city_in(phrase):
    selected = [city for city in STORES if city.casefold().rstrip("ь") in phrase]
    return selected[0] if len(selected)==1 else None


class ShopDialog:
    def __init__(self, catalog, history=None):
        self.cat = catalog
        self.history = history
        self.selected = []
        self.options = []
        self.page = 0
        self.city = None
        self.attrs = set()
        self.compare = False
        self.pending_kit = ""

    def stores(self, city):
        rows = [c for c in self.cat.contacts if not city or city==c["city"]]
        if not rows:
            return "Адреса и телефоны опубликованы на странице контактов.", [{"label":"Контакты магазинов","href":HANDOFF}]
        links = []
        for row in rows:
            links.extend((
                {"label":f'{row["city"]} · адрес', "href":HANDOFF + "#" + row["key"]},
                {"label":f'Позвонить {row["phone"]}', "href":"tel:" + row["phone"]},
            ))
        return "\n".join(f'{c["address"]}. {c["hours"]}. Телефон {c["phone"]}.' for c in rows), links

    def equipment(self, phrase):
        explicit = tuple(name for name, pattern in ITEMS.items() if re.search(pattern, phrase))
        sport = next((sport for sport in SPORTS if re.search(sport[0], phrase)), None)
        slots = explicit if len(explicit)>1 else (sport[2] if sport else ())
        if "одежд" in phrase and len(explicit)<2:
            slots = tuple(slot for slot in slots if slot in {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"})
        if not slots:
            self.pending_kit = phrase
            return "Для какого спорта собрать комплект? Можно указать вещи и общий бюджет.", []
        self.pending_kit = ""
        self.options = []
        apparel = {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"}
        size_labels = [m[0] for m in SIZES.finditer(phrase) if not re.search(r"oz|унц|кг",m[0])]
        size_labels += re.findall(r"\bразмер\w*\s+(\d+)(?!\d|\s*(?:oz|унц|кг))",phrase)
        sizes = words(" ".join(size_labels),variant=True)
        ranked, missing, used = [], [], set()
        for slot in slots:
            best = {}
            exact = bool(sizes and slot in apparel)
            for row in self.cat.rows:
                stock = row["склады"]
                title = normalize(row["товар"])
                if (row["код"] in used or not row["id"] or not row["price_cents"]
                        or not re.search(ITEMS[slot], title)
                        or (exact and not sizes<=words(row["вариант"],variant=True))
                        or not (stock[self.city] if self.city else any(stock.values()))):
                    continue
                if slot=="Перчатки" and sport:
                    if sport[1]=="ММА" and not re.search(r"\b(?:мма|mma|грэпплинг)\b", title):
                        continue
                    if sport[1]!="ММА" and not re.search(r"бокс|(?:\d+\s*oz)", title):
                        continue
                key = row["код"]
                if key not in best or row["price_cents"]<best[key]["price_cents"]:
                    best[key] = row
            if not best:
                missing.append(slot)
                continue
            choice = min(best.values(),key=lambda row:(row["price_cents"],row["товар"],row["id"]))
            used.add(choice["код"])
            ranked.append((slot,choice,exact))
        if not ranked:
            return "Сейчас не нашёл доступных позиций для этого комплекта. Уточните состав или магазин.", []
        place = f" в магазине «{self.city}»" if self.city else " по двум магазинам"
        lines = [f"Подборка{place}. Названия ведут к карточкам, где можно выбрать размер."]
        if not self.city:
            lines.append("Позиции могут быть в разных магазинах.")
        for slot,row,exact in ranked:
            variant = f' ({row["вариант"]})' if exact else ""
            price = rub(row["price_cents"])
            lines.append(f'{slot} · {row["товар"]}{variant} · {price if exact else "от "+price}')
        total = sum(row["price_cents"] for _,row,_ in ranked)
        lines.append(f"Сумма найденных позиций от {rub(total)}. Цена зависит от варианта.")
        budget = re.search(r"(?:до|бюджет(?:ом)?)\s*(\d(?:[\d ]{0,8}\d)?)\s*(тыс\.?|к\b)?",phrase)
        if budget:
            amount = int(budget[1].replace(" ","")) * (1000 if budget[2] else 1)
            if total>amount*100:
                lines.append("Эта подборка выше указанного бюджета.")
        if missing:
            lines.append("Не нашёл: " + ", ".join(missing) + ".")
        return "\n".join(lines), [link(row,exact) for _,row,exact in ranked]

    def menu(self):
        part = self.options[self.page:self.page+8]
        title = "Уточните модель или вариант. Можно написать номер строки, размер или цвет."
        lines, links = [title], []
        for number, (exact, row) in enumerate(part,self.page+1):
            label = row["товар"] + (" · " + row["вариант"] if exact else "")
            links.append({**link(row,exact),"label":f"№{number} {label}"})
        if len(self.options)>8:
            lines.append(f"Показано {self.page+1}–{self.page+len(part)} из {len(self.options)}. Напишите «ещё» для продолжения.")
        return "\n".join(lines), links

    def show(self, row):
        self.options = []
        self.selected = [row["код"]]
        self.attrs = words(row["вариант"],variant=True) if row["id"] else set()
        if not row["id"]:
            return f'{row["товар"]}. У карточки пока нет активных вариантов для покупки.', [link(row)]
        lines = [f'{row["товар"]} · {row["вариант"]}']
        if row["price_cents"]:
            lines.append("Цена сейчас " + rub(row["price_cents"]) + ".")
            if row["old_price_cents"] > row["price_cents"]:
                lines.append("На карточке зачёркнута цена " + rub(row["old_price_cents"]) + ".")
        for city in [self.city] if self.city else STORES:
            lines.append(f'{city} — доступно {row["склады"][city]} шт.')
        if self.compare:
            points = self.history.recent(row["id"], row["код"][1:]) if self.history else []
            if points:
                lines.append("Наблюдения по этому варианту:")
                lines.extend(f"{date} · {rub(price)}" for date,price in points)
                delta = row["price_cents"] - points[0][1]
                if len(points)>1 or delta:
                    change = "выше на " + rub(delta) if delta>0 else "ниже на " + rub(-delta) if delta<0 else "совпадает с первой наблюдавшейся ценой"
                    lines.append("Текущая цена " + change + ".")
                else:
                    lines.append("Пока есть только первое наблюдение. Изменение цены подтвердить рано.")
            else:
                lines.append("Пока нет подтверждённой истории по этому варианту.")
        return "\n".join(lines), [link(row, True)]

    def choose(self, codes):
        indices = [i for code in codes for i in self.cat.by_product[code]]
        rows = [self.cat.rows[i] for i in indices if self.attrs<=self.cat.variant_words[i]]
        if not rows:
            self.options = []
            return "Такой размер или цвет не найден для этой модели. Уточните вариант.", []
        self.selected = list(dict.fromkeys(row["код"] for row in rows))
        if len(rows)==1:
            return self.show(rows[0])
        self.options = ([(False,self.cat.products[code]) for code in self.selected] if len(self.selected)>1
                        else [(True,row) for row in rows])
        self.page = 0
        return self.menu()

    def answer(self, turns):
        result = (HELP, [])
        for question in turns:
            if not blocked(question):
                result = self.turn(normalize(question))
        return result

    def turn(self, phrase):
        if CONTACT.search(phrase):
            return self.stores(city_in(phrase))
        if re.search(r"\b(?:возврат\w*|вернуть|обменять)\b", phrase):
            return "Условия обмена и возврата есть на сайте.", [{"label":"Обмен и возврат","href":"/shop?legal=returns"}]
        if re.search(r"\b(?:доставк\w*|оплат\w*)\b", phrase):
            return "Условия доставки и оплаты зависят от способа получения. Подробности на сайте.", [{"label":"Доставка и оплата","href":"/shop?legal=offer"}]
        if re.fullmatch(r"(?:привет|здравствуйте|добрый (?:день|вечер)|что ты умеешь)[.!?]*", phrase):
            return HELP, []
        if re.search(r"сколько (?:всего )?(?:товаров|карточек|моделей|вариантов)(?: всего)? (?:на сайте|в каталоге|в магазине)", phrase):
            return f"Карточек в каталоге — {len(self.cat.products)}. Активных вариантов — {sum(bool(r['id']) for r in self.cat.rows)}.", []
        if self.options and phrase.rstrip(".!?") in {"еще","дальше"}:
            self.page = self.page+8 if self.page+8<len(self.options) else 0
            return self.menu()
        chosen = re.fullmatch(r"(?:№|#|номер\s+|вариант\s+)(\s*[0-9]{1,5})[.!?]?",phrase)
        if chosen:
            n = int(chosen[1])
            if not 1<=n<=len(self.options):
                return "Укажите номер из последней подборки.", []
            exact, row = self.options[n-1]
            return self.show(row) if exact else self.choose([row["код"]])
        city = city_in(phrase)
        kit = KIT.search(phrase) and (re.search(r"соб[еир]|подбер|подоб|нуж|куп|комплект|набор", phrase)
                                     or any(re.search(rule[0],phrase) for rule in SPORTS))
        if kit or self.pending_kit and (city or any(re.search(rule[0],phrase) for rule in SPORTS)):
            self.city = city or self.city
            return self.equipment((self.pending_kit + " " + phrase).strip())
        anchors = [m[0] for m in COLORS.finditer(phrase)] + [m[0] for m in SIZES.finditer(phrase)]
        anchors += re.findall(r"\b(?:размер\w*|рост(?:ом|а)?)\s+(\d+(?:[.,]\d+)?)",phrase)
        term_phrase = COLORS.sub(" ", SIZES.sub(" ", phrase))
        categories = [re.compile(pattern) for pattern in ITEMS.values() if re.search(pattern,phrase)]
        direct = list(dict.fromkeys(code for token in re.findall(r"[\w-]{3,64}",phrase)
                                    for code in self.cat.articles.get(token,())
                                    if all(pattern.search(normalize(self.cat.products[code]["товар"])) for pattern in categories)))
        terms = words(term_phrase) - FILLER - words(" ".join(anchors),variant=True)
        common = terms & self.cat.vocabulary
        # A size-only follow-up must not become a new product search.
        if self.selected and not common and (anchors or city):
            direct = []
        codes = direct or ([code for code,tokens in self.cat.product_words.items() if common<=tokens] if common else [])
        compare = bool(HISTORY.search(phrase))
        all_cities = all(name.casefold().rstrip("ь") in phrase for name in STORES) or bool(re.search(r"\b(?:оба|обоих|всех|другом)\s+(?:город\w*|магазин\w*|склад\w*)", phrase))
        if codes:
            self.attrs = words(" ".join(anchors),variant=True)
            self.city = city
            self.compare = compare
        elif self.selected and (anchors or city or all_cities or compare) and not common:
            codes = self.selected
            if anchors:
                self.attrs = words(" ".join(anchors),variant=True)
            if city or all_cities:
                self.city = city
            self.compare = self.compare or compare
        else:
            return HELP, []
        return self.choose(codes)
