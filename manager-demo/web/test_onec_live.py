import json
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_knowledge import competition_answer, expand_terms
from onec_live import LiveCatalog, OnecReader, budget_value
from onec_reader_windows import Retail

P="00000000-0000-4000-8000-000000000001"
V="00000000-0000-4000-8000-000000000002"
A="00000000-0000-4000-8000-000000000003"
B="00000000-0000-4000-8000-000000000004"
P2="00000000-0000-4000-8000-000000000005"
V2="00000000-0000-4000-8000-000000000006"
V3="00000000-0000-4000-8000-000000000007"
P3="00000000-0000-4000-8000-000000000008"
P4="00000000-0000-4000-8000-000000000009"


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

    def test_stock_for_three_variants_and_separate_site_price(self):
        rows = [{**self.item, "id": uid, "вариант": f"12 oz, {color}"} for uid, color in
                ((V, "Красный"), (V2, "Синий"), (V3, "Черный"))]
        stamp = datetime.now(timezone.utc)
        self.client.balances = Mock(side_effect=[
            (stamp, {A: Decimal(2), B: Decimal(1)}),
            (stamp, {A: Decimal(0)}),
            (stamp, {B: Decimal(-1)}),
        ])
        parse = Mock(return_value='{"product":"DEMO","variant":"12 oz","warehouse":""}')
        cat = LiveCatalog({"склады": list(self.client.stores), "товары": rows}, self.client)
        result = cat.answer(["DEMO 12 oz", "Покажи актуальное количество и их цены на сайте"], parse)
        self.assertEqual(self.client.balances.call_count, 3)
        parse.assert_called_once()
        self.assertIn("Склад А — свободно 0.", result)
        self.assertIn("Склад Б — в регистре нет строки", result)
        self.assertIn("Склад Б — свободных нет, расчётный остаток 1С -1.", result)
        self.assertIn("Цены сайта по этим вариантам пока недоступны.", result)
        self.assertTrue(all(row["вариант"] in result for row in rows))
        self.assertNotIn("Найдено 3", result)

        self.client.balances.side_effect = None
        self.client.balances.return_value = stamp, {A: Decimal(1), B: Decimal(1)}
        fresh = LiveCatalog({"склады": list(self.client.stores), "товары": rows}, self.client)
        answer = fresh.answer(["DEMO 12 oz", "Покажи количество этих 3х перчаток"], parse)
        self.assertEqual(answer.count("Склад А — свободно 1."), 3)

    def test_budget_requires_confirmed_price_and_stock_in_every_requested_store(self):
        rows = [
            {**self.item, "код": str(i), "product": product, "товар": f"Снарядные перчатки {name}"}
            for i, (product, name) in enumerate([(P, "Альфа"), (P2, "Бета"), (P3, "Гамма"), (P4, "Дельта")], 1)
        ]
        balances = {(product, V): {A: Decimal(3), B: Decimal(1)} for product in (P, P3, P4)}
        balances[P2, V] = {A: Decimal(1)}
        prices = {(P, V): Decimal("5000"), (P2, V): Decimal("999.99"), (P3, V): Decimal("5000.01")}
        self.client.offers = Mock(return_value=(datetime.now(timezone.utc), balances, prices))
        def parse(messages):
            self.assertEqual(json.loads(messages[1]["content"])["вопрос"], "найди мне снарядные перчатки")
            return '{"product":"снарядные перчатки","variant":"","warehouse":""}'
        cat = LiveCatalog({"склады": list(self.client.stores), "товары": rows}, self.client)
        answer = cat.answer(["Найди мне снарядки до 5к рублей"], parse)
        self.assertIn("Альфа", answer)
        self.assertNotRegex(answer, "Бета|Гамма|Дельта")
        self.assertIn("5 000,00 ₽", answer)
        self.assertIn("Склад Б — свободно 1.", answer)
        self.assertIn("без подтверждённой розничной цены", answer)
        self.client.offers.assert_called_once_with([P, P2, P3, P4])

    def test_details_are_read_for_the_exact_product(self):
        reader = self.client
        reader.details = Mock(return_value={
            "manufacturer": "HARD-PRO", "kind": "Перчатки", "full_name": "Перчатки Демо",
            "description": "Синтетическая кожа",
        })
        cat = LiveCatalog({"склады": list(reader.stores), "товары": [self.item]}, reader)
        result = cat.answer(["Описание и какие размеры у DEMO?"], lambda _: '{"product":"DEMO","variant":"","warehouse":""}')
        reader.details.assert_called_once_with(self.item)
        reader.get.assert_not_called()
        self.assertIn("Характеристики в 1С: M.", result)
        self.assertIn("Описание: Синтетическая кожа", result)

    def test_batched_read_rejects_foreign_products_and_does_not_invent_prices(self):
        entry = {"product": P, "variant": V, "price": "5000,00", "currency": ""}
        payload = {**self.response, "balances": [{**self.response["balances"][0], "product": P, "variant": V}],
                   "prices": [entry]}
        self.client.get.return_value = payload
        _, stocks, prices = self.client.offers([P])
        self.assertEqual(stocks[P, V], {A: Decimal(0)})
        self.assertEqual(prices[P, V], Decimal("5000"))
        for entries in ([{**entry, "product": P2}], [entry, entry]):
            self.client.get.return_value = {**payload, "prices": entries}
            with self.assertRaises(ValueError):
                self.client.offers([P])
        self.client.get.return_value = {**payload, "prices": None}
        self.assertEqual(self.client.offers([P])[2], {})

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

    def test_reader_keeps_stock_if_price_role_is_missing_and_rejects_duplicate_products(self):
        class ComError(Exception):
            pass

        reader = Retail.__new__(Retail)
        reader.stores = [A, B]
        reader.array = list
        reader.ref = Mock(side_effect=lambda _, uid: uid)
        balances = [{"product": P, "variant": V, **self.response["balances"][0]}]
        reader.rows = Mock(side_effect=[balances, ComError()])
        with patch.dict(sys.modules, {"pywintypes": SimpleNamespace(com_error=ComError)}):
            result = reader.offers(P)
        self.assertEqual(result["balances"], balances)
        self.assertIsNone(result["prices"])
        self.assertEqual(reader.rows.call_count, 2)
        with self.assertRaises(ValueError):
            reader.offers(f"{P},{P}")
        self.assertEqual(reader.rows.call_count, 2)

    @patch("onec_live.socket.create_connection")
    @patch("onec_live.HTTPSConnection")
    @patch("onec_live.ssl.create_default_context")
    def test_ssh_relay_keeps_server_identity(self, tls_context, https, connect):
        reader = OnecReader({"host": "192.0.2.1", "port": 18771, "ssh_relay": True,
                             "ca": "ca", "cert": "cert", "key": "key", "warehouses": {"A": A, "B": B}})
        response = https.return_value.getresponse.return_value
        response.status = 200
        response.headers.get_content_type.return_value = "application/json"
        response.read.return_value = b"{}"
        sock = MagicMock()
        connect.return_value.__enter__.return_value = sock
        https.return_value.timeout = 20

        self.assertEqual(reader.get("/v1/catalog"), {})
        connect.assert_called_once_with(("127.0.0.1", 18771), 20)
        reader.tls.wrap_socket.assert_called_once_with(sock, server_hostname="192.0.2.1")
        self.assertIs(https.return_value.sock, reader.tls.wrap_socket.return_value)
        https.return_value.close.assert_called_once()


