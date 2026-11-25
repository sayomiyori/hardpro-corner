import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manager_knowledge import (
    competition_answer, definition_answer, expand_terms, merge_clarification, resolve_context, terminology,
)


class TerminologyTest(unittest.TestCase):
    def test_shopping_queries_keep_budget_size_and_article(self):
        cases = [
            ("Снарядки до 5к", "снарядные перчатки до 5к"),
            ("Тай‑пэды Fairtex до 8000", "тайские лапы fairtex до 8000"),
            ("Ищу кудоги 160", "ищу кимоно для кудо 160"),
            ("Хогу для ВТ", "защитный жилет для тхэквондо для вт"),
            ("Электронные футы на 39", "электронные носки для тхэквондо на 39"),
            ("шингарды для муайтай L", "защита голени для муайтай l"),
            ("Шлем BGR-2272w размер M", "шлем bgr-2272w размер m"),
            ("Кимоно GI-41", "кимоно gi-41"),
            ("Груша на растяжках", "груша на растяжках"),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(expand_terms(query), expected)

    def test_ambiguous_terms_ask_for_the_relevant_detail(self):
        for query, expected in [
            ("Какие блинчики до 5к?", "диски для штанги"),
            ("ФКР", "Федерацию карате России"),
            ("Ищу ги рост 150", "Для какой дисциплины"),
            ("Щитки размер M", "на предплечья"),
        ]:
            with self.subTest(query=query):
                self.assertIn(expected, resolve_context(query)[1])
        for query in ("ФКР поинтфайтинг", "Ищу ги для БЖЖ", "Ищу рашгард no gi", "Щитки на голень и стопу"):
            with self.subTest(query=query):
                self.assertIsNone(resolve_context(query)[1])
        self.assertIn("диски", expand_terms("Блинчики для штанги"))
        self.assertIn("снарядные перчатки", expand_terms("Блинчики для мешка"))

    def test_definitions_and_specific_rules_cite_sources(self):
        self.assertIn("397 г", definition_answer("14 oz это сколько в граммах?"))
        self.assertIn("nist.gov", definition_answer("Шо такое унцовки?"))
        self.assertIn("мишени", definition_answer("Тайские пады это что?"))
        self.assertIn("поинтфайтинг", definition_answer("Расшифруй PF"))
        self.assertIn("wako.sport", definition_answer("Расшифруй PF"))
        self.assertIn("ibjjf.com", definition_answer("Что такое no gi?"))
        self.assertIn("Тхэквондо ГТФ", competition_answer("Какая форма допускается на соревнованиях ГТФ?"))
        self.assertIn("Тхэквондо МФТ", competition_answer("Какие футы для соревнований МФТ?"))
        self.assertIn("Каратэ WKF", competition_answer("Подойдёт ли форма ФКР для кумитэ?"))
        self.assertIn("Кикбоксинг", competition_answer("Какие перчатки допустит ФКР на К1?"))

    def test_answer_to_clarification_keeps_original_intent(self):
        merged = merge_clarification(["Какие перчатки допущены ФКР?", "Каратэ"])
        self.assertEqual(len(merged), 1)
        self.assertIn("Каратэ WKF", competition_answer(merged[-1]))
        merged = merge_clarification(["Блинчики до 5000", "для мешка"])
        self.assertIn("снарядные перчатки до 5000", expand_terms(merged[-1]))
        self.assertEqual(merge_clarification(["Блинчики до 5000", "Ищу пояс дзюдо"]),
                         ["Блинчики до 5000", "Ищу пояс дзюдо"])

    def test_every_term_and_ambiguity_has_a_source(self):
        data = terminology()
        ids = set()
        source_ids = data["sources"].keys()
        for source in data["sources"].values():
            parsed = urlsplit(source["url"])
            self.assertTrue(parsed.scheme == "https" and parsed.hostname and not parsed.username)
        for group in data["groups"]:
            self.assertIn(group["family"], data["families"])
            self.assertTrue(group["terms"] and group["sources"])
            for entry in group["terms"]:
                self.assertNotIn(entry["id"], ids)
                ids.add(entry["id"])
                self.assertTrue(entry["aliases"] and entry["note"])
                self.assertLessEqual(set(entry.get("attested", [])), set(entry["aliases"]))
                self.assertLessEqual(set(entry.get("sources") or group["sources"]), source_ids)
        for entry in data["ambiguities"]:
            self.assertGreaterEqual(len(entry["cases"]), 2)
            self.assertLessEqual(set(entry["sources"]), source_ids)
