import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onec_live import BROWSE_LIMIT, LiveCatalog


def catalog_row(code, name, variant, identifier=None):
    return {"код": code, "товар": name, "артикул": code, "вариант": variant,
            "product": "p-" + code, "id": identifier or "v-" + code}


class ManagerDialogTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            catalog_row("r1", "Рашгард Venum Альфа", "M"),
            catalog_row("r2", "Рашгард Venum Бета", "L"),
            catalog_row("r3", "Комплект Venum рашгард и шорты", "M"),
            catalog_row("c1", "Капа BoyBo Jab", "Детская"),
            catalog_row("c2", "Капа BoyBo Defender", "Взрослая"),
            catalog_row("c3", "Капа Другого бренда", "Взрослая"),
            catalog_row("c4", "Капа Непроверенная", "Взрослая"),
            catalog_row("w1", "Бинты боксерские Venum Kontact", "4 м, Красный", "w-red"),
            catalog_row("w1", "Бинты боксерские Venum Kontact", "4 м, Синий", "w-blue"),
            catalog_row("w1", "Бинты боксерские Venum Kontact", "4.5 м, Красный", "w-red-long"),
            catalog_row("h1", "Шлем боксерский тренировочный HARD-PRO Один", "M"),
            catalog_row("h2", "Шлем тренировочный HARD-PRO Два", "L"),
            catalog_row("h3", "Шлем боксерский Venum Три", "M"),
            catalog_row("h4", "Шлем тренировочный Venum Четыре", "L"),
        ]
        self.stores = {"Симферополь": "a", "Севастополь": "b"}
        available = {
            ("p-c1", "v-c1"): {"a": Decimal(2), "b": Decimal(0)},
            ("p-c2", "v-c2"): {"a": Decimal(0), "b": Decimal(1)},
            ("p-c3", "v-c3"): {"a": Decimal(0), "b": Decimal(0)},
            ("p-h1", "v-h1"): {"a": Decimal(1)},
            ("p-h2", "v-h2"): {"a": Decimal(2)},
            ("p-h3", "v-h3"): {"b": Decimal(3)},
            ("p-h4", "v-h4"): {"b": Decimal(4)},
        }
        self.reader = SimpleNamespace(
            stores=self.stores,
            offers=Mock(side_effect=lambda products: (datetime.now(timezone.utc),
                {key: row for key, row in available.items() if key[0] in products}, {})),
            balances=Mock(side_effect=lambda row: (datetime.now(timezone.utc),
                available.get((row["product"], row["id"]), {}))),
        )
        self.parse = Mock(side_effect=AssertionError("Категорийный запрос должен работать без LLM"))

    def reply(self, turns):
        self.reader.offers.reset_mock()
        self.reader.balances.reset_mock()
        data = {"склады": list(self.stores), "товары": self.rows}
        return LiveCatalog(data, self.reader).answer(turns, self.parse)

    def test_reported_conversation_uses_current_category_and_real_stock(self):
        dialogue = [
            ("Подскажи рашгарды Venum, какие у нас есть", ("Рашгард Venum Альфа",), ("Комплект",), 0),
            ("Подскажи какие капы в наличии", ("Капа BoyBo Jab", "Симферополь"), ("Рашгард", "Непроверенная"), 1),
            ("Какие КАПЫ в наличии", ("Капа BoyBo Jab", "Капа BoyBo Defender"), ("Другого бренда",), 1),
            ("какие капы в наличии", ("Капа BoyBo Jab",), ("Уточните размер или цвет",), 1),
            ("Какие бинты venum красного цвета есть?", ("Красный", "4.5 м"), ("Синий", "Капа"), 0),
            ("Какие капы boybo в наличии", ("Капа BoyBo Jab", "Капа BoyBo Defender"), ("Бинты",), 1),
            ("Какие “Капа Boybo” в наличии", ("Капа BoyBo Jab",), ("Бинты",), 1),
            ("Какие тренировочные шлема HARD-PRO есть в наличии?", ("HARD-PRO Один", "HARD-PRO Два"), ("Venum",), 1),
            ("Какие шлема для бокса есть в наличии?", ("HARD-PRO Один", "Venum Три"), ("Venum Четыре", "HARD-PRO Два"), 1),
            ("Нужны именно боксерские шлема, какие у нас есть", ("HARD-PRO Один", "Venum Три"), ("Venum Четыре",), 1),
            ("А если не HARD-PRO шлема, какие-то другие", ("Venum Три",), ("HARD-PRO Один", "Venum Четыре"), 1),
            ("Они подойдут для тхэквандо?", ("WT или ITF", "для бокса"), ("Найдено", "Venum Три"), 0),
        ]
        turns = []
        for question, required, absent, calls in dialogue:
            turns.append(question)
            with self.subTest(question=question):
                result = self.reply(turns)
                for text in required:
                    self.assertIn(text, result)
                for text in absent:
                    self.assertNotIn(text, result)
                self.assertEqual(self.reader.offers.call_count, calls)
        self.parse.assert_not_called()

    def test_city_followup_keeps_product_family_and_brand(self):
        result = self.reply(["Какие капы BoyBo в наличии?", "А в Симферополе?"])
        self.assertIn("Капа BoyBo Jab", result)
        self.assertNotIn("Капа BoyBo Defender", result)
        self.assertNotIn("Севастополь", result)
        self.reader.offers.assert_called_once_with(["p-c1", "p-c2"])

        result = self.reply(["Какая защита зубов в наличии?", "А в Севастополе?"])
        self.assertIn("Капа BoyBo Defender", result)
        self.assertNotIn("Капа BoyBo Jab", result)

    def test_city_followup_respects_an_exact_menu_selection(self):
        result = self.reply(["Какие капы BoyBo есть?", "№1", "А в Симферополе?"])
        self.assertIn("Капа BoyBo Jab", result)
        self.assertNotIn("Капа BoyBo Defender", result)
        self.reader.balances.assert_called_once()
        self.assertNotIn("Севастополь", result)

    def test_tournament_followup_links_only_to_selected_federation(self):
        result = self.reply(["Какие шлема для бокса есть в наличии?", "Они подойдут для тхэквандо?",
                             "WT, на соревнования"])
        self.assertIn("worldtaekwondo.org", result)
        self.assertIn("Допуск этих моделей пока не подтверждён", result)
        self.assertNotIn("itftkd.sport", result)
        self.reader.offers.assert_not_called()

    def test_missing_balance_and_partial_batch_never_mean_not_in_stock(self):
        self.rows = [catalog_row(str(i), f"Капа Демо {i}", "M") for i in range(BROWSE_LIMIT + 1)]
        result = self.reply(["Какие капы в наличии?"])
        self.assertIn("остаток не подтверждён", result)
        self.assertIn(f"{BROWSE_LIMIT} из {BROWSE_LIMIT + 1}", result)
        self.assertEqual(len(self.reader.offers.call_args.args[0]), BROWSE_LIMIT)

        self.reader.offers.side_effect = OSError("Reader unavailable")
        result = self.reply(["Какие капы в наличии?"])
        self.assertIn("пока не подтвердила остатки", result)

    def test_natural_cap_forms_also_work_on_model_parser_fallback(self):
        self.parse.side_effect = None
        self.parse.return_value = '{"product":"капы другого бренда","variant":"","warehouse":""}'
        result = self.reply(["Какие капы Другого бренда есть?"])
        self.assertIn("Капа Другого бренда", result)
        self.parse.assert_called_once()

    def test_named_product_refinement_goes_through_existing_parser(self):
        self.parse.side_effect = None
        self.parse.return_value = '{"product":"r1","variant":"M","warehouse":""}'
        result = self.reply(["Какие капы BoyBo в наличии?", "А покажи рашгард Venum Альфа M"])
        self.assertIn("Рашгард Venum Альфа", result)
        self.assertNotIn("Капа BoyBo", result)
        self.parse.assert_called_once()
        self.assertIn('"product": ""', self.parse.call_args.args[0][1]["content"])


if __name__ == "__main__":
    unittest.main()
