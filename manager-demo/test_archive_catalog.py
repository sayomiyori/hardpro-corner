"""Проверяем уточнение варианта и точность складской строки на вымышленных данных."""
import unittest

from archive_catalog import Catalog, words


def record(code, variant, uid, stock, name="Перчатки Луна"):
    return {"код": code, "товар": name, "артикул": "L-1", "вариант": variant, "id": uid,
            "склады": {"Центр": {"свободно": stock} if stock is not None else {}}}


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog({"склады": ["Центр"], "товары": [
            record("01", "XL, Белый", "a", "2"), record("01", "2XL, Белый", "b", "0"),
            record("01", "M, Белый", "c", None), record("01", "M, Белый", "d", "4"),
            record("02", "Стандарт", "e", "1", "Шлем Луна")]})

    def query(self, product, variant="", warehouse=""):
        return self.catalog.search(dict(product=product, variant=variant, warehouse=warehouse))

    def test_distinct_sizes_missing_stock_and_duplicate_labels(self):
        self.assertIn("свободно 2.", self.query("01", "XL"))
        self.assertIn("свободно 2.", self.query("01", "XL White"))
        self.assertIn("свободно 0.", self.query("01", "XXL"))
        self.assertIn("Уточните", self.query("01", "M"))
        self.assertIn("[ID c]", self.catalog.menu())
        self.assertIn("неизвестно", self.catalog.reply("№1", None))
        self.assertNotEqual(words("42.5"), words("42"))

    def test_choose_product_before_reporting_stock(self):
        self.assertIn("Найдено 2", self.query("L-1"))
        self.assertIn("Уточните", self.catalog.reply("№1", None))
        self.assertNotIn("свободно", self.query("01", "80 oz"))
        self.assertIn("Такого склада", self.query("01", "M", "Другой"))
        self.assertIn("Совпадений", self.query("несуществующая модель"))

    def test_spoken_size_survives_weight_label_from_model(self):
        catalog = Catalog({"склады": ["Центр"], "товары": [
            record("03", "10 oz, Синий", "f", "6"), record("03", "12 oz, Синий", "g", "3")]})
        question = "Перчатки Луна, синие, 10 oz. Сколько свободно?"
        parsed = '{"product":"Перчатки Луна","variant":"цвет синий, вес 10 oz","warehouse":""}'
        self.assertIn("свободно 6.", catalog.reply(question, lambda _: parsed))


if __name__ == "__main__":
    unittest.main()