class ManagerKnowledgeTest(unittest.TestCase):
    def test_slang_and_budget(self):
        self.assertEqual(expand_terms("Снарядки Грин Хилл"), "снарядные перчатки Green Hill")
        self.assertEqual(expand_terms("Блинчики для штанги"), "диски для штанги")
        self.assertEqual(expand_terms("Блинчики"), "блинчики")
        for text, value in (("до 5к рублей", "5000"), ("не дороже 4,5 тыс. руб.", "4500"),
                            ("в пределах 4 999,99 ₽", "4999.99"), ("до 5 кг", None)):
            with self.subTest(text=text):
                self.assertEqual(budget_value(text), Decimal(value) if value else None)

    def test_rules_use_documents_and_disambiguate_discipline(self):
        self.assertIsNone(competition_answer("Сколько перчаток ММА в наличии?"))
        self.assertIn("Уточните дисциплину", competition_answer("Что надеть на соревнования по тхэквондо?"))
        self.assertIn("itf-russia.ru", competition_answer("Подходит ли добок для соревнований по тхэквондо ИТФ?"))
        self.assertIn("Боевое самбо", competition_answer("Какая форма на соревнованиях по боевому самбо?"))
        rule = competition_answer("Какие перчатки подойдут для ФБР?")
        self.assertIn("BGR-2272w", rule)
        self.assertIn("В карточке HARD-PRO", rule)
        self.assertNotIn("BGR-2272w", competition_answer("Какой шлем подойдёт для ФБР?"))
