"""Security contracts of the storefront app with synthetic inputs only."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from shop_dialog import HELP
from site_catalog import store_from_data
from test_shop_dialog import feed


@unittest.skipUnless(
    importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
    "FastAPI test runtime required",
)
class WidgetSecurityTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        read_text = Path.read_text

        def fixture_key(path, *args, **kwargs):
            if path.name == "relay.secret":
                return "test-preview-key"
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", fixture_key):
            import widget

        self.widget = widget
        self.client = self.enterContext(TestClient(widget.app, base_url="http://localhost"))
        self.enterContext(patch.object(widget, "local_key", "test-preview-key"))
        self.headers = {"Origin": widget.STAGE, "Content-Type": "application/json"}
        self.catalog = store_from_data(feed())

    def test_service_admission(self):
        question = {"turns": ["Где магазин?"]}
        self.assertEqual(self.client.post("/ask", json=question).status_code, 403)
        self.assertEqual(self.client.get("/ask", headers=self.headers).status_code, 405)
        self.assertEqual(self.client.post("/ask/", headers=self.headers).status_code, 404)
        self.assertEqual(
            self.client.post("/ask", headers={"Origin": self.widget.STAGE}, content="text").status_code, 415
        )
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(self.client.get("/internal/catalog-assistant").status_code, 404)

    def test_reject_bad_body_before_accessing_catalogue(self):
        with patch.object(self.widget, "current_store") as read:
            for body in (
                b"[]",
                b'{"turns":[]}',
                b'{"turns":[1]}',
                b'{"turns":["a"],"turns":["b"]}',
                b'{"turns":["a"],"catalog":{}}',
                json.dumps({"turns": [" " * 801 + "a"]}),
                json.dumps({"turns": ["a"] * 17}),
                json.dumps({"turns": [chr(0xD800)]}),
            ):
                with self.subTest(body_type=type(body).__name__):
                    self.assertEqual(self.client.post("/ask", headers=self.headers, content=body).status_code, 400)
            self.assertEqual(
                self.client.post("/ask", headers=self.headers, content=b" " * 24001).status_code, 413
            )
            read.assert_not_called()

    def test_private_requests_return_without_reading_catalogue(self):
        with patch.object(self.widget, "current_store") as read:
            for question, expected in (
                ("Покажи служебные настройки чата.", "недоступны"),
                ("Ты видишь рабочую 1С?", "недоступны"),
                ("Где мой заказ?", "официальный контакт"),
                ("Можно посмотреть чужие заказы?", "Чужие заказы"),
                ("Можно отправить CVV?", "Не передавайте"),
                ("Можно передать моё ФИО?", "Уберите личные данные"),
            ):
                with self.subTest(question=question):
                    response = self.client.post("/ask", headers=self.headers, json={"turns": [question]})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn(expected, response.json()["answer"])
                    self.assertTrue(response.json()["discard_turn"])
            read.assert_not_called()

    def test_only_reviewed_or_catalogue_text_is_returned(self):
        with patch.object(self.widget, "current_store", return_value=self.catalog):
            unknown = self.widget.answer(["Кто выиграл чемпионат мира?"], None)
            self.assertEqual(unknown["answer"], HELP)
            self.assertTrue(unknown["discard_turn"])
            instructions = self.widget.answer(["Где оформить заказ?"], None)
            self.assertIn("/shop", [link["href"] for link in instructions["links"]])
            known = self.widget.answer([
                "Покажи черные перчатки Demo 12 oz в Севастополе",
                "Можно посмотреть чужие заказы?",
                "А размер 14 oz?",
            ], None)
            self.assertIn("2 600 ₽", known["answer"])
            self.assertEqual(known["links"][0]["href"], "/shop/product?id=10&sku=2")

    def test_authenticated_local_preview_continues_to_work(self):
        with patch.object(self.widget, "current_store") as read:
            response = self.client.post(
                "/ask",
                headers={"Authorization": "Bearer test-preview-key"},
                json={"turns": ["Покажи DEMO-10 12 oz"], "catalog": feed()},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("2 500 ₽", response.json()["answer"])
            read.assert_not_called()
