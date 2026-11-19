"""Buyer-only service. Stage data crosses a dedicated loopback SSH forward."""
import asyncio
import json
import runpy
import secrets
import sys
from functools import lru_cache
from pathlib import Path
from urllib.request import Request as FeedRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path.home() / ".local/share/hardpro"
RELEASE = ROOT / "onec-pilot/full-release"
sys.path.insert(0, str(RELEASE))
from site_catalog import store_from_data

cli = runpy.run_path(str(RELEASE / "hp-manager"))
model_key = next(s.partition("=")[2].strip() for s in cli["KEY_FILE"].read_text().splitlines()
                 if s.startswith("VLLM_API_KEY="))
local_key = (ROOT / "catalog-web/relay.secret").read_text().strip()
stage_origin = "https://hardpro.test.mp-bridge.ru"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1","localhost"])
slots = asyncio.Semaphore(3)


@lru_cache(maxsize=384)
def parse(system, question):
    return cli["ask"]([{"role":"system","content":system},{"role":"user","content":question}],
                      model_key, json_output=True)


def answer(turns, snapshot):
    if snapshot is None:
        req = FeedRequest("http://127.0.0.1:18865/internal/catalog-assistant",
                          headers={"Host":"127.0.0.1:18094","Accept":"application/json"})
        with urlopen(req, timeout=30) as resp:
            if resp.headers.get_content_type() != "application/json":
                raise ValueError("Invalid storefront response")
            raw = resp.read(8_000_001)
            if len(raw)>8_000_000:
                raise ValueError("Catalog exceeds limit")
            snapshot = json.loads(raw)
    cat, stamp = store_from_data(snapshot)
    for q in turns:
        reply = cat.reply(q,lambda m:parse(m[0]["content"],m[1]["content"]))
    return {"answer":reply.replace("в выгрузке","в каталоге сайта").replace("артикул или код из 1С","артикул"),
            "as_of":stamp}


@app.post("/ask")
async def ask(request: Request):
    auth = request.headers.get("authorization","")
    local = secrets.compare_digest(auth.encode(),("Bearer "+local_key).encode())
    if not local and (auth or request.headers.get("origin")!=stage_origin):
        raise HTTPException(403)
    if request.headers.get("content-type","").split(";")[0]!="application/json":
        raise HTTPException(415)
    limit = 8_000_000 if local else 24000
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>limit:
            raise HTTPException(413)
        raw.extend(chunk)
    try:
        body = json.loads(raw)
        turns = body["turns"]
        if not isinstance(turns,list) or not 1<=len(turns)<=16 or not all(
            isinstance(q,str) and 0<len(q.strip())<=800 for q in turns
        ):
            raise ValueError()
        snapshot = body["catalog"] if local else None
        if local and not isinstance(snapshot,dict):
            raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise HTTPException(400,"Некорректный диалог")
    try:
        await asyncio.wait_for(slots.acquire(),timeout=3)
    except TimeoutError:
        raise HTTPException(429,"Все места заняты. Повторите через несколько секунд.")
    try:
        result=await run_in_threadpool(answer,turns,snapshot)
        return JSONResponse(result,headers={"Cache-Control":"no-store"})
    except (ValueError,KeyError,TypeError,OSError,RuntimeError):
        raise HTTPException(503,"Сейчас не удаётся сверить каталог. Попробуйте ещё раз.")
    finally:
        slots.release()
