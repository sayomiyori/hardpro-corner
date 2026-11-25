"""Manager-only 1C connection. Credentials are private TLS files on the server."""
import copy
import json
import logging
import re
import socket
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

from archive_catalog import COLORS, SIZES
from manager_catalog import ManagerCatalog, display, units
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
SITE_PRICE_ONLY = re.compile(r"\bтолько\s+цен\w*\b")
IN_STOCK = re.compile(r"\b(?:в наличии|из наличия)\b")
COUNT_STOCK = re.compile(r"\b(?:сколько|количеств\w*|остат\w*)\b")
ANY_CITY_STOCK = re.compile(r"\b(?:есть|имеются)\b.*\b(?:симферопол\w*|севастопол\w*)\b")
BOTH_IN_STOCK = re.compile(r"\b(?:в|на)\s+обоих\s+(?:магазин|склад)ах\b")
DETAIL_REQUEST = re.compile(r"\b(?:описани\w*|материал\w*|состав|производител\w*|характеристик\w*|"
                            r"(?:какие|доступные) размер\w*)\b")
GROUP_REFERENCE = re.compile(r"\b(?:этих|эти|их|все цвета|все варианты|кажд\w*)\b")
ALL_STORES = re.compile(r"\b(?:обоих|обоим|оба|всех|все|двух|двум)\s+(?:склад\w*|магазин\w*)\b")
MSK = timezone(timedelta(hours=3))
KNOWLEDGE = Path.home() / ".local/share/hardpro/onec-pilot/catalog-knowledge.json"
log = logging.getLogger("hardpro.catalog")


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
        self.metadata = {}
        self.metadata_stamp = ""
        self.host = config["host"]
        self.port = int(config["port"])
        self.ssh_relay = config.get("ssh_relay", False)
        if not isinstance(self.ssh_relay, bool):
            raise ValueError("ssh_relay должен быть логическим значением")
        self.stores = {name: str(UUID(uid)) for name, uid in config["warehouses"].items()}
        if len(self.stores) != 2 or len(set(self.stores.values())) != 2:
            raise ValueError("Требуются два разных физических склада")
        self.tls = ssl.create_default_context(cafile=config["ca"])
        self.tls.load_cert_chain(config["cert"], config["key"])

    def get(self, route, **params):
        conn = HTTPSConnection(self.host, self.port, context=self.tls, timeout=20)
        try:
            if self.ssh_relay:
                with socket.create_connection(("127.0.0.1", self.port), conn.timeout) as sock:
                    conn.sock = self.tls.wrap_socket(sock, server_hostname=self.host)
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

    def warm_from_knowledge(self):
        try:
            knowledge = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
            data = knowledge["catalog"]
            stamp = datetime.strptime(data["момент"], "%d.%m.%Y %H:%M МСК").replace(tzinfo=MSK)
            if knowledge["schema"] != 1 or set(data["склады"]) != self.stores.keys() or not data["товары"]:
                raise ValueError("Несовместимая база каталога")
            self.metadata = knowledge["metadata_by_code"]
            self.metadata_stamp = data["момент"]
            if timedelta(0) <= datetime.now(MSK) - stamp < timedelta(days=1):
                self.index = LiveCatalog(data, self, indexed_at=stamp)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.info("Снимок базы каталога не загружен: %s", type(exc).__name__)

    def refresh_index(self):
        try:
            refreshed = self.catalog()
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Фоновое обновление индекса 1С отложено: %s", type(exc).__name__)
            with self.lock:
                self.expires = time.monotonic() + 60
            return
        with self.lock:
            self.index = refreshed
            self.expires = time.monotonic() + 300

    def session(self):
        with self.lock:
            if self.index is None:
                self.warm_from_knowledge()
            if self.index is None:
                self.index = self.catalog()
                self.expires = time.monotonic() + 300
            elif time.monotonic() >= self.expires:
                self.expires = float("inf")
                threading.Thread(target=self.refresh_index, daemon=True).start()
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

    def check(self):
        page = self.get("/v1/catalog", after="")
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) or not page["items"]:
            raise ValueError("1С не подтвердила чтение справочника")

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

    def offers(self, products, *, require_prices=False):
        products = {str(UUID(uid)) for uid in products}
        payload = self.get("/v1/offers", products=",".join(sorted(products)))
        if require_prices and payload["prices"] is None:
            raise OSError("1С не подтвердила чтение регистра цен")
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


