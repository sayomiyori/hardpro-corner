"""Public catalogue answers behind the storefront edge."""
import asyncio
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request as FeedRequest, build_opener

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path.home() / ".local/share/hardpro"
sys.path.append(str(ROOT / "onec-pilot/full-release"))
from price_history import PriceHistory
from shop_dialog import HELP, ShopDialog, blocked
from site_catalog import store_from_data

FEED = "http://127.0.0.1:18865/internal/catalog-assistant"
MIME = "application/json"
STAGE = "https://hardpro.test.mp-bridge.ru"
local_key = (ROOT / "catalog-web/relay.secret").read_text().strip()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1","localhost"])
slots = asyncio.Semaphore(3)
snapshot_lock = threading.Lock()
snapshot_until = 0
snapshot_data = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_store():
    req = FeedRequest(FEED, headers={"Host":"127.0.0.1:18094","Accept":MIME})
    with build_opener(NoRedirect()).open(req,timeout=9) as response:
        if response.headers.get_content_type()!=MIME:
            raise ValueError("Invalid storefront response")
        raw = response.read(8_000_001)
        if len(raw)>8_000_000:
            raise ValueError("Catalog exceeds limit")
        return json.loads(raw)


def current_store():
    global snapshot_data, snapshot_until
    with snapshot_lock:
        if time.monotonic() >= snapshot_until:
            snapshot_data = store_from_data(fetch_store())
            snapshot_until = time.monotonic()+8
        return snapshot_data


def answer(turns, snapshot):
    refusal = blocked(turns[-1])
    if refusal:
        message,links = refusal
        return {"answer":message,"links":links,"discard_turn":True}
    catalog, stamp = current_store() if snapshot is None else store_from_data(snapshot)
    history = PriceHistory() if snapshot is None else None
    message,links = ShopDialog(catalog,history).answer(turns)
    return {"answer":message,"links":links,"as_of":stamp,"discard_turn":message==HELP}


@app.post("/ask")
async def ask(request: Request):
    auth = request.headers.get("authorization","")
    local = bool(local_key) and secrets.compare_digest(auth.encode(),("Bearer "+local_key).encode())
    if not local and (auth or request.headers.get("origin")!=STAGE):
        raise HTTPException(403)
    if request.headers.get("content-type","").split(";",1)[0].strip().lower()!=MIME:
        raise HTTPException(415)
    limit = 8_000_000 if local else 24_000
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>limit:
            raise HTTPException(413)
        raw.extend(chunk)
    try:
        body = json.loads(raw)
        if set(body)!=({"catalog","turns"} if local else {"turns"}):
            raise ValueError()
        turns = body["turns"]
        if not isinstance(turns,list) or not 1<=len(turns)<=16 or not all(
            isinstance(q,str) and 0<len(q.strip())<=800 for q in turns
        ):
            raise ValueError()
        snapshot = body["catalog"] if local else None
        if local and not isinstance(snapshot,dict):
            raise ValueError()
    except (ValueError,KeyError,TypeError,RecursionError):
        raise HTTPException(400,"Некорректный диалог")
    try:
        await asyncio.wait_for(slots.acquire(),timeout=1)
    except TimeoutError:
        raise HTTPException(429,"Помощник занят. Повторите через несколько секунд.")
    try:
        result = await run_in_threadpool(answer,turns,snapshot)
        return JSONResponse(result,headers={"Cache-Control":"no-store"})
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error):
        raise HTTPException(503,"Сейчас не удаётся сверить данные магазина. Попробуйте ещё раз.")
    finally:
        slots.release()


if __name__=="__main__":
    os.umask(0o077)
    feed = fetch_store()
    catalog, _ = store_from_data(feed)
    if catalog.rows and any(row["price_cents"] for row in catalog.rows):
        PriceHistory().observe(catalog,feed["as_of"])
    else:
        raise SystemExit("The storefront did not provide prices")
