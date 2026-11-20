"""Manager-only 1C connection. Credentials are private TLS files on the server."""
import copy
import json
import re
import threading
import time
import ssl
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from archive_catalog import COLORS, SIZES, Catalog
from manager_knowledge import expand_terms


BUDGET = re.compile(
    r"\b(?:до|не дороже|не больше|в пределах|бюджет)\s+"
    r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d{1,2}))?"
    r"\s*(к\b|k\b|тыс\b\.?|тысяч\w*)?\s*(?:₽|руб\w*\.?)?"
    r"(?![\d.,]|[ \u00a0\u202f]\d|\s*(?:кг|oz|унци\w*|см)\b)", re.I
)
STOCK_REQUEST = re.compile(r"\b(?:количеств\w*|остат\w*|наличи\w*|свободно|сколько|доступные размер\w*)\b")
PRICE_REQUEST = re.compile(r"\b(?:цен[ауыеи]\w*|стоимост\w*|стоит|стоят|почем)\b")
SITE_PRICE = re.compile(r"\b(?:на сайте|в интернет[- ]магазине|на витрине|hard-pro\.ru)\b")
DETAIL_REQUEST = re.compile(r"\b(?:описани\w*|материал\w*|состав|производител\w*|характеристик\w*|"
                            r"(?:какие|доступные) размер\w*)\b")
GROUP_REFERENCE = re.compile(r"\b(?:этих|эти|их|все цвета|все варианты|кажд\w*)\b")
ALL_STORES = re.compile(r"\b(?:обоих|обоим|оба|всех|все|двух|двум)\s+(?:склад\w*|магазин\w*)\b")


def budget_value(query):
    found = BUDGET.search(query)
    if not found:
        return None
    rubles = re.sub(r"\s", "", found[1])
    amount = Decimal(f"{rubles}.{found[2] or '0'}")
    return amount * 1000 if found[3] else amount


def price_text(price):
    return f"{price:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def read_stamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or not -15 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 120:
        raise ValueError("Время ответа 1С не подтверждено")
    return stamp.astimezone(timezone(timedelta(hours=3)))


