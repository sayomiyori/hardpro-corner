"""Read the public product view at the start of each buyer turn."""
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo
from archive_catalog import Catalog

STORES = ["Севастополь", "Симферополь"]


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
    data = data["catalog"]
    rows = []
    for r in data["rows"]:
        stock = [r["sevastopol"], r["simferopol"]]
        if (r["sku"] is not None and any(type(n) is not int or n < 0 for n in stock)) or (r["sku"] is None and stock != [None, None]):
            raise ValueError("Invalid shop quantity")
        rows.append({"товар": r["title"], "артикул": r["article"], "вариант": r["variant"],
                     "код": "P"+str(r["product"]), "id": str(r["sku"]) if r["sku"] is not None else "", "склады": dict(zip(STORES, stock))})
    stamp = datetime.fromisoformat(data["as_of"]).astimezone(ZoneInfo("Europe/Moscow"))
    return StoreCatalog({"склады": STORES, "товары": rows}), stamp.strftime("%d.%m.%Y %H:%M МСК")
