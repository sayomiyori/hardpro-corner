"""OpenAI-compatible catalogue interface for the two local pilot clients."""
import copy
import json
import logging
import os
import runpy
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("hardpro.catalog")

ROOT = Path.home() / ".local/share/hardpro"
RELEASE = ROOT / "onec-pilot/full-release"
SNAPSHOT = ROOT / "onec-pilot/full-catalog.json"
LIVE_SNAPSHOT = ROOT / "onec-pilot/live-snapshot.json"
MSK = timezone(timedelta(hours=3))
sys.path.insert(0, str(RELEASE))
from archive_catalog import Catalog

cli = runpy.run_path(str(RELEASE / "hp-manager"))
vllm_key = next(line.partition("=")[2].strip() for line in cli["KEY_FILE"].read_text().splitlines()
                if line.startswith("VLLM_API_KEY="))


from site_catalog import read_store
from onec_live import configured_reader
from manager_knowledge import competition_answer, definition_answer, merge_clarification, resolve_context

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
models = {"manager": "hardpro-manager", "buyer": "hardpro-shop"}
reader_config = os.environ.get("ONEC_READER_CONFIG")
keys = {role: os.environ[role.upper() + "_TOKEN"] for role in models}

_snapshot = {"path": None, "mtime": 0.0, "stamp": "", "data": None}
_saved_at = 0.0
_link_down_until = 0.0
_link_lock = threading.Lock()


def snapshot_source():
    """Свежий снимок из рабочей 1С, если он есть, иначе сохранённая выгрузка."""
    for path in (LIVE_SNAPSHOT, SNAPSHOT):
        try:
            return path, path.stat().st_mtime
        except OSError:
            continue
    raise OSError("нет сохранённой выгрузки каталога")


def snapshot():
    path, mtime = snapshot_source()
    if _snapshot["path"] != path or _snapshot["mtime"] != mtime:
        data = json.loads(path.read_text(encoding="utf-8"))
        _snapshot.update(path=path, mtime=mtime, stamp=data["момент"], data=data)
    return _snapshot["stamp"], _snapshot["data"]


def snapshot_catalog():
    stamp, data = snapshot()
    return Catalog(data), stamp


def save_live(cat):
    """Удачное чтение 1С переживает её отключение и перезапуск сервера."""
    global _saved_at
    if time.monotonic() - _saved_at < 300:
        return
    data = {"момент": datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК"),
            "склады": list(cat.stores), "товары": cat.rows}
    tmp = LIVE_SNAPSHOT.with_name(LIVE_SNAPSHOT.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, LIVE_SNAPSHOT)
    _saved_at = time.monotonic()
    log.warning("сохранён свежий снимок каталога: %d строк", len(cat.rows))



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


def live_catalog(turns):
    """Ответ по рабочей 1С. None означает, что канал не отвечает и нужен резерв."""
    global _link_down_until
    if time.monotonic() < _link_down_until:
        return None
    try:
        cat = configured_reader(reader_config).session()
    except OSError as exc:
        with _link_lock:
            _link_down_until = time.monotonic() + 120
        log.warning("канал рабочей 1С не отвечает, отвечаю по выгрузке: %s", exc, exc_info=True)
        return None
    try:
        save_live(cat)
    except OSError as exc:
        log.warning("снимок каталога не сохранён: %s", exc)
    try:
        return cat.answer(turns, lambda m: parse(m[0]["content"], m[1]["content"])).replace("\n", "  \n")
    except OSError as exc:
        with _link_lock:
            _link_down_until = time.monotonic() + 120
        log.warning("канал рабочей 1С оборвался во время ответа: %s", exc, exc_info=True)
        return None


def reply(role, turns):
    if role == "manager":
        turns = merge_clarification(turns)
        definition = definition_answer(turns[-1])
        if definition:
            return definition.replace("\n", "  \n")
        _, clarification = resolve_context(turns[-1])
        if clarification:
            return clarification
        competition = competition_answer(turns[-1], turns[:-1])
        if competition:
            return competition.replace("\n", "  \n")
    if role == "buyer":
        source, separator, _ = turns[-1].partition("\n")
        parts = source.split(":")
        if not separator or len(parts) != 3 or parts[0] != "shop" or parts[1] not in {"stage","local"}:
            raise ValueError("Missing site context")
        cat, stamp = read_store(parts[1], parts[2])
        turns = [q.partition("\n")[2] for q in turns if q.startswith(f"shop:{parts[1]}:")]
        prefix = f"По данным сайта на {stamp}."
    elif reader_config:
        answer = live_catalog(turns)
        if answer is not None:
            return answer
        cat, stamp = snapshot_catalog()
        prefix = (f"Рабочая 1С сейчас не отвечает, отвечаю по последней выгрузке на {stamp}. "
                  "Свежее наличие и брони не подтверждены.")
    else:
        cat, stamp = snapshot_catalog()
        prefix = f"По выгрузке на {stamp}."
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
    except OSError as exc:
        log.warning("источник каталога недоступен: %s", exc, exc_info=True)
        raise HTTPException(503, "Источник каталога недоступен. Повторите вопрос.")
    except (ValueError, RuntimeError, KeyError) as exc:
        log.warning("вопрос не разобран: %s", exc, exc_info=True)
        raise HTTPException(503, "Не удалось разобрать вопрос. Переформулируйте его.")
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
