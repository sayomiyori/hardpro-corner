"""Serve the upstream Flowise widget and only its buyer flow on localhost."""
import json
import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4
from urllib.error import HTTPError
from urllib.request import Request as RemoteRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path.home() / ".local/share/hardpro/catalog-web"
INFLIGHT = ROOT / "inflight"
INFLIGHT.mkdir(mode=0o700, exist_ok=True)
RELAY_KEY = (ROOT / "relay.secret").read_text().strip()
FLOW = os.environ["BUYER_FLOW"]
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/embed", StaticFiles(directory=ROOT / "flowise/node_modules/flowise-embed/dist"), name="embed")


@app.get("/inline")
@app.get("/stage/inline")
def inline(request: Request):
    prefix = "/catalog-assistant" if request.url.path.startswith("/stage/") else ""
    html = (Path(__file__).parent / "inline.html").read_text().replace("__FLOW__", str(UUID(FLOW))).replace("__PREFIX__", prefix)
    return HTMLResponse(html, headers={"Cache-Control":"no-store", "Content-Security-Policy":"frame-ancestors 'self' http://127.0.0.1:8094 https://hardpro.test.mp-bridge.ru", "Referrer-Policy":"same-origin"})


@app.get("/")
def home():
    html = (Path(__file__).parent / "widget.html").read_text().replace("__FLOW__", str(UUID(FLOW)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


def forward(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = RemoteRequest("http://127.0.0.1:8768/api/v1/" + path + "/" + FLOW,
                        data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=100) as r:
            payload = r.read()
            if path == "prediction" and r.headers.get_content_type() == "application/json":
                obj = json.loads(payload)
                payload = json.dumps({k: obj[k] for k in ("text","chatId","chatMessageId","sessionId") if k in obj}, ensure_ascii=False).encode()
            return Response(payload, media_type=r.headers.get_content_type())
    except (HTTPError, OSError):
        raise HTTPException(502, "Сервис каталога временно недоступен")


@app.get("/api/v1/{name}/{flow}")
def public_config(name: str, flow: str):
    if flow != FLOW or name not in {"public-chatbotConfig", "chatflows-streaming", "chatflows-uploads"}:
        raise HTTPException(404)
    if name == "chatflows-streaming":
        return {"isStreaming": False}
    return forward(name)


@app.post("/api/v1/prediction/{flow}")
async def predict(flow: str, request: Request):
    if flow != FLOW:
        raise HTTPException(404)
    if not secrets.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + RELAY_KEY).encode()):
        raise HTTPException(403)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > 8_000_000:
            raise HTTPException(413)
        data.extend(part)
    try:
        body = json.loads(data)
        question = body["question"]
        sid = str(UUID(body["chatId"]))
        if not isinstance(question, str) or not 0 < len(question.strip()) <= 1200:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Нужны вопрос и идентификатор чата")
    site = body.get("site")
    catalog = body.get("catalog")
    if site not in {"local","stage"} or not isinstance(catalog, dict) or not isinstance(catalog.get("rows"),list):
        raise HTTPException(400)
    nonce = str(uuid4())
    path = INFLIGHT / nonce
    try:
        path.write_text(json.dumps({"site":site,"catalog":catalog}, ensure_ascii=False))
        return await run_in_threadpool(forward, "prediction", {"question": f"shop:{site}:{nonce}\n{question}", "chatId": sid, "streaming": False})
    finally:
        path.unlink(missing_ok=True)
