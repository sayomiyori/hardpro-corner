"""Manager-only 1C connection. Credentials are private TLS files on the server."""
import copy
import json
import logging
import os
import re
import threading
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from uuid import UUID

from archive_catalog import COLORS, SIZES, Catalog, words
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


CATEGORY_FORMS = {
    "рашгарды": r"рашгард(?:ы|ов|а|у|е|ом)?",
    "капы": r"кап(?:а|ы|у|ой|ами|ах)?",
    "бинты": r"бинт(?:ы|ов|а|у|ами)?",
    "шлемы": r"шлем(?:ы|а|ов|у|ом|ами)?",
}
DENTAL_PROTECTION = re.compile(r"\bзащит[ау]\s+(?:для\s+)?зубов\b")
NEW_CATEGORY = re.compile(r"\b(?:какие|покажи\w*|подскажи\w*|найди\w*|нужн\w*|ищу)\b")
OTHER_CITY = re.compile(r"\bв друг\w* город\w*\b")
CATEGORY = re.compile(r"\b(?:" + "|".join(CATEGORY_FORMS.values()) + r")\b")
PRODUCT_CATEGORIES = {
    key: re.compile(r"^(?:" + forms + r")\b") for key, forms in CATEGORY_FORMS.items()
}
BRANDS = {
    "Venum": r"venum|венум",
    "BoyBo": r"boy[\s-]?bo|бой[\s-]?бо",
    "HARD-PRO": r"hard[\s-]?pro|хард[\s-]?про",
}
BRAND_FORMS = {
    brand: re.compile(r"(?<![\w-])(?:(не|кроме|без)\s+)?(?:" + pattern + r")(?![\w-])")
    for brand, pattern in BRANDS.items()
}
BOXING = re.compile(r"\b(?:бокс(?:а|у|ом|е)?|боксерск\w*)\b")
TRAINING = re.compile(r"\bтренировочн\w*\b")
BROWSE_WORDS = re.compile(
    r"\b(?:какие-то|какие|какой|какая|подскажи\w*|покажи\w*|пожалуйста|найди\w*|нужн\w*|ищу|есть|"
    r"наличи\w*|остат\w*|свободно|сейчас|сегодня|друг\w*|какие-то|именно|размер\w*|"
    r"цвет\w*|у|нас|еще|в|на|по|для|а|если|и|город\w*|магазин\w*|склад\w*|обоих|оба|всех)\b"
)
TAEKWONDO_USE = re.compile(r"\bподойд\w*\b.*\bтх[эе]кв[оа]ндо\b|\bтх[эе]кв[оа]ндо\b.*\bподойд\w*\b")
TAEKWONDO_FEDERATIONS = {
    "WT": (r"\b(?:wtf?|втф?)\b",
           "https://www.worldtaekwondo.org/att_file/documents/"
           "WT%20Competition%20Rules%20and%20Interpretation%20%28September%2030%2C%202024%29.pdf"),
    "ITF": (r"\b(?:itf|итф)\b", "https://itftkd.sport/rules/"),
    "ГТФ": (r"\b(?:gtf|гтф)\b", None),
    "МФТ": (r"\bмфт\b", None),
}
BROWSE_LIMIT = 24
STOCK_ITEMS = 6
HYPHENS = str.maketrans('‐‑–', '---')


@dataclass(frozen=True)
class Browse:
    category: str
    brand: str = ""
    excluded_brand: str = ""
    boxing: bool = False
    training: bool = False

    @property
    def label(self):
        qualifiers = [self.category]
        if self.boxing:
            qualifiers.append("для бокса")
        if self.training:
            qualifiers.append("тренировочные")
        if self.brand:
            qualifiers.append(self.brand)
        if self.excluded_brand:
            qualifiers.append(f"кроме {self.excluded_brand}")
        return " ".join(qualifiers)

    def includes(self, title):
        if self.brand and not BRAND_FORMS[self.brand].search(title):
            return False
        if self.excluded_brand and BRAND_FORMS[self.excluded_brand].search(title):
            return False
        return (not self.boxing or BOXING.search(title)) and (not self.training or TRAINING.search(title))


