"""Security contracts of the storefront app with synthetic inputs only."""

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
                b'{"turns":["a"],"context":{"product":true}}',
                b'{"turns":["a"],"context":{"product":10,"sku":0}}',
                b'{"turns":["a"],"context":{"product":10,"city":"other"}}',
                b'{"turns":["a"],"context":{"product":10,"price":1}}',
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

    def test_product_page_context_uses_only_published_variants(self):
        context = {"product": 10, "sku": 1, "city": "Симферополь"}
        accepted_cases = (
            (["Сколько стоит?"], context, 1, "Симферополь — доступно 1"),
            (["а такие же на 14 унций есть?"], context, 2, "Симферополь — доступно 0"),
            (["а такие же на 14 унций есть?", "А в Севастополе?"], context, 2, "Севастополь — доступно 1"),
            (["Покажи капу DEMO-30"], context, 4, "Капа для бокса"),
            (["а такие же на 14 унций есть?"], {**context, "sku": 4}, 2, "Симферополь — доступно 0"),
        )
        with patch.object(self.widget, "current_store", return_value=self.catalog):
            for turns, hint, sku, detail in accepted_cases:
                with self.subTest(turns=turns, hint=hint):
                    response = self.client.post("/ask", headers=self.headers, json={"turns": turns, "context": hint})
                    self.assertEqual(response.status_code, 200)
                    result = response.json()
                    self.assertIn(detail, result["answer"])
                    self.assertTrue(result["links"][0]["href"].endswith(f"&sku={sku}"))
            unknown = self.client.post("/ask", headers=self.headers, json={
                "turns": ["А такие же на 14 унций есть?"], "context": {**context, "product": 999},
            })
            self.assertEqual(unknown.json()["answer"], HELP)

    def test_handoff_requires_confirmed_transport_and_reuses_receipt(self):
        payload = {"handoff": {"id": "0123456789abcdef" * 2, "question": "Есть такая модель на 14 oz?",
                              "context": {"product": 10, "sku": 2, "city": "Симферополь"}}}
        config = {"MAX_SUPPORT_TOKEN": "synthetic", "MAX_SUPPORT_CHAT_ID": "-101"}
        with TemporaryDirectory() as state, patch.dict("os.environ", {**config, "MAX_SUPPORT_STATE": state}), (
            patch.object(self.widget, "current_store", return_value=self.catalog)
        ), patch.object(self.widget.max_handoff, "send_to_max") as send:
            self.assertEqual(self.client.post("/ask", headers=self.headers, json={"handoff_status": True}).json(),
                             {"available": True})
            send.assert_not_called()
            response = self.client.post("/ask", headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json(), {"status": "delivered", "receipt": "HP-0123456789AB",
                                               "store_hours": "Вт–Сб 11:00–19:00"})
            message = send.call_args.args[0]
            self.assertIn("Вариант: 14 oz, Черный", message)
            self.assertIn("Магазин: Симферополь", message)
            self.assertIn("Вопрос: Есть такая модель на 14 oz?", message)
            again = self.client.post("/ask", headers=self.headers, json=payload)
            self.assertEqual(again.json(), response.json())
            send.assert_called_once()
            self.assertNotIn(payload["handoff"]["question"].encode(), (Path(state) / "receipts.sqlite3").read_bytes())

    def test_handoff_refuses_private_questions_and_never_retries_ambiguous_delivery(self):
        payload = {"handoff": {"id": "a" * 32, "question": "Дайте совет по выбору капы."}}
        with TemporaryDirectory() as state, patch.dict("os.environ", {
            "MAX_SUPPORT_TOKEN": "synthetic", "MAX_SUPPORT_CHAT_ID": "-101", "MAX_SUPPORT_STATE": state,
        }), patch.object(self.widget.max_handoff, "send_to_max", side_effect=TimeoutError) as send:
            private = {"handoff": {**payload["handoff"], "question": "Мой телефон +7 000 000 00 00"}}
            self.assertEqual(self.client.post("/ask", headers=self.headers, json=private).status_code, 400)
            send.assert_not_called()
            for _ in range(2):
                response = self.client.post("/ask", headers=self.headers, json=payload)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["status"], "unconfirmed")
            changed = {"handoff": {**payload["handoff"], "question": "Есть капа для тренировок?"}}
            self.assertEqual(self.client.post("/ask", headers=self.headers, json=changed).status_code, 409)
            send.assert_called_once()

    def test_handoff_validates_variant_and_obeys_global_rate_limit(self):
        payload = {"handoff": {"id": "b" * 32, "question": "Есть такой вариант?",
                              "context": {"product": 10, "sku": 4}}}
        with TemporaryDirectory() as state, patch.dict("os.environ", {
            "MAX_SUPPORT_TOKEN": "synthetic", "MAX_SUPPORT_CHAT_ID": "-101", "MAX_SUPPORT_STATE": state,
        }), patch.object(self.widget, "current_store", return_value=self.catalog), (
            patch.object(self.widget.max_handoff, "REQUEST_LIMIT", 1)
        ), patch.object(self.widget.max_handoff, "send_to_max") as send:
            self.assertEqual(self.client.post("/ask", headers=self.headers, json=payload).json()["status"], "catalog_changed")
            send.assert_not_called()
            payload["handoff"]["context"]["sku"] = 2
            self.assertEqual(self.client.post("/ask", headers=self.headers, json=payload).status_code, 201)
            payload["handoff"]["id"] = "c" * 32
            self.assertEqual(self.client.post("/ask", headers=self.headers, json=payload).status_code, 429)
            send.assert_called_once()

    def test_max_transport_sends_to_configured_chat_and_requires_receipt(self):
        transport = self.widget.max_handoff
        with patch.dict("os.environ", {"MAX_SUPPORT_TOKEN": "test-only", "MAX_SUPPORT_CHAT_ID": "-101"}), (
            patch.object(transport, "HTTPSConnection")
        ) as connect:
            conn = connect.return_value
            response = conn.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"message":{"body":{"mid":"test-receipt"}}}'
            transport.send_to_max("Контрольное сообщение")
            self.assertEqual(connect.call_args.args, ("platform-api2.max.ru",))
            method, route, body, headers = conn.request.call_args.args
            self.assertEqual((method, route), ("POST", "/messages?chat_id=-101"))
            self.assertEqual(headers["Authorization"], "test-only")
            self.assertEqual(json.loads(body)["text"], "Контрольное сообщение")
            response.read.return_value = b'{"message":{"body":{}}}'
            with self.assertRaises(ValueError):
                transport.send_to_max("Контрольное сообщение")

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

    def test_busy_service_asks_client_to_wait(self):
        class Busy:
            async def acquire(self):
                raise TimeoutError

        with patch.object(self.widget, "slots", Busy()), patch.object(self.widget, "current_store") as read:
            response = self.client.post("/ask", headers=self.headers, json={"turns": ["Покажи каталог."]})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "2")
            read.assert_not_called()

    def test_manager_contact_without_live_catalogue(self):
        with patch.object(self.widget, "current_store") as read:
            reply = self.client.post("/ask", headers=self.headers, json={"turns": ["Позови менеджера."]})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["links"][0]["action"], "manager")
            read.assert_not_called()
