"""Local shop preview. The hosted storefront has its own direct server link."""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ORIGIN="http://127.0.0.1:8094"
KEY=(Path(__file__).parent/"relay.secret").read_text().strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def approved(self):
        return (self.headers.get("Host")=="127.0.0.1:8764" and self.path=="/ask"
                and self.headers.get("Origin")==ORIGIN)

    def send_body(self,status,mime,data):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin",ORIGIN)
        self.send_header("Access-Control-Allow-Methods","POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        if data:self.wfile.write(data)

    def do_OPTIONS(self):
        if not self.approved():return self.send_error(403)
        self.send_body(204,"text/plain",b"")

    def do_POST(self):
        if not self.approved():return self.send_error(403)
        try:
            n=int(self.headers.get("Content-Length","0"))
            if not 0<n<=24000 or self.headers.get_content_type()!="application/json":return self.send_error(400)
            turns=json.loads(self.rfile.read(n))["turns"]
            with self.get_connection(8094) as src:
                src.request("GET","/internal/catalog-assistant")
                res=src.getresponse()
                if res.status!=200:raise OSError()
                catalog=json.loads(res.read(8_000_001))
            with self.get_connection(8766) as dst:
                dst.request("POST","/ask",json.dumps({"catalog":catalog,"turns":turns},ensure_ascii=False).encode(),
                            {"Content-Type":"application/json","Authorization":"Bearer "+KEY})
                res=dst.getresponse()
                self.send_body(res.status,"application/json; charset=utf-8",res.read(65536))
        except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException):
            self.send_body(503,"application/json",b'{"detail":"catalog_unavailable"}')

    @staticmethod
    def get_connection(port):
        from contextlib import closing
        return closing(http.client.HTTPConnection("127.0.0.1",port,timeout=75))


if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",8764),Handler).serve_forever()