class OnecReader:
    def __init__(self, config):
        self.lock = threading.Lock()
        self.index = None
        self.expires = 0
        self.host = config["host"]
        self.port = int(config["port"])
        self.stores = {name: str(UUID(uid)) for name, uid in config["warehouses"].items()}
        if len(self.stores) != 2 or len(set(self.stores.values())) != 2:
            raise ValueError("Требуются два разных физических склада")
        self.tls = ssl.create_default_context(cafile=config["ca"])
        self.tls.load_cert_chain(config["cert"], config["key"])

    def get(self, route, **params):
        conn = HTTPSConnection(self.host, self.port, context=self.tls, timeout=20)
        try:
            conn.request("GET", route + "?" + urlencode(params), headers={"Accept": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200 or resp.headers.get_content_type() != "application/json":
                raise OSError("Источник 1С недоступен")
            body = resp.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError("Ответ 1С превышает лимит")
            return json.loads(body)
        finally:
            conn.close()

    def session(self):
        with self.lock:
            if self.index is None or time.monotonic() >= self.expires:
                self.index = self.catalog()
                self.expires = time.monotonic() + 300
            session = copy.copy(self.index)
        session.criteria = session.criteria.copy()
        session.options = []
        return session

    def catalog(self):
        after, rows, seen = "", [], set()
        for _ in range(2048):
            page = self.get("/v1/catalog", after=after)
            for row in page["items"]:
                key = (str(UUID(row["product"])), str(UUID(row["id"])))
                if key in seen or not row["код"] or not row["товар"]:
                    raise ValueError("Некорректная страница справочника")
                seen.add(key)
                rows.append({**row, "product": key[0], "id": key[1]})
            cursor = page["next"]
            if cursor is None:
                return LiveCatalog({"склады":list(self.stores), "товары":rows}, self)
            if not cursor or cursor == after:
                raise ValueError("Повтор страницы справочника")
            after = cursor
        raise ValueError("Превышен предел страниц справочника")

    def balances(self, row):
        product, variant = str(UUID(row["product"])), str(UUID(row["id"]))
        reply = self.get("/v1/stock", product=product, variant=variant)
        if (reply["product"], reply["variant"]) != (product, variant):
            raise ValueError("1С вернула другую характеристику")
        as_of = read_stamp(reply["as_of"])
        return as_of, self.free_by_store(reply["balances"])

    def free_by_store(self, entries):
        by_store = {}
        for entry in entries:
            uid = str(UUID(entry["warehouse"]))
            if uid not in self.stores.values() or uid in by_store:
                raise ValueError("Некорректная строка склада")
            try:
                on_hand, reserved = (Decimal(str(entry[k])) for k in ("on_hand","reserved"))
            except InvalidOperation as e:
                raise ValueError("Количество не распознано") from e
            if not on_hand.is_finite() or not reserved.is_finite():
                raise ValueError("Количество не распознано")
            by_store[uid] = on_hand - reserved
        return by_store

    def offers(self, products):
        products = {str(UUID(uid)) for uid in products}
        payload = self.get("/v1/offers", products=",".join(sorted(products)))
        stamp = read_stamp(payload["as_of"])
        stocks, prices = {}, {}
        for row in payload["balances"]:
            key = str(UUID(row["product"])), str(UUID(row["variant"]))
            if key[0] not in products:
                raise ValueError("1С вернула другой товар")
            stocks.setdefault(key, []).append(row)
        stocks = {key: self.free_by_store(rows) for key, rows in stocks.items()}
        for row in payload["prices"] or ():
            key = str(UUID(row["product"])), str(UUID(row["variant"]))
            if key[0] not in products or key in prices:
                raise ValueError("Неоднозначные цены из 1С")
            try:
                price = Decimal(str(row["price"]).replace(",", "."))
            except InvalidOperation:
                price = Decimal(0)
            if (str(row["currency"]).casefold().strip() in {"", "rub", "643", "руб", "руб."}
                    and price.is_finite() and 0 < price <= 100_000_000 and price == price.quantize(Decimal(".01"))):
                prices[key] = price
            else:
                prices[key] = None
        return stamp, stocks, prices

    def details(self, row):
        uid = str(UUID(row["product"]))
        payload = self.get("/v1/details", product=uid)
        if payload["product"] != uid:
            raise ValueError("1С вернула другой товар")
        return payload["details"]


class LiveCatalog(Catalog):
    def __init__(self, data, reader):
        super().__init__(data)
        self.reader = reader
        self.selected = None

    def show(self, idx, city):
        # История нужна для выбора варианта. Запрашиваем остатки только для последней реплики.
        self.selected = ([idx], city)
        return ""

    def answer(self, turns, parse):
        previous_budget = None
        for question in turns:
            query = expand_terms(question)
            budget = budget_value(query)
            previous_product = self.criteria["product"]
            want_stock = bool(STOCK_REQUEST.search(query))
            want_price = bool(PRICE_REQUEST.search(query))
            want_details = bool(DETAIL_REQUEST.search(query))
            site_price = want_price and SITE_PRICE.search(query)
            previous = self.selected
            self.selected = None
            options = [key for kind, key in self.options if kind == "variant"]
            is_reference = GROUP_REFERENCE.search(query) and (want_stock or want_price) and not (
                COLORS.search(query) or SIZES.search(query)
                or any(city.casefold().rstrip("ь") in query for city in self.stores)
            )
            if is_reference and (options or previous):
                self.selected = (options, self.criteria["warehouse"]) if options else previous
                if ALL_STORES.search(query):
                    self.selected = (self.selected[0], "")
                result = ""
            else:
                result = self.reply(BUDGET.sub(" ", query).strip() if budget is not None else query, parse)
                options = [key for kind, key in self.options if kind == "variant"]
                if options and result == self.menu() and (want_stock or want_price or want_details or GROUP_REFERENCE.search(query)):
                    self.selected = (options, self.criteria["warehouse"])
            if budget is None and self.criteria["product"] == previous_product:
                budget = previous_budget
            previous_budget = budget
            if budget is not None and (self.selected or (self.options and result == self.menu())):
                _, matches = self.matching_rows(self.criteria["product"], self.criteria["variant"])
                city = self.selected[1] if self.selected else self.criteria["warehouse"]
                self.selected = (matches, city) if matches else self.selected
        if self.selected is None:
            return result.replace("в выгрузке", "в справочнике 1С")
        indices, city = self.selected
        stores = [city] if city else self.stores
        if budget is not None and site_price:
            return "Для отбора по бюджету доступны розничные цены 1С. Цены сайта по этим вариантам не подключены."
        if budget is not None or (want_price and not site_price):
            answer = self.price_and_stock(indices, stores, budget)
        elif want_details and not want_stock:
            answer = ""
        elif len(indices) > 8:
            answer = "Уточните размер или цвет. Можно запросить остатки сразу для восьми вариантов."
        else:
            answer = "\n\n".join(self.stock_text(idx, stores) for idx in indices)
        if want_details:
            answer = "\n\n".join(filter(None, [self.details_text(indices), answer]))
        if site_price:
            answer += "\n\nЦены сайта по этим вариантам пока недоступны."
        return answer

    def stock_text(self, idx, stores):
        row = self.rows[idx]
        stamp, amounts = self.reader.balances(row)
        return "\n".join([f"По рабочей 1С на {stamp:%d.%m.%Y %H:%M:%S} МСК.",
                          self.variant_text(idx, stores, amounts)])

    def variant_text(self, idx, stores, amounts):
        lines = [f"{self.rows[idx]['товар']} — {self.label(idx)}."]
        for name in stores:
            uid = self.reader.stores[name]
            if uid not in amounts:
                lines.append(f"{name} — в регистре нет строки, количество неизвестно.")
            elif amounts[uid] < 0:
                lines.append(f"{name} — свободных нет, расчётный остаток 1С {amounts[uid]:f}.")
            else:
                lines.append(f"{name} — свободно {amounts[uid]:f}.")
        return "\n".join(lines)

    def price_and_stock(self, indices, stores, budget):
        by_product = {}
        for idx in indices:
            by_product.setdefault(self.rows[idx]["product"], []).append(idx)
        products = list(by_product)
        if len(products) > 72:
            return "Уточните категорию, марку или размер, чтобы проверить цены и остатки."
        if budget is None and len(indices) > 24:
            return "Уточните модель, размер или цвет. Можно запросить цены для 24 вариантов."
        selected = []
        lacks_price = False
        has_price = False
        for start in range(0, len(products), 24):
            batch = products[start:start + 24]
            try:
                stamp, stocks, prices = self.reader.offers(batch)
            except OSError:
                return "Розничные цены 1С сейчас недоступны." + (
                    "\n\n" + "\n\n".join(self.stock_text(idx, stores) for idx in indices)
                    if budget is None and len(indices) <= 8 else ""
                )
            for product in batch:
                for idx in by_product[product]:
                    key = product, self.rows[idx]["id"]
                    price, amounts = prices.get(key), stocks.get(key, {})
                    lacks_price |= price is None
                    has_price |= price is not None
                    if budget is None or (price is not None and price <= budget
                                           and all(amounts.get(self.reader.stores[name], 0) > 0 for name in stores)):
                        selected.append((price, idx, amounts, stamp))
        if not selected:
            if not has_price:
                return "Для найденных вариантов розничные цены 1С не подтверждены. Подбор по бюджету недоступен."
            answer = f"В 1С нет подтверждённых вариантов до {price_text(budget)} в наличии: {', '.join(stores)}."
            if lacks_price:
                answer += " Часть вариантов без подтверждённой розничной цены."
            return answer
        selected.sort(key=lambda item: (item[0] is None, item[0] or 0, self.rows[item[1]]["товар"], item[1]))
        lines = [f"По рабочей 1С. Вариантов: {len(selected)}." + (" Первые 20 по цене." if len(selected) > 20 else "")]
        for price, idx, amounts, stamp in selected[:20]:
            info = self.variant_text(idx, stores, amounts)
            amount = price_text(price) if price is not None else "не подтверждена"
            lines.append(f"{info}\nРозничная цена 1С — {amount}. Проверено в {stamp:%H:%M:%S} МСК.")
        if budget is not None and lacks_price:
            lines.append("Варианты без подтверждённой розничной цены не включены.")
        return "\n\n".join(lines)

    def details_text(self, indices):
        rows = [self.rows[i] for i in indices]
        row = rows[0]
        if any(item["product"] != row["product"] for item in rows):
            return "Уточните модель или артикул, чтобы показать описание из 1С."
        variants = [self.label(idx) for idx in self.by_product[row["код"]]]
        lines = [f"{row['товар']} (артикул {row['артикул'] or 'не указан'}).",
                 "Характеристики в 1С: " + ", ".join(variants[:24]) + "."]
        if len(variants) > 24:
            lines.append(f"Показано 24 из {len(variants)} характеристик.")
        try:
            details = self.reader.details(row)
        except OSError:
            lines.append("Описание товара из 1С сейчас недоступно.")
            return "\n".join(lines)
        if details:
            for key, label in (("full_name", "Полное наименование"), ("manufacturer", "Производитель"),
                               ("kind", "Вид номенклатуры"),
                               ("description", "Описание")):
                value = details[key].strip()
                if value:
                    value = re.sub(r"([\\\x60\[\]<>!])", r"\\\1", value)
                    lines.append(f"{label}: {value}")
            if not details["description"].strip():
                lines.append("В карточке 1С описание не заполнено.")
        else:
            lines.append("Карточка товара в 1С недоступна.")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def configured_reader(path):
    return OnecReader(json.loads(Path(path).read_text(encoding="utf-8")))
