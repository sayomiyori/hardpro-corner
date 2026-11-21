import os
import unittest
from pathlib import Path

from retrieval import MODES, KnowledgeIndex, load_guides


@unittest.skipUnless(os.environ.get("FASTEMBED_CACHE_PATH"), "Укажите папку локальных моделей FastEmbed")
class KnowledgeIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = KnowledgeIndex(load_guides(), Path(os.environ["FASTEMBED_CACHE_PATH"]))
        cls.addClassCleanup(cls.index.client.close)

    def test_searches_by_exact_article(self) -> None:
        for mode in ("lexical", "hybrid"):
            with self.subTest(mode=mode):
                found = self.index.search("Артикул SWB1002", mode)
                self.assertEqual(found[0].id, "sasung-white-belt")

    def test_filtered_tournament_request_cannot_return_product_card(self) -> None:
        query = "Допустят ли добок Sasung ITF White Belt на Кубок ITF?"
        for mode in MODES:
            with self.subTest(mode=mode):
                found = self.index.search(query, mode, scope="itf")
                self.assertEqual([guide.id for guide in found], ["itf-equipment"])

    def test_model_scope_excludes_other_fitting_instructions(self) -> None:
        found = self.index.search("Как подогнать новую капу в горячей воде?", scope="sisu-3d")
        self.assertTrue(found)
        self.assertEqual({guide.scope for guide in found}, {"sisu-3d"})
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self.index.search("Как подогнать капу?", mode, scope="missing-model"), [])

    def test_invalid_query_and_scope_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.index.search("  ")
        with self.assertRaises(ValueError):
            self.index.search("а" * 1001)
        with self.assertRaises(ValueError):
            self.index.search("Как найти инструкцию?", scope="модель?")


if __name__ == "__main__":
    unittest.main()
