"""Проверка импорта на вымышленных товарах и складах."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from import_onec_snapshot import collect


class SnapshotTest(unittest.TestCase):
    def test_reserved_zero_missing_and_checksum(self):
        scope = {"complete_for_declared_scope": True, "completed_codes": ["1"]}
        variant = {"product_uuid": "p", "characteristic_uuid": "m"}
        stores = {"a": "Склад А", "b": "Склад Б", "c": "Склад В"}
        stock = {**scope, "finished_utc": "2026-01-01T09:00:00Z", "rows": [
            {**variant, "physical_warehouse_uuid": "a", "on_hand": 7, "reserved": 2},
            {**variant, "physical_warehouse_uuid": "b", "on_hand": 2, "reserved": 2},
        ]}
        profile = {"configuration": "Розница", "profile": "work"}
        files = {
            "connection-info.json": {"before": profile, "after": profile},
            "catalog-reference-manifest.json": {"rows": [
                {"uuid": "p", "name": "Пример", "article": "X", "code": "1", "deleted": False, "is_group": False}
            ]},
            "characteristics-snapshot.json": {**scope, "rows": [{**variant, "deleted": False, "name": "M"}]},
            "stocks-snapshot.json": stock,
        }
        selection = {"variants": [variant], "warehouses": stores}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            entries = []
            for name, data in files.items():
                encoded = json.dumps(data).encode()
                (directory / name).write_bytes(encoded)
                entries.append({"path": name, "sha256": hashlib.sha256(encoded).hexdigest()})
            manifest = json.dumps({"files": entries}).encode()
            (directory / "evidence-manifest.json").write_bytes(manifest)
            digest = hashlib.sha256(manifest).hexdigest()
            result = collect(directory, digest, selection)
            rows = result["товары"][0]["склады"]
            self.assertEqual(result["момент"], "01.01.2026 12:00 МСК")
            self.assertEqual(rows["Склад А"]["свободно"], "5")
            self.assertEqual(rows["Склад Б"]["свободно"], "0")
            self.assertNotIn("свободно", rows["Склад В"])
            (directory / "stocks-snapshot.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "контрольная сумма"):
                collect(directory, digest, selection)


if __name__ == "__main__":
    unittest.main()
