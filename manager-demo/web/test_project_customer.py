import unittest
from project_customer import project


class BuyerViewTest(unittest.TestCase):
    def test_selection_and_balance(self):
        def item(variant):
            return {"товар": "Учебный товар", "артикул": "DEMO", "код": "internal-code", "id": variant,
                    "вариант": "M", "склады": {"A": {"свободно": "2", "резерв": "3"}, "B": {"свободно": "0"}}}
        catalog = {"момент": "архив", "склады": ["A", "B", "C"], "товары": [item(k) for k in ("v", "w", "z")]}
        site = {"products": {"1": "p", "2": "p"}, "variants": [
            {"product_id": 1, "uuid": "p#v", "active": 1},
            {"product_id": 1, "uuid": "p#w", "active": 0},
            {"product_id": 2, "uuid": "p#z", "active": 1}]}
        ref = {"rows": [{"code": "internal-code", "uuid": "p", "deleted": False, "is_group": False}]}
        result = project(catalog, site, ref, {1})
        self.assertEqual(len(result["товары"]), 1)
        row = result["товары"][0]
        self.assertEqual(row["склады"], {"A": "available", "B": "unavailable", "C": "unknown"})
        self.assertNotEqual(row["код"], "internal-code")
        self.assertNotEqual(row["id"], "v")