class LiveCatalog(ManagerCatalog):
    def __init__(self, data, reader, *, indexed_at=None):
        super().__init__(data)
        self.reader = reader
        self.selected = None
        self.indexed_at = indexed_at or datetime.now(MSK)

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
            only_available = bool((IN_STOCK.search(query) or ANY_CITY_STOCK.search(query))
                                  and not COUNT_STOCK.search(query))
            both_in_stock = bool(BOTH_IN_STOCK.search(query))
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
            if (budget is not None or only_available) and self.selected is None and self.options and result == self.menu():
                _, matches = self.matching_rows(self.criteria["product"], self.criteria["variant"])
                if matches:
                    self.selected = (matches, self.criteria["warehouse"])
        if site_price and (SITE_PRICE_ONLY.search(query) or not want_stock):
            return "Цены сайта по этим вариантам пока недоступны."
        if self.selected is None:
            if self.options and result == self.menu():
                answer = self.offer_menu(show_prices=not site_price)
                if site_price:
                    answer += "\n\nЦены сайта по этим вариантам пока недоступны."
                return answer
            return result
        indices, city = self.selected
        stores = [city] if city else self.stores
        if budget is not None and site_price:
            return "Для отбора по бюджету доступны розничные цены 1С. Цены сайта по этим вариантам не подключены."
        if budget is not None or only_available or (want_price and not site_price):
            answer = self.price_and_stock(indices, stores, budget, only_available=only_available,
                                          both_in_stock=both_in_stock, show_prices=not site_price)
        elif want_details and not want_stock:
            answer = ""
        elif want_stock or site_price:
            if len(indices) > 8:
                return "Уточните размер или цвет. Можно запросить остатки сразу для восьми вариантов."
            answer = "\n\n".join(self.stock_text(idx, stores) for idx in indices)
        elif len(indices) > 24:
            answer = "Уточните размер или цвет. Можно запросить остатки сразу для восьми вариантов."
        else:
            answer = self.price_and_stock(indices, stores, None)
        if want_details:
            answer = "\n\n".join(filter(None, [self.details_text(indices), answer]))
        if site_price and not only_available:
            answer += "\n\nЦены сайта по этим вариантам пока недоступны."
        return answer

    def offer_menu(self, *, show_prices=True):
        visible = list(self.visible())
        products = list(dict.fromkeys(self.rows[indices[0]]["product"] for _, indices in visible))
        stamp, stocks, prices = self.reader.offers(products)
        city = self.criteria["warehouse"]
        stores = [city] if city else self.stores
        lines = [f"По рабочей 1С на {stamp:%d.%m.%Y %H:%M:%S} МСК. Позиций: {len(self.options)}."]
        for number, indices in visible:
            row = self.rows[indices[0]]
            keys = [(self.rows[i]["product"], self.rows[i]["id"]) for i in indices]
            confirmed_prices = sorted({prices[key] for key in keys if prices.get(key) is not None})
            if not confirmed_prices:
                price = "розничная цена не подтверждена"
            elif len(confirmed_prices) > 1:
                price = f"{price_text(confirmed_prices[0])} – {price_text(confirmed_prices[-1])}"
            else:
                price = price_text(confirmed_prices[0])
            if confirmed_prices and any(prices.get(key) is None for key in keys):
                price += " (по вариантам с ценой)"
            amounts = []
            for name in stores:
                uid = self.reader.stores[name]
                values = [max(stocks[key][uid], Decimal(0)) for key in keys if uid in stocks.get(key, {})]
                total = sum(values)
                if len(values) == len(keys):
                    detail = f"{units(total)} шт."
                elif total > 0:
                    detail = f"от {units(total)} шт."
                else:
                    detail = "нет данных"
                amounts.append(f"{display(name)} — {detail}")
            price_label = f" · {price}" if show_prices else ""
            lines.append(f"№{number} {display(row['товар'])} · {self.variant_summary(indices)}{price_label}. "
                         f"{'; '.join(amounts).rstrip('.')}.")
        return "\n".join([*lines, self.menu_hint()])

    def stock_text(self, idx, stores):
        row = self.rows[idx]
        stamp, amounts = self.reader.balances(row)
        return "\n".join([f"По рабочей 1С на {stamp:%d.%m.%Y %H:%M:%S} МСК.",
                          self.variant_text(idx, stores, amounts)])

    def variant_text(self, idx, stores, amounts):
        lines = [f"{display(self.rows[idx]['товар'])} — {self.label(idx)}."]
        for name in stores:
            uid = self.reader.stores[name]
            if uid not in amounts:
                lines.append(f"{name} — в регистре нет строки, количество неизвестно.")
            elif amounts[uid] < 0:
                lines.append(f"{name} — свободных нет, расчётный остаток 1С {amounts[uid]:f}.")
            else:
                lines.append(f"{name} — свободно {amounts[uid]:f}.")
        return "\n".join(lines)

    def price_and_stock(self, indices, stores, budget, *, only_available=False, both_in_stock=False, show_prices=True):
        by_product = {}
        for idx in indices:
            by_product.setdefault(self.rows[idx]["product"], []).append(idx)
        products = list(by_product)
        if len(products) > 72:
            return "Уточните категорию, марку или размер, чтобы проверить цены и остатки."
        if budget is None and not only_available and len(indices) > 24 and len(products) > 1:
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
                    if budget is not None and (price is None or price > budget):
                        continue
                    if budget is not None or only_available:
                        positives = [amounts.get(self.reader.stores[name], 0) > 0 for name in stores]
                        if not (all(positives) if both_in_stock else any(positives)):
                            continue
                    selected.append((price, idx, amounts, stamp))
        if not selected:
            if budget is None:
                return f"По рабочей 1С нет подтверждённых свободных остатков: {', '.join(stores)}."
            if not has_price:
                return "Для найденных вариантов розничные цены 1С не подтверждены. Подбор по бюджету недоступен."
            answer = f"В 1С нет подтверждённых вариантов до {price_text(budget)} в наличии: {', '.join(stores)}."
            if lacks_price:
                answer += " Часть вариантов без подтверждённой розничной цены."
            return answer
        selected.sort(key=lambda item: (
            not any(item[2].get(self.reader.stores[name], 0) > 0 for name in stores),
            item[0] is None, item[0] or 0, self.rows[item[1]]["товар"], item[1],
        ))
        lines = [f"По рабочей 1С. Вариантов: {len(selected)}." +
                 (" Показаны первые 20, начиная с подтверждённых остатков." if len(selected) > 20 else "")]
        for price, idx, amounts, stamp in selected[:20]:
            info = self.variant_text(idx, stores, amounts)
            amount = price_text(price) if price is not None else "не подтверждена"
            price_line = f"Розничная цена 1С — {amount}. " if show_prices else ""
            lines.append(f"{info}\n{price_line}Проверено в {stamp:%H:%M:%S} МСК.")
        if budget is not None and lacks_price:
            lines.append("Варианты без подтверждённой розничной цены не включены.")
        if not show_prices:
            lines.append("Цены сайта по этим вариантам пока недоступны.")
        return "\n\n".join(lines)

    def details_text(self, indices):
        rows = [self.rows[i] for i in indices]
        row = rows[0]
        if any(item["product"] != row["product"] for item in rows):
            return "Уточните модель, чтобы показать описание из 1С."
        variants = [self.label(idx) for idx in self.by_product[row["код"]]]
        lines = [f"{display(row['товар'])}.",
                 "Характеристики в 1С: " + ", ".join(variants[:24]) + "."]
        if len(variants) > 24:
            lines.append(f"Показано 24 из {len(variants)} характеристик.")
        try:
            details = self.reader.details(row)
        except OSError:
            details = getattr(self.reader, "metadata", {}).get(row["код"])
            if not details or details["title"] != row["товар"]:
                lines.append("Описание товара из 1С сейчас недоступно.")
                return "\n".join(lines)
            lines.append(f"Карточка по снимку 1С на {self.reader.metadata_stamp}.")
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
