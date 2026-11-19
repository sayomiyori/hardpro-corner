import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from onec_live import LiveCatalog, OnecReader
from onec_reader_windows import Retail

P="00000000-0000-4000-8000-000000000001"
V="00000000-0000-4000-8000-000000000002"
A="00000000-0000-4000-8000-000000000003"
B="00000000-0000-4000-8000-000000000004"


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.client=OnecReader.__new__(OnecReader)
        self.client.stores={"Склад А":A,"Склад Б":B}
        self.item={"product":P,"id":V,"код":"01","товар":"Демо","вариант":"M","артикул":"DEMO"}
        self.response={"product":P,"variant":V,"as_of":datetime.now(timezone.utc).isoformat(),
                       "balances":[{"warehouse":A,"on_hand":"2.50","reserved":"2.50"}]}
        self.client.get=Mock(return_value=self.response)

    def test_zero_and_missing_are_distinct(self):
        cat=LiveCatalog({"склады":list(self.client.stores),"товары":[self.item]},self.client)
        result=cat.answer(["Демо","А размер M?"],lambda _: '{"product":"01","variant":"M","warehouse":""}')
        self.assertIn("Склад А — свободно 0.00",result)
        self.assertIn("Склад Б — в регистре нет строки",result)
        self.client.get.assert_called_once_with("/v1/stock",product=P,variant=V)

    def test_refuses_stale_duplicate_or_mismatched_result(self):
        for change in ({"variant":P},{"as_of":"2020-01-01T00:00:00Z"},{"balances":self.response["balances"]*2}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.client.get.return_value={**self.response,**change}
                self.client.balances(self.item)

    def test_catalog_cursor_and_unknown_variant(self):
        reader=Retail.__new__(Retail)
        reader.array=lambda x:x
        reader.rows=Mock(side_effect=[[{"uid":P,"code":"01","title":"Демо","article":"A"}],
                                      [{"code":"01","title":"M","uid":V}]])
        page=reader.catalog("")
        self.assertIsNone(page["next"])
        self.assertEqual([(r["product"],r["id"]) for r in page["items"]],[(P,V)])
