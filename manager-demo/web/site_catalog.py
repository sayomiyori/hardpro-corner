"""Read the public product view at the start of each buyer turn."""
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo
from archive_catalog import Catalog

STORES = ["Севастополь", "Симферополь"]
PHOTO_PATH = re.compile(r"/assets/images/(?:[a-z0-9_-][a-z0-9_.-]*/)*[a-z0-9_-][a-z0-9_.-]*\.(?:avif|webp|jpe?g|png)", re.I)


def public_photo(value):
    if not isinstance(value, str) or len(value) > 512:
        return ""
    path = value if value.startswith("/") else "/" + value
    return path if PHOTO_PATH.fullmatch(path) else ""


class StoreCatalog(Catalog):
    def reply(self, question, parse):
        query = question.strip().casefold().rstrip(" .?!")
        if re.fullmatch(r"сколько (?:всего )?(?:товаров|карточек|моделей|вариантов)(?: всего)? (?:на сайте|в каталоге|в магазине)", query):
            variants = sum(row["id"] != "" for row in self.rows)
            return f"Карточек в каталоге сайта — {len(self.products)}. Активных вариантов — {variants}."
        return super().reply(question, parse)

    def label(self, idx):
        return self.rows[idx]["вариант"]

    def menu(self):
        end = min(self.offset + 8, len(self.options))
        lines = ["Какой вариант вас интересует? Уточните размер, цвет или напишите номер строки."]
        for i in range(self.offset, end):
            kind, key = self.options[i]
            row = self.products[key] if kind == "product" else self.rows[key]
            suffix = row["артикул"] if kind == "product" else row["вариант"]
            lines.append(f"№{i+1}  {row['товар']}" + (f" — {suffix}" if suffix else ""))
        if end < len(self.options):
            lines.append("Напишите «ещё» для продолжения.")
        return "\n".join(lines)

    def show(self, idx, city):
        row = self.rows[idx]
        lines = [f"{row['товар']} — {row['вариант']}."]
        if not row["id"]:
            return f"{row['товар']}. На сайте нет доступных для покупки вариантов."
        for store in ([city] if city else self.stores):
            lines.append(f"{store} — доступно {row['склады'][store]} шт.")
        return "\n".join(lines)


def read_store(site, nonce):
    path = Path.home() / ".local/share/hardpro/catalog-web/inflight" / str(UUID(nonce))
    data = json.loads(path.read_text())
    if data["site"] != site:
        raise ValueError("Incorrect catalogue source")
    return store_from_data(data["catalog"])


def store_from_data(data):
    rows = []
    contacts = []
    for r in data["rows"]:
        if type(r["product"]) is not int or r["product"]<=0 or (r["sku"] is not None and
                (type(r["sku"]) is not int or r["sku"]<=0)):
            raise ValueError("Invalid storefront identifier")
        price, old_price = r.get("price_cents",0), r.get("old_price_cents",0)
        if any(type(value) is not int or not 0<=value<=10**12 for value in (price,old_price)):
            raise ValueError("Invalid storefront price")
        stock = [r["sevastopol"], r["simferopol"]]
        if (r["sku"] is not None and any(type(n) is not int or n < 0 for n in stock)) or (r["sku"] is None and stock != [None, None]):
            raise ValueError("Invalid shop quantity")
        rows.append({"товар": r["title"], "артикул": r["article"], "вариант": r["variant"],
                     "код": "P"+str(r["product"]), "id": str(r["sku"]) if r["sku"] is not None else "", "склады": dict(zip(STORES, stock)),
                     "price_cents":price,"old_price_cents":old_price,
                     "photo":public_photo(r.get("photo")), "product_photo":public_photo(r.get("product_photo"))})
    for contact in data.get("contacts",[]):
        if ((contact["key"],contact["city"]) not in zip(("sevastopol","simferopol"),STORES)
                or not re.fullmatch(r"\+7[0-9]{10}",contact["phone"])):
            raise ValueError("Invalid store contact")
        contacts.append(contact)
    stamp = datetime.fromisoformat(data["as_of"]).astimezone(ZoneInfo("Europe/Moscow"))
    catalog = StoreCatalog({"склады": STORES, "товары": rows})
    catalog.contacts = contacts
    catalog.sections = [
        (section["id"], section["title"]) for section in data.get("sections", [])
        if isinstance(section, dict) and type(section.get("id")) is int and 0 < section["id"] < 10**10
        and isinstance(section.get("title"), str) and 0 < len(section["title"]) <= 160
    ]
    catalog.vocabulary = set().union(*catalog.product_words.values())
    catalog.articles = {}
    for code,row in catalog.products.items():
        catalog.articles.setdefault(row["артикул"].casefold(),[]).append(code)
    return catalog, stamp.strftime("%d.%m.%Y %H:%M МСК")
