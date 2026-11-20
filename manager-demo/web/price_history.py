"""Daily observations of public SKU prices. No visitor data."""
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FILE = Path.home() / ".local/share/hardpro/catalog-web/observed-prices.sqlite3"


class PriceHistory:
    def __init__(self, path=FILE):
        self.path = path

    def observe(self, catalog, observed_at):
        local = datetime.fromisoformat(observed_at).astimezone(ZoneInfo("Europe/Moscow"))
        day = local.date()
        cutoff = (day - timedelta(days=89)).isoformat()
        rows = [(int(r["id"]), int(r["код"][1:]), day.isoformat(), r["price_cents"])
                for r in catalog.rows if r["id"] and r["price_cents"] > 0]
        with closing(sqlite3.connect(self.path, timeout=4)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS observed_price (sku INTEGER NOT NULL, product INTEGER NOT NULL, "
                       "day TEXT NOT NULL, cents INTEGER NOT NULL, PRIMARY KEY (sku,product,day)) WITHOUT ROWID")
            db.execute("DELETE FROM observed_price WHERE day < ?", (cutoff,))
            db.executemany("INSERT INTO observed_price VALUES (?,?,?,?) ON CONFLICT DO NOTHING", rows)

    def recent(self, sku, product):
        if not self.path.is_file():
            return []
        cutoff = (datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=89)).isoformat()
        with closing(sqlite3.connect(self.path.as_uri()+"?mode=ro", uri=True, timeout=2)) as db:
            rows = db.execute("SELECT day,cents FROM observed_price WHERE sku=? AND product=? AND day>=? ORDER BY day",
                              (int(sku),int(product),cutoff)).fetchall()
        if not rows:
            return []
        changes = [rows[0]] + [row for prev,row in zip(rows,rows[1:]) if row[1] != prev[1]]
        # Retain the first price as the comparison baseline and show up to three subsequent changes.
        visible = changes if len(changes)<=4 else [changes[0],*changes[-3:]]
        if len(rows)>1 and visible[-1][0]!=rows[-1][0] and len(visible)<4:
            visible.append(rows[-1])
        return [(date.fromisoformat(day).strftime("%d.%m.%Y"),cents) for day,cents in visible]
