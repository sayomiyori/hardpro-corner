import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onec_live import CatalogNotReady, OnecReader


def item(number, title):
    return {
        "product": str(UUID(int=number)),
        "id": str(UUID(int=number + 100)),
        "код": f"00-{number}",
        "товар": f"Кимоно дзюдо {title}",
        "артикул": title,
        "вариант": "140 см",
    }


def parse_kimono(_messages):
    return json.dumps({"product": "кимоно дзюдо", "variant": "140", "warehouse": ""})


class CatalogSnapshotTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.path = Path(temporary) / "data" / "catalog-snapshot.json"
        self.first = item(1, "Alpha")
        self.second = item(2, "Beta")
        self.stop = Event()

    def reader(self):
        with patch("onec_live.ssl.create_default_context"):
            return OnecReader({"host": "example.invalid", "port": 443, "ca": "ca.pem",
                               "key": "key.pem", "cert": "cert.pem",
                               "warehouses": {"Склад А": str(UUID(int=10)), "Склад Б": str(UUID(int=11))}},
                              self.path)

    def write_initial_snapshot(self, reader):
        reader.get = Mock(side_effect=[
            {"items": [self.first], "next": self.first["код"]},
            {"items": [self.second], "next": None},
        ])
        reader.refresh_snapshot(self.stop)
        self.assertEqual(reader.get.call_count, 2)

    def test_restart_during_outage_keeps_search_and_reports_unknown_stock(self):
        self.write_initial_snapshot(self.reader())
        restarted = self.reader()
        restarted.get = Mock(side_effect=OSError("offline"))
        restarted.load_snapshot()
        result = restarted.session().answer(["Кимоно дзюдо на рост 140 см"], parse_kimono)
        self.assertIn("Справочник 1С на", result)
        self.assertIn("№1 Кимоно дзюдо Alpha", result)
        self.assertIn("№2 Кимоно дзюдо Beta", result)
        restarted.get.assert_not_called()

        result = restarted.session().answer(["Кимоно дзюдо на рост 140 см", "№1"], parse_kimono)
        self.assertIn("Кимоно дзюдо Alpha", result)
        self.assertIn("Актуальное наличие в 1С подтвердить не удалось", result)
        self.assertNotIn("свободно 0", result)
        restarted.get.assert_called_once_with("/v1/stock", product=self.first["product"], variant=self.first["id"])

    def test_interrupted_or_invalid_refresh_preserves_last_complete_snapshot(self):
        reader = self.reader()
        self.write_initial_snapshot(reader)
        original = self.path.read_bytes()
        for last_page, expected in [
            (OSError("connection lost"), OSError),
            ({"items": [self.first], "next": None}, ValueError),
        ]:
            with self.subTest(error=expected):
                reader.get = Mock(side_effect=[{"items": [self.first], "next": self.first["код"]}, last_page])
                with self.assertRaises(expected):
                    reader.refresh_snapshot(self.stop)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(reader.session().rows, [self.first, self.second])
        reader.get = Mock(return_value={"items": [self.second], "next": None})
        with patch.object(Path, "replace", side_effect=PermissionError), self.assertRaises(PermissionError):
            reader.refresh_snapshot(self.stop)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(reader.session().rows, [self.first, self.second])
        self.assertFalse(list(self.path.parent.glob(".catalog-*")))

    def test_existing_catalog_remains_available_during_refresh(self):
        reader = self.reader()
        self.write_initial_snapshot(reader)
        entered, release = Event(), Event()
        loop_errors = []

        def get_page(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Refresh blocked the test")
            return {"items": [self.second], "next": None}

        def refresh():
            try:
                reader.refresh_snapshot(self.stop)
            except Exception as exc:
                loop_errors.append(exc)

        reader.get = get_page
        worker = Thread(target=refresh, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(reader.session().rows, [self.first, self.second])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(loop_errors, [])
        self.assertEqual(reader.session().rows, [self.second])
        restarted = self.reader()
        restarted.load_snapshot()
        self.assertEqual(restarted.session().rows, [self.second])

    def test_missing_or_corrupt_disk_snapshot_has_no_partial_index(self):
        reader = self.reader()
        reader.load_snapshot()
        with self.assertRaises(CatalogNotReady):
            reader.session()
        self.path.parent.mkdir()
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertLogs("onec_live", level="WARNING"):
            reader.load_snapshot()
        with self.assertRaises(CatalogNotReady):
            reader.session()
