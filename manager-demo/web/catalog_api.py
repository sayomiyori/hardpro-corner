"""OpenAI-compatible catalogue interface for the two local pilot clients."""
import copy
import json
import os
import runpy
import secrets
import sys
import time
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

ROOT = Path.home() / ".local/share/hardpro"
RELEASE = ROOT / "onec-pilot/full-release"
sys.path.insert(0, str(RELEASE))
from archive_catalog import Catalog

cli = runpy.run_path(str(RELEASE / "hp-manager"))
vllm_key = next(line.partition("=")[2].strip() for line in cli["KEY_FILE"].read_text().splitlines()
                if line.startswith("VLLM_API_KEY="))


from site_catalog import read_store
from onec_live import configured_reader

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
models = {"manager": "hardpro-manager", "buyer": "hardpro-shop"}
reader_config = os.environ.get("ONEC_READER_CONFIG")
if not reader_config:
    data = json.loads((ROOT / "onec-pilot/full-catalog.json").read_text())
    archive = Catalog(data)
    archive_date = data["момент"]
keys = {role: os.environ[role.upper() + "_TOKEN"] for role in models}


def authorize(role, request):
    if role not in keys or not secrets.compare_digest(
        request.headers.get("Authorization", "").encode(), ("Bearer " + keys[role]).encode()
    ):
        raise HTTPException(401)


@app.get("/{role}/v1/models")
def model_list(role: str, request: Request):
    authorize(role, request)
    return {"object": "list", "data": [{"id": models[role], "created": 0,
                                        "object": "model", "owned_by": "hardpro"}]}


@lru_cache(maxsize=512)
def parse(system, question):
    return cli["ask"]([{"role": "system", "content": system}, {"role": "user", "content": question}],
                      vllm_key, json_output=True)


def reply(role, turns):
    if role == "buyer":
        source, separator, _ = turns[-1].partition("\n")
        parts = source.split(":")
        if not separator or len(parts) != 3 or parts[0] != "shop" or parts[1] not in {"stage","local"}:
            raise ValueError("Missing site context")
        cat, stamp = read_store(parts[1], parts[2])
        turns = [q.partition("\n")[2] for q in turns if q.startswith(f"shop:{parts[1]}:")]
        prefix = f"По данным сайта на {stamp}."
    elif reader_config:
        reader = configured_reader(reader_config)
        cat = reader.session()
        return cat.answer(turns, lambda m: parse(m[0]["content"], m[1]["content"])).replace("\n", "  \n")
    else:
        cat = copy.copy(archive)
        cat.criteria = cat.criteria.copy()
        prefix = f"По выгрузке на {archive_date}."
    for question in turns:
        answer = cat.reply(question, lambda m: parse(m[0]["content"], m[1]["content"]))
    if role == "buyer":
        answer = answer.replace("в выгрузке", "в каталоге сайта").replace("артикул или код из 1С", "артикул")
    return prefix + "\n\n" + answer.replace("\n", "  \n")


@app.post("/{role}/v1/chat/completions")
async def complete(role: str, request: Request):
    authorize(role, request)
    body = await request.json()
    if not isinstance(body, dict) or body.get("model") != models[role]:
        raise HTTPException(400, "Unknown model")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) > 125:
        raise HTTPException(400, "Начните новый диалог.")
    turns = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            value = message.get("content")
            if isinstance(value, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in value):
                value = "\n".join(p["text"] for p in value)
            if not isinstance(value, str) or not 0 < len(value.strip()) <= 1500:
                raise HTTPException(400, "Введите текст до 1500 символов.")
            turns.append(value.strip())
    if not turns:
        raise HTTPException(400, "Нет вопроса")
    try:
        answer = await run_in_threadpool(reply, role, turns)
    except (OSError, ValueError, RuntimeError, KeyError):
        raise HTTPException(503, "Поиск временно недоступен. Повторите вопрос.")
    base = dict(id="chatcmpl-" + uuid.uuid4().hex, created=int(time.time()), model=models[role])
    if not body.get("stream"):
        return {**base, "object": "chat.completion", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}
    def events():
        for delta, reason in [({"role": "assistant", "content": answer}, None), ({}, "stop")]:
            item = {**base, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")