def category_in(text):
    found = set(CATEGORY.findall(text))
    categories = [key for key, forms in PRODUCT_CATEGORIES.items() if any(forms.fullmatch(word) for word in found)]
    return categories[0] if len(categories) == 1 else None


def browse_query(query, category, stores, previous, criteria):
    if category is None and (CATEGORY.search(query) or previous is None or not query.startswith("а ")):
        return None
    brands = [(brand, found) for brand, expr in BRAND_FORMS.items() for found in expr.finditer(query)]
    if len(brands) > 1:
        return None
    brand, excluded = "", ""
    if brands:
        name, match = brands[0]
        if match[1]:
            excluded = name
        else:
            brand = name

    city_matches = [
        (city, re.compile(r"\b" + re.escape(city.casefold().replace("ё", "е").rstrip("ь")) + r"\w*\b"))
        for city in stores
    ]
    cities = [city for city, pattern in city_matches if pattern.search(query)]
    attrs = [match[0] for pattern in (COLORS, SIZES) for match in pattern.finditer(query)]
    rest = query
    for pattern in (CATEGORY, BOXING, TRAINING, COLORS, SIZES, *BRAND_FORMS.values(),
                    *(pattern for _, pattern in city_matches), BROWSE_WORDS):
        rest = pattern.sub(" ", rest)
    if re.search(r"\w", rest) or (not category and not (brands or cities or attrs
                                                         or ALL_STORES.search(query) or OTHER_CITY.search(query))):
        return None
    continuation = previous and query.startswith('а ') and category in (None, previous.category)
    if continuation:
        plan = Browse(previous.category, brand if brands else previous.brand,
                      excluded if brands else previous.excluded_brand,
                      previous.boxing or (previous.category == 'шлемы' and bool(BOXING.search(query))),
                      previous.training or bool(TRAINING.search(query)))
        all_stores = ALL_STORES.search(query) or OTHER_CITY.search(query) or len(cities) > 1
        warehouse = '' if all_stores else (cities[0] if cities else criteria['warehouse'])
        variant = " ".join(attrs) if attrs else criteria["variant"]
    else:
        plan = Browse(category, brand, excluded, category == "шлемы" and bool(BOXING.search(query)),
                      bool(TRAINING.search(query)))
        warehouse = cities[0] if len(cities) == 1 else ""
        variant = " ".join(attrs)
    product = criteria["product"] if continuation and not category and not brands else plan.category
    return plan, dict(product=product or plan.category, variant=variant, warehouse=warehouse)


def taekwondo_answer(query, boxing):
    prefix = "В подборке были шлемы для бокса. " if boxing else ""
    federations = [name for name, (pattern, _) in TAEKWONDO_FEDERATIONS.items() if re.search(pattern, query)]
    federation = federations[0] if len(federations) == 1 else None
    if federation and re.search(r"\b(?:соревновани\w*|турнир\w*|старт\w*)\b", query):
        _, source = TAEKWONDO_FEDERATIONS[federation]
        instruction = (prefix + f"Для соревнований {federation} нужно сверить актуальное положение старта "
                       "и маркировку конкретного шлема.")
        if source:
            instruction += f" [Документы федерации {federation}]({source})"
        return instruction + "\nДопуск этих моделей пока не подтверждён. Какую модель и турнир проверить?"
    if federation and re.search(r"\bтренировк\w*\b", query):
        return (prefix + f"Для тренировки {federation} сверим назначение конкретной модели "
                "с требованиями тренера. Как называется шлем?")
    return (prefix + "По названиям моделей пригодность для тхэквондо подтвердить не могу. "
            "Подскажите, вам нужен шлем для WT или ITF, на тренировку или на соревнования?")

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

