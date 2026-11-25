"""Public catalogue answers behind the storefront edge."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.request import HTTPRedirectHandler, Request as FeedRequest, build_opener

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from price_history import PriceHistory
import max_handoff
from shop_dialog import HELP, ShopDialog, blocked, normalize
from shop_knowledge import CATALOG_LINK, MANAGER_LINK, equipment_answer, manager_answer, scope_answer
from site_catalog import STORES, store_from_data

FEED = os.environ.get("STOREFRONT_FEED_URL", "http://127.0.0.1:8094/internal/catalog-assistant")
MIME = "application/json"
STAGE = os.environ.get("STOREFRONT_ORIGIN", "http://127.0.0.1:8094")
local_key = os.environ.get("STOREFRONT_PREVIEW_KEY", "")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1","localhost"])
slots = asyncio.Semaphore(3)
snapshot_lock = threading.Lock()
snapshot_until = 0
snapshot_data = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_store():
    req = FeedRequest(FEED, headers={"Accept": MIME})
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


def answer(turns, snapshot, context=None):
    question = turns[-1]
    phrase = normalize(question)
    refusal = scope_answer(phrase) or blocked(question)
    if refusal:
        message,links = refusal
        return {"answer":message,"links":links,"discard_turn":True,"handoff_blocked":True}
    if handoff := manager_answer(phrase):
        message, links = handoff
        return {"answer": message, "links": links, "discard_turn": False, "handoff_only": True}
    if message := equipment_answer(question):
        return {"answer":message,"links":[],"discard_turn":False}
    turns = [q for q in turns[:-1] if not blocked(q) and not scope_answer(normalize(q))
             and not equipment_answer(q)] + [question]
    catalog, stamp = current_store() if snapshot is None else store_from_data(snapshot)
    history = PriceHistory() if snapshot is None else None
    dialog = ShopDialog(catalog, history)
    if context is not None:
        dialog.start_from_product_page(context)
    message, links = dialog.answer(turns)
    if message == HELP:
        links = [CATALOG_LINK, MANAGER_LINK]
    result = {"answer": message, "links": links, "as_of": stamp, "discard_turn": message == HELP}
    if dialog.display is not None:
        result["display"] = dialog.display
    if dialog.cart is not None:
        result["cart"] = dialog.cart
    return result


def unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Repeated field")
        fields[key] = value
    return fields


def page_context(body):
    if "context" not in body:
        return None
    context = body["context"]
    if not isinstance(context, dict) or context.keys() - {"product", "sku", "city"}:
        raise ValueError()
    for field in ("product", "sku"):
        if field == "sku" and field not in context:
            continue
        value = context[field]
        if type(value) is not int or not 0 < value <= 4_294_967_295:
            raise ValueError()
    if "city" in context and context["city"] not in STORES:
        raise ValueError()
    return context


def prepare_handoff(submission):
    if not isinstance(submission, dict) or submission.keys() not in (
            {"id", "question"}, {"id", "question", "context"}):
        raise ValueError()
    request_id = submission["id"]
    question = submission["question"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError()
    if not isinstance(question, str) or not 0 < len(question.strip()) <= 500:
        raise ValueError()
    question = " ".join(question.split())
    question.encode("utf-8")
    if blocked(question) or scope_answer(normalize(question)) or re.search(r"https?://|www\.", question, re.I):
        raise ValueError()
    return request_id, question, page_context(submission)


def forward_handoff(request_id, question, context):
    details = []
    hours = None
    if context is not None:
        catalog, _ = current_store()
        code = f'P{context["product"]}'
        product = catalog.products.get(code)
        if product is None:
            return "catalog_changed", {}
        details.append(f'Товар: {product["товар"]}')
        if "sku" in context:
            variant = next((catalog.rows[index] for index in catalog.by_product[code]
                            if catalog.rows[index]["id"] == str(context["sku"])), None)
            if variant is None:
                return "catalog_changed", {}
            details.append(f'Вариант: {variant["вариант"]}')
        if city := context.get("city"):
            details.append(f"Магазин: {city}")
            hours = next((c["hours"] for c in catalog.contacts if c["city"] == city), None)
    details.append(f"Вопрос: {question}")
    status, receipt = max_handoff.dispatch(request_id, details)
    result = {"receipt": receipt}
    if hours:
        result["store_hours"] = hours
    return status, result


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
    submission = None
    try:
        body = json.loads(raw, object_pairs_hook=unique_fields)
        if not local and isinstance(body, dict) and body.keys() == {"handoff_status"} and body["handoff_status"] is True:
            return JSONResponse({"available": max_handoff.configured()}, headers={"Cache-Control": "no-store"})
        if not local and isinstance(body, dict) and body.keys() == {"handoff"}:
            submission = prepare_handoff(body["handoff"])
            if not max_handoff.configured():
                raise HTTPException(503, "Автоматическая передача пока недоступна. Напишите менеджеру в MAX.")
        else:
            required = {"catalog", "turns"} if local else {"turns"}
            if not isinstance(body, dict) or body.keys() not in (required, required | {"context"}):
                raise ValueError()
            context = page_context(body)
            turns = body["turns"]
            if not isinstance(turns,list) or not 1<=len(turns)<=16 or not all(
                isinstance(q,str) and 0<len(q)<=800 and q.strip() for q in turns
            ):
                raise ValueError()
            for turn in turns:
                turn.encode("utf-8")
            snapshot = body["catalog"] if local else None
            if local and not isinstance(snapshot,dict):
                raise ValueError()
    except (ValueError,KeyError,TypeError,RecursionError):
        raise HTTPException(400,"Некорректный диалог")
    try:
        await asyncio.wait_for(slots.acquire(),timeout=1)
    except TimeoutError:
        raise HTTPException(429,"Помощник занят. Повторите через несколько секунд.", headers={"Retry-After": "2"})
    try:
        if submission is not None:
            status, result = await run_in_threadpool(forward_handoff, *submission)
            codes = {"delivered": 201, "pending": 202, "unconfirmed": 202,
                     "changed": 409, "catalog_changed": 409, "busy": 429}
            return JSONResponse({"status": status, **result}, status_code=codes[status],
                                headers={"Cache-Control": "no-store"})
        result = await run_in_threadpool(answer, turns, snapshot, context)
        if max_handoff.configured():
            result["handoff_available"] = True
        return JSONResponse(result,headers={"Cache-Control":"no-store"})
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,sqlite3.Error):
        if submission is not None:
            raise HTTPException(503, "Не удалось подтвердить передачу в MAX.")
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
