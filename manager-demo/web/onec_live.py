"""Manager-only 1C connection. Credentials are private TLS files on the server."""
import copy
import json
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

from archive_catalog import Catalog


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
                rows.append(row)
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
        as_of = datetime.fromisoformat(reply["as_of"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if as_of.tzinfo is None or not -15 <= (now-as_of).total_seconds() <= 120:
            raise ValueError("Время ответа 1С не подтверждено")
        by_store = {}
        for entry in reply["balances"]:
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
        return as_of.astimezone(timezone(timedelta(hours=3))), by_store


class LiveCatalog(Catalog):
    def __init__(self, data, reader):
        super().__init__(data)
        self.reader = reader
        self.selected = None

    def show(self, idx, city):
        # История нужна для выбора варианта. Запрашиваем остатки только для последней реплики.
        self.selected = (idx, city)
        return ""

    def answer(self, turns, parse):
        for question in turns:
            self.selected = None
            result = self.reply(question, parse)
        if self.selected is None:
            return result.replace("в выгрузке", "в справочнике 1С")
        idx, city = self.selected
        row = self.rows[idx]
        stamp, amounts = self.reader.balances(row)
        lines = [f"По рабочей 1С на {stamp:%d.%m.%Y %H:%M:%S} МСК.",
                 f"{row['товар']} — {self.label(idx)}."]
        for name in [city] if city else self.stores:
            uid = self.reader.stores[name]
            lines.append(f"{name} — свободно {amounts[uid]:f}." if uid in amounts else
                         f"{name} — в регистре нет строки, количество неизвестно.")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def configured_reader(path):
    return OnecReader(json.loads(Path(path).read_text(encoding="utf-8")))
