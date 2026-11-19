"""The shop-side preview relay sends a fresh public catalogue with each prediction."""
import http.client
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

STAGE = "https://hardpro.test.mp-bridge.ru"
LOCAL = "http://127.0.0.1:8764"
KEY = (Path(__file__).parent / "relay.secret").read_text().strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def upstream(self, method, path, port, headers=None, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=100)
        try:
            conn.request(method, path, body=data, headers=headers or {})
            res = conn.getresponse()
            return res.status, res.getheader("Content-Type","application/octet-stream"), res.read(8_000_001)
        finally:
            conn.close()

    def reply(self, status, mime, body):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        return self.headers.get("Host") in {"127.0.0.1:8764","localhost:8764"}

    def do_GET(self):
        if not self.allowed_host() or not re.fullmatch(r"/(?:|inline|stage/inline|embed/web\.js|api/v1/(?:public-chatbotConfig|chatflows-streaming|chatflows-uploads)/[a-f0-9-]{36})(?:\?[^#\s]*)?", self.path):
            return self.send_error(404)
        try:
            self.reply(*self.upstream("GET", self.path, 8766))
        except OSError:
            self.send_error(503)

    def do_POST(self):
        origin=self.headers.get("Origin")
        if not self.allowed_host() or origin not in {LOCAL,STAGE,"http://localhost:8764"}:
            return self.send_error(403)
        if not re.fullmatch(r"/api/v1/prediction/[a-f0-9-]{36}",self.path):
            return self.send_error(404)
        try:
            size=int(self.headers.get("Content-Length","-1"))
            if not 0 < size <= 131072 or self.headers.get_content_type()!="application/json":
                return self.send_error(400)
            body=json.loads(self.rfile.read(size))
            q=body["question"]
            sid=str(UUID(body["chatId"]))
            if not isinstance(q,str) or not 0<len(q.strip())<=1200:
                return self.send_error(400)
        except (ValueError,KeyError,TypeError,AttributeError):
            return self.send_error(400)
        stage=origin==STAGE
        try:
            status,mime,raw=self.upstream("GET","/internal/catalog-assistant",18765 if stage else 8094,
                                         {"Host":"127.0.0.1:"+("18094" if stage else "8094")})
            if status!=200 or "application/json" not in mime or len(raw)>8_000_000:
                return self.send_error(503)
            snapshot=json.loads(raw)
            data=json.dumps({"chatId":sid,"question":q,"site":"stage" if stage else "local",
                             "catalog":snapshot},ensure_ascii=False).encode()
            self.reply(*self.upstream("POST",self.path,8766,{"Authorization":"Bearer "+KEY,
                        "Content-Type":"application/json"},data))
        except (OSError,ValueError,http.client.HTTPException):
            self.send_error(503)


if __name__=="__main__":
    ThreadingHTTPServer(("127.0.0.1",8764),Handler).serve_forever()
