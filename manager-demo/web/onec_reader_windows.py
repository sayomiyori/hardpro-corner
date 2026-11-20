"""Read-only HTTP adapter for the Windows host of the 1C:Retail database."""
import argparse
import json
import ssl
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

EMPTY = "00000000-0000-0000-0000-000000000000"
RETAIL = "2c8c5ad6-9b1b-11e9-8950-bc5ff42b2249"
MSK = timezone(timedelta(hours=3))
PRODUCTS = """ВЫБРАТЬ ПЕРВЫЕ 65 Н.Код КАК code, Н.Наименование КАК title, Н.Артикул КАК article,
    УНИКАЛЬНЫЙИДЕНТИФИКАТОР(Н.Ссылка) КАК uid ИЗ Справочник.Номенклатура КАК Н
    ГДЕ НЕ Н.ПометкаУдаления И НЕ Н.ЭтоГруппа И Н.Код > &after
    УПОРЯДОЧИТЬ ПО Н.Код"""
VARIANTS = """ВЫБРАТЬ ПЕРВЫЕ 4097 Х.Владелец.Код КАК code, Х.Наименование КАК title,
    УНИКАЛЬНЫЙИДЕНТИФИКАТОР(Х.Ссылка) КАК uid ИЗ Справочник.ХарактеристикиНоменклатуры КАК Х
    ГДЕ НЕ Х.ПометкаУдаления И Х.Владелец.Код В (&codes)"""
STOCK = """ВЫБРАТЬ ПЕРВЫЕ 3 УНИКАЛЬНЫЙИДЕНТИФИКАТОР(О.Склад) КАК warehouse,
    О.КоличествоОстаток КАК on_hand, О.РезервОстаток КАК reserved
    ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки(, Номенклатура = &product
    И Характеристика = &variant И Склад В (&stores)) КАК О"""
OFFER_STOCK = """ВЫБРАТЬ ПЕРВЫЕ 8193 УНИКАЛЬНЫЙИДЕНТИФИКАТОР(О.Номенклатура) КАК product,
    УНИКАЛЬНЫЙИДЕНТИФИКАТОР(О.Характеристика) КАК variant, УНИКАЛЬНЫЙИДЕНТИФИКАТОР(О.Склад) КАК warehouse,
    О.КоличествоОстаток КАК on_hand, О.РезервОстаток КАК reserved
    ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки(, Номенклатура В (&products) И Склад В (&stores)) КАК О"""
OFFER_PRICE = """ВЫБРАТЬ ПЕРВЫЕ 8193 УНИКАЛЬНЫЙИДЕНТИФИКАТОР(Ц.Номенклатура) КАК product,
    УНИКАЛЬНЫЙИДЕНТИФИКАТОР(Ц.Характеристика) КАК variant, Ц.Цена КАК price, Ц.Валюта КАК currency
    ИЗ РегистрСведений.ЦеныНоменклатуры.СрезПоследних(&period, Номенклатура В (&products)
    И ВидЦены = &retail) КАК Ц
    ГДЕ Ц.Упаковка = ЗНАЧЕНИЕ(Справочник.УпаковкиНоменклатуры.ПустаяСсылка)"""
DETAILS = """ВЫБРАТЬ ПЕРВЫЕ 1 ВЫРАЗИТЬ(Н.Описание КАК СТРОКА(4000)) КАК description,
    Н.НаименованиеПолное КАК full_name, ПРЕДСТАВЛЕНИЕ(Н.Производитель) КАК manufacturer,
    ПРЕДСТАВЛЕНИЕ(Н.ВидНоменклатуры) КАК kind
    ИЗ Справочник.Номенклатура КАК Н ГДЕ Н.Ссылка = &product И НЕ Н.ПометкаУдаления И НЕ Н.ЭтоГруппа"""


