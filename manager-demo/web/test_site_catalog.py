import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_catalog import StoreCatalog


class StoreViewTest(unittest.TestCase):
    def test_site_quantity_and_display_only(self):
        stores = ["Севастополь", "Симферополь"]
        item = {"товар":"Перчатки Демо","артикул":"A","код":"P1","id":"15","вариант":"M",
                "склады":dict(zip(stores,[0,6]))}
        display = {**item, "код":"P2", "id":"", "товар":"Витринная карточка",
                   "вариант":"Нет вариантов для покупки","склады":dict.fromkeys(stores)}
        catalog = StoreCatalog({"склады":stores,"товары":[item,display]})
        self.assertIn("Симферополь — доступно 6 шт.",catalog.show(0,""))
        self.assertIn("Севастополь — доступно 0 шт.",catalog.show(0,""))
        self.assertIn("нет доступных",catalog.show(1,""))
        answer = catalog.reply("Сколько всего товаров на сайте?", None)
        self.assertIn("Карточек в каталоге сайта — 2",answer)
        self.assertIn("Активных вариантов — 1",answer)