SNAPSHOT = Path.home() / ".local/share/hardpro/catalog-web/data/catalog-snapshot.json"
REFRESH_SECONDS = 900
MOSCOW = timezone(timedelta(hours=3))
CATALOG_FIELDS = {"код", "товар", "артикул", "вариант", "product", "id"}
SOURCE_ERRORS = (OSError, HTTPException, ValueError, TypeError, KeyError)
log = logging.getLogger(__name__)


class CatalogNotReady(RuntimeError):
    pass


class OnecReader:
    def __init__(self, config, snapshot_path=SNAPSHOT):
        self.lock = threading.Lock()
        self.index = None
        self.snapshot_path = snapshot_path
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
            if self.index is None:
                raise CatalogNotReady("Справочник 1С ещё загружается. Повторите поиск позже.")
            session = copy.copy(self.index)
        session.criteria = session.criteria.copy()
        session.options = []
        return session

    def build_index(self, data):
        if not isinstance(data, dict) or set(data) != {"момент", "товары"}:
            raise ValueError("Некорректный формат копии справочника")
        stamp = datetime.fromisoformat(data["момент"])
        rows = data["товары"]
        if stamp.tzinfo is None or not isinstance(rows, list) or not rows:
            raise ValueError("Копия справочника пуста или без даты")
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != CATALOG_FIELDS
                    or not all(isinstance(value, str) for value in row.values())
                    or not row["код"].strip() or not row["товар"].strip()):
                raise ValueError("Некорректная строка справочника")
            key = (str(UUID(row["product"])), str(UUID(row["id"])))
            if key in seen:
                raise ValueError("Повтор строки справочника")
            seen.add(key)
            row["product"], row["id"] = key
        return LiveCatalog({"склады": list(self.stores), "товары": rows}, self, stamp.astimezone(MOSCOW))

    def load_snapshot(self):
        try:
            index = self.build_index(json.loads(self.snapshot_path.read_bytes()))
        except FileNotFoundError:
            return
        except SOURCE_ERRORS as exc:
            log.warning("Cannot load manager catalog snapshot (%s)", type(exc).__name__)
            return
        with self.lock:
            self.index = index

    def save_snapshot(self, data):
        self.snapshot_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.snapshot_path.parent,
                                  prefix=".catalog-", delete=False)
        pending = Path(file.name)
        try:
            with file:
                json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            pending.replace(self.snapshot_path)
        finally:
            pending.unlink(missing_ok=True)

    def refresh_snapshot(self, stop):
        data = self.catalog(stop)
        if data is None or stop.is_set():
            return
        index = self.build_index(data)
        if stop.is_set():
            return
        self.save_snapshot(data)
        with self.lock:
            self.index = index
        log.info("Manager catalog snapshot updated: %d rows", len(index.rows))

    def refresh_loop(self, stop):
        while not stop.is_set():
            try:
                self.refresh_snapshot(stop)
            except SOURCE_ERRORS as exc:
                log.warning("Cannot refresh manager catalog snapshot (%s)", type(exc).__name__)
            stop.wait(REFRESH_SECONDS)

    def catalog(self, stop):
        after, rows, cursors = "", [], {""}
        for _ in range(2048):
            if stop.is_set():
                return None
            page = self.get("/v1/catalog", after=after)
            cursor = page["next"]
            items = page["items"]
            if not isinstance(items, list) or (cursor is not None and not items):
                raise ValueError("Некорректная страница справочника")
            rows.extend(items)
            if cursor is None:
                return {"момент": datetime.now(timezone.utc).isoformat(), "товары": rows}
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Повтор страницы справочника")
            cursors.add(cursor)
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
    def __init__(self, data, reader, snapshot_stamp=None):
        super().__init__(data)
        self.reader = reader
        self.selected = None
        self.snapshot_stamp = snapshot_stamp
        self.browse = None
        self.browse_products = {key: [] for key in PRODUCT_CATEGORIES}
        for code, row in self.products.items():
            title = row['товар'].strip().casefold().replace('ё', 'е').translate(HYPHENS)
            for category, pattern in PRODUCT_CATEGORIES.items():
                if pattern.match(title):
                    self.browse_products[category].append((code, title))
                    break

    def with_snapshot_date(self, text):
        if self.snapshot_stamp is None:
            return text
        return f"Справочник 1С на {self.snapshot_stamp:%d.%m.%Y %H:%M} МСК.\n{text}"

    def show(self, idx, city):
        # История нужна для выбора варианта. Запрашиваем остатки только для последней реплики.
        self.selected = ([idx], city)
        return ""

    def matching_rows(self, product, variant):
        browse = self.browse
        if browse is None or product != browse.category:
            return super().matching_rows(product, variant)
        codes = [code for code, title in self.browse_products[browse.category] if browse.includes(title)]
        attrs = words(variant, variant=True)
        matches = [idx for code in codes for idx in self.by_product[code] if attrs <= self.variant_words[idx]]
        return codes, matches

    def menu(self):
        if not self.options:
            return super().menu()
        if self.options[0][0] == "variant":
            end = min(self.offset + 8, len(self.options))
            row = self.rows[self.options[0][1]]
            lines = [f"У модели «{row['товар']}» есть такие варианты:"]
            for i in range(self.offset, end):
                lines.append(f"№{i + 1} {self.label(self.options[i][1])}")
            if end < len(self.options):
                lines.append("Напишите «ещё», чтобы посмотреть продолжение.")
            lines.append("Какой вариант проверить по наличию?")
            return "\n".join(lines)
        total = len(self.options)
        end = min(self.offset + 8, total)
        intro = f"Вот что нашёл в каталоге по запросу «{self.browse.label}»." if self.browse else "Вот что нашёл в каталоге."
        lines = [intro]
        if total > 8:
            lines.append(f"Показываю модели {self.offset + 1}–{end} из {total}.")
        for i in range(self.offset, end):
            _, code = self.options[i]
            lines.append(f"№{i + 1} {self.products[code]['товар']}")
        lines.append("Назовите модель, размер или город — проверю наличие.")
        if end < total:
            lines.append("Напишите «ещё», чтобы посмотреть следующие модели.")
        return "\n".join(lines)

    def browse_stock(self, matches, city):
        products = list(dict.fromkeys(self.rows[idx]["product"] for idx in matches))
        if not products:
            return f"По запросу «{self.browse.label}» подходящих вариантов в каталоге нет."
        stores = [city] if city else self.stores
        checked = products[:BROWSE_LIMIT]
        checked_set = set(checked)
        try:
            stamp, stock, _ = self.reader.offers(checked)
        except SOURCE_ERRORS:
            return f"По запросу «{self.browse.label}» есть совпадения в каталоге. 1С пока не подтвердила остатки."

        found = {}
        every_balance_known = True
        for idx in matches:
            row = self.rows[idx]
            if row["product"] not in checked_set:
                continue
            amounts = stock.get((row["product"], row["id"]), {})
            available = [name for name in stores if amounts.get(self.reader.stores[name], 0) > 0]
            every_balance_known &= all(self.reader.stores[name] in amounts for name in stores)
            if available:
                item = f"{self.label(idx)}: {', '.join(available)}"
                found.setdefault((row["product"], row["товар"]), []).append(item)

        coverage = ""
        if len(products) > len(checked):
            coverage = f"\nПроверил первые {len(checked)} из {len(products)} моделей."
        if not found:
            if coverage or not every_balance_known:
                return ("Среди проверенных вариантов свободный остаток не подтверждён."
                        f"{coverage}\nНазовите модель или размер — проверю точнее.")
            return f"Свободных остатков по запросу «{self.browse.label}» сейчас нет в 1С."
        lines = [f"Вот что сейчас есть в наличии по запросу «{self.browse.label}»:"]
        for (_, title), variants in list(found.items())[:STOCK_ITEMS]:
            suffix = f"; ещё вариантов: {len(variants) - 3}" if len(variants) > 3 else ""
            lines.append(f"• {title} — {'; '.join(variants[:3])}{suffix}.")
        if len(found) > STOCK_ITEMS:
            lines.append(f"Показал {STOCK_ITEMS} из {len(found)} моделей с подтверждённым остатком.")
        lines.append(f"По данным 1С на {stamp:%H:%M:%S} МСК.{coverage}")
        if not every_balance_known:
            lines.append("Для части проверенных вариантов остаток неизвестен.")
        lines.append("Какую модель или размер проверить подробнее?")
        return "\n".join(lines)

    def answer(self, turns, parse):
        previous_budget = None
        previous_availability = False
        pending_taekwondo = False
        final_stock = None
        result = 'Скажите, какой товар подобрать.'
        for question in turns:
            query = DENTAL_PROTECTION.sub('капы', expand_terms(question).translate(HYPHENS))
            final_stock = None
            if self.browse and self.browse.category == 'шлемы' and (
                    TAEKWONDO_USE.search(query) or (pending_taekwondo and not category_in(query)
                    and any(re.search(pattern, query) for pattern, _ in TAEKWONDO_FEDERATIONS.values()))):
                result = taekwondo_answer(query, self.browse.boxing)
                pending_taekwondo = True
                self.selected = None
                continue
            pending_taekwondo = False
            budget = budget_value(query)
            previous_product = self.criteria["product"]
            want_stock = bool(STOCK_REQUEST.search(query))
            want_price = bool(PRICE_REQUEST.search(query))
            want_details = bool(DETAIL_REQUEST.search(query))
            site_price = want_price and SITE_PRICE.search(query)
            previous = self.selected
            self.selected = None
            options = [key for kind, key in self.options if kind == "variant"]
            category = category_in(query)
            browse = None if budget is not None or want_price or want_details else browse_query(
                query, category, self.stores, self.browse, self.criteria
            )
            if browse:
                plan, criteria = browse
                same_search = (self.browse and self.browse.category == plan.category
                               and (query.startswith('а ') or re.search(r'\bименно\b', query)))
                want_stock = want_stock or (same_search and previous_availability)
                self.browse = plan
                previous_availability = want_stock
                if want_stock:
                    self.criteria, self.options = criteria, []
                    _, matches = self.matching_rows(criteria['product'], criteria['variant'])
                    final_stock = (matches, criteria['warehouse'])
                else:
                    result = self.search(criteria)
                previous_budget = None
                continue
            previous_availability = False
            if CATEGORY.search(query) and (NEW_CATEGORY.search(query) or not query.startswith('а ')):
                self.browse = None
                self.criteria = dict(product='', variant='', warehouse='')
                self.options = []
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
        if final_stock is not None:
            return self.browse_stock(*final_stock)
        if self.selected is None:
            return self.with_snapshot_date(result.replace("в выгрузке", "в справочнике 1С"))
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
            answer = self.stocks_text(indices, stores)
        if want_details:
            answer = "\n\n".join(filter(None, [self.details_text(indices), answer]))
        if site_price:
            answer += "\n\nЦены сайта по этим вариантам пока недоступны."
        return answer

    def stocks_text(self, indices, stores):
        try:
            return "\n\n".join(self.stock_text(idx, stores) for idx in indices)
        except SOURCE_ERRORS:
            titles = [f"{self.rows[idx]['товар']} — {self.label(idx)}." for idx in indices]
            return self.with_snapshot_date("\n".join([
                *titles, "Актуальное наличие в 1С подтвердить не удалось."
            ]))

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
            except SOURCE_ERRORS:
                return "Розничные цены 1С сейчас недоступны." + (
                    "\n\n" + self.stocks_text(indices, stores)
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
        except SOURCE_ERRORS:
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