class Retail:
    def __init__(self, config):
        import win32crypt
        from win32com.client import Dispatch
        encrypted = Path(config["password_dpapi"]).read_bytes()
        password = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8")
        password = password.replace('"', '""')
        self.db = Dispatch("V83.COMConnector").Connect(
            f'Srvr="Hoster";Ref="base";Usr="HP_Manager_Read";Pwd="{password}";WA=0;')
        if self.db.UserName() != "HP_Manager_Read":
            raise ValueError("Неверный пользователь 1С")
        self.stores = self.array([self.ref("Склады", str(UUID(uid))) for uid in config["warehouses"]])
        if len(set(config["warehouses"])) != 2:
            raise ValueError("Неверный состав складов")

    def array(self, values):
        result = self.db.NewObject("Array")
        for value in values:
            result.Add(value)
        return result

    def ref(self, catalog, uid):
        return getattr(self.db.Catalogs, catalog).GetRef(self.db.NewObject("UUID",uid))

    def rows(self, query, fields, **params):
        q = self.db.NewObject("Query")
        q.Text = query
        for key,value in params.items():
            q.SetParameter(key,value)
        cursor = q.Execute().Select()
        result = []
        while cursor.Next():
            result.append({key: (self.db.String(getattr(cursor,key)) if key in {"uid","warehouse","product","variant"}
                                  else str(getattr(cursor,key))) for key in fields})
        return result

    def catalog(self, after):
        if len(after) > 64 or any(ord(c)<32 for c in after):
            raise ValueError("Недопустимый курсор")
        products = self.rows(PRODUCTS, ["code","title","article","uid"], after=after)
        page = products[:64]
        if len({p["code"] for p in products}) != len(products):
            raise RuntimeError("Неоднозначный код номенклатуры")
        variants = {}
        if page:
            items = self.rows(VARIANTS, ["code","title","uid"], codes=self.array([p["code"] for p in page]))
            if len(items)>4096:
                raise RuntimeError("Слишком много характеристик")
            for v in items:
                variants.setdefault(v["code"],[]).append((v["uid"],v["title"]))
        result = []
        for p in page:
            for uid,label in sorted(variants.get(p["code"],[(EMPTY,"Без характеристики")]),
                                    key=lambda v:(v[1],v[0])):
                result.append({"код":p["code"],"товар":p["title"],"артикул":p["article"],
                               "product":p["uid"],"id":uid,"вариант":label})
        return {"items":result,"next":page[-1]["code"] if len(products)>64 else None}

    def stock(self, product, variant):
        product, variant = str(UUID(product)), str(UUID(variant))
        values = self.rows(STOCK, ["warehouse","on_hand","reserved"],
                           product=self.ref("Номенклатура",product),
                           variant=self.ref("ХарактеристикиНоменклатуры",variant), stores=self.stores)
        if len(values)>2:
            raise RuntimeError("Некорректный состав складов")
        return {"product":product,"variant":variant,"balances":values,
                "as_of":datetime.now(timezone.utc).isoformat()}

    def offers(self, products):
        keys = products.split(",", 24)
        if not 1 <= len(keys) <= 24:
            raise ValueError("От 1 до 24 товаров на запрос")
        ids = [str(UUID(key)) for key in keys]
        if EMPTY in ids or len(set(ids)) != len(ids):
            raise ValueError("Некорректные ссылки товаров")
        refs = self.array([self.ref("Номенклатура", key) for key in ids])
        stamp = datetime.now(timezone.utc)
        stocks = self.rows(OFFER_STOCK, ["product", "variant", "warehouse", "on_hand", "reserved"],
                           products=refs, stores=self.stores)
        from pywintypes import com_error
        # COM serializes the UTC clock fields. The register uses Moscow wall time.
        period = stamp.astimezone(MSK).replace(tzinfo=timezone.utc, microsecond=0)
        try:
            prices = self.rows(OFFER_PRICE, ["product", "variant", "price", "currency"], products=refs,
                               retail=self.ref("ВидыЦен", RETAIL), period=period)
        except com_error:
            prices = None
        if len(stocks) > 8192 or (prices is not None and len(prices) > 8192):
            raise RuntimeError("Слишком много складских или ценовых строк")
        return {"as_of": stamp.isoformat(), "balances": stocks, "prices": prices}

    def details(self, product):
        product = str(UUID(product))
        rows = self.rows(DETAILS, ["description", "full_name", "manufacturer", "kind"],
                         product=self.ref("Номенклатура", product))
        return {"product": product, "details": rows[0] if rows else None}


class Endpoint(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            uri = urlsplit(self.path)
            params = parse_qs(uri.query,keep_blank_values=True,max_num_fields=3,strict_parsing=True)
            if uri.path=="/v1/catalog" and set(params)=={"after"} and len(params["after"])==1:
                payload = self.server.retail.catalog(params["after"][0])
            elif uri.path=="/v1/stock" and set(params)=={"product","variant"} and all(len(v)==1 for v in params.values()):
                payload = self.server.retail.stock(params["product"][0],params["variant"][0])
            elif uri.path=="/v1/offers" and set(params)=={"products"} and len(params["products"])==1:
                payload = self.server.retail.offers(params["products"][0])
            elif uri.path=="/v1/details" and set(params)=={"product"} and len(params["product"])==1:
                payload = self.server.retail.details(params["product"][0])
            else:
                self.send_error(404)
                return
        except ValueError:
            self.send_error(400)
            return
        except Exception:
            self.send_error(503)
            self.server.retail = None
            return
        data = json.dumps(payload,ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self,*args):
        pass


class SecureServer(HTTPServer):
    def get_request(self):
        sock,address = super().get_request()
        sock.settimeout(8)
        try:
            return self.tls.wrap_socket(sock,server_side=True),address
        except OSError:
            sock.close()
            raise

    def finish_request(self,request,client_address):
        if self.retail is None:
            try:
                self.retail = Retail(self.config)
            except Exception:
                request.close()
                return
        super().finish_request(request,client_address)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("config",type=Path)
    args = cli.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cfg["cert"],cfg["key"])
    tls.load_verify_locations(cafile=cfg["client_ca"])
    tls.verify_mode = ssl.CERT_REQUIRED
    with SecureServer((cfg["bind"],int(cfg["port"])),Endpoint) as server:
        server.config, server.tls, server.retail = cfg, tls, None
        server.serve_forever()


if __name__=="__main__":
    main()
