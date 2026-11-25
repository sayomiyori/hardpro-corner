"""OpenAI-compatible catalogue interface for the two local pilot clients."""
import copy
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
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
sys.path.append(str(RELEASE))
from assistant_dialog import catalog_turns, decision, health_question, model_json, with_intro
from manager_catalog import TOPIC_RESET, ManagerCatalog
import manager_reports


from site_catalog import read_store
from shop_dialog import ShopDialog, blocked
from onec_live import KNOWLEDGE, configured_reader, site_price_only
from manager_knowledge import competition_answer, definition_answer, expand_terms, merge_clarification, resolve_context

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
models = {"manager": "hardpro-manager", "buyer": "hardpro-shop"}
reader_config = os.environ.get("ONEC_READER_CONFIG")
keys = {role: os.environ[role.upper() + "_TOKEN"] for role in models}

_snapshot = {"path": None, "mtime": 0.0, "stamp": "", "data": None}
_saved_at = None
_snapshot_lock = threading.Lock()
_link_down_until = 0.0
_link_lock = threading.Lock()
LINK_RETRY_AFTER = 5


def snapshot_source():
    """Свежий снимок из рабочей 1С, если он есть, иначе сохранённая выгрузка."""
    for path in (KNOWLEDGE, LIVE_SNAPSHOT, SNAPSHOT):
        try:
            return path, path.stat().st_mtime
        except OSError:
            continue
    raise OSError("нет сохранённой выгрузки каталога")


def snapshot():
    path, mtime = snapshot_source()
    if _snapshot["path"] != path or _snapshot["mtime"] != mtime:
        data = json.loads(path.read_text(encoding="utf-8"))
        if path == KNOWLEDGE:
            catalog, offers = data["catalog"], data["offers_by_product"]
            rows = []
            for row in catalog["товары"]:
                free = offers[row["product"]]["variants"][row["id"]]["free"]
                rows.append({**row, "склады": {name: {"свободно": quantity}
                                               for name, quantity in free.items()}})
            data = {**catalog, "товары": rows}
        _snapshot.update(path=path, mtime=mtime, stamp=data["момент"], data=data)
    return _snapshot["stamp"], _snapshot["data"]


def snapshot_catalog():
    stamp, data = snapshot()
    return ManagerCatalog(data), stamp


def save_live(cat):
    """Удачное чтение 1С переживает её отключение и перезапуск сервера."""
    global _saved_at
    if _saved_at is not None and cat.indexed_at <= _saved_at:
        return
    with _snapshot_lock:
        if _saved_at is not None and cat.indexed_at <= _saved_at:
            return
        data = {"момент": cat.indexed_at.strftime("%d.%m.%Y %H:%M МСК"),
                "склады": list(cat.stores), "товары": cat.rows}
        tmp = LIVE_SNAPSHOT.with_name(LIVE_SNAPSHOT.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, LIVE_SNAPSHOT)
        _saved_at = cat.indexed_at
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


parse = model_json


def live_catalog(turns):
    """Ответ по рабочей 1С. None означает, что канал не отвечает и нужен резерв."""
    global _link_down_until
    if time.monotonic() < _link_down_until:
        return None
    try:
        cat = configured_reader(reader_config).session()
    except (OSError, ValueError, KeyError) as exc:
        with _link_lock:
            _link_down_until = time.monotonic() + LINK_RETRY_AFTER
        log.warning("канал рабочей 1С не отвечает, отвечаю по выгрузке: %s", exc, exc_info=True)
        return None
    try:
        save_live(cat)
    except OSError as exc:
        log.warning("снимок каталога не сохранён: %s", exc)
    try:
        return cat.answer(turns, lambda m: parse(m[0]["content"], m[1]["content"])).replace("\n", "  \n")
    except (OSError, ValueError, KeyError) as exc:
        with _link_lock:
            _link_down_until = time.monotonic() + LINK_RETRY_AFTER
        log.warning("канал рабочей 1С оборвался во время ответа: %s", exc, exc_info=True)
        return None


def onec_status():
    global _link_down_until
    if not reader_config:
        return "Для этого чата связь с рабочей 1С не настроена."
    try:
        configured_reader(reader_config).check()
    except (OSError, ValueError, KeyError) as exc:
        log.warning("контрольный запрос к читателю 1С завершился ошибкой: %s", type(exc).__name__)
        return "Проверил подключение. Читатель 1С не подтвердил чтение каталога."
    with _link_lock:
        _link_down_until = 0.0
    return f"Связь с рабочей 1С есть. Пробное чтение каталога прошло в {datetime.now(MSK):%H:%M:%S} МСК."


def reply(role, turns, context=()):
    if role == "manager":
        turns = [TOPIC_RESET.sub("", turn, count=1) or turn for turn in turns]
        question = turns[-1]
        if health_question(question):
            return onec_status()
        if reader_config and (business_answer := manager_reports.reply(
                question, parse, lambda: configured_reader(reader_config))) is not None:
            return business_answer
        definition = definition_answer(turns[-1])
        if definition:
            return definition.replace("\n", "  \n")
        _, clarification = resolve_context(turns[-1])
        if clarification:
            return clarification
        competition = competition_answer(turns[-1], turns[:-1])
        if competition:
            return competition.replace("\n", "  \n")
        action, message = decision(turns, parse, context=context)
        if action == "chat":
            return message
        if site_price_only(expand_terms(question)):
            return "Цены сайта по этим вариантам пока недоступны."
        turns = merge_clarification(catalog_turns(turns, parse) if action == "followup" else [question])
        if reader_config:
            answer = live_catalog(turns)
            if answer is not None:
                return answer
        cat, stamp = snapshot_catalog()
        answer = cat.archive_answer(turns, lambda m: parse(m[0]["content"], m[1]["content"]))
        prefix = f"По выгрузке 1С на {stamp}. Остатки архивные. Актуальные цены не подтверждены."
        if reader_config:
            prefix = "Чат сейчас не получил ответ от читателя 1С. " + prefix
        return prefix + "\n\n" + answer.replace("\n", "  \n")
    if role == "buyer":
        source, separator, _ = turns[-1].partition("\n")
        parts = source.split(":")
        if not separator or len(parts) != 3 or parts[0] != "shop" or parts[1] not in {"stage","local"}:
            raise ValueError("Missing site context")
        cat, stamp = read_store(parts[1], parts[2])
        turns = [q.partition("\n")[2] for q in turns if q.startswith(f"shop:{parts[1]}:")]
        if health_question(turns[-1]):
            return "В этом чате я могу проверить товары, цены и наличие по каталогу магазина."
        turns = [q for q in turns[:-1] if not blocked(q)] + turns[-1:]
        action, message = decision(turns, parse, buyer=True)
        if action == "chat":
            return message
        turns = catalog_turns(turns, parse, buyer=True) if action == "followup" else turns[-1:]
        answer, _ = ShopDialog(cat).answer(turns)
        return with_intro(turns[-1], f"По данным сайта на {stamp}.\n\n" + answer.replace("\n", "  \n"), parse)
    raise ValueError("Unknown role")


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
    context = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            value = message.get("content")
            if isinstance(value, list) and all(isinstance(p, dict) and p.get("type") == "text"
                                               and isinstance(p.get("text"), str) for p in value):
                value = "\n".join(p["text"] for p in value)
            if not isinstance(value, str) or not 0 < len(value.strip()) <= 1500:
                raise HTTPException(400, "Введите текст до 1500 символов.")
            turns.append(value.strip())
            context.append({"вопрос": value.strip()})
        elif isinstance(message, dict) and message.get("role") == "assistant":
            value = message.get("content")
            if isinstance(value, str) and value.strip():
                context.append({"предыдущий ответ": value.strip()[:1200]})
    if not turns:
        raise HTTPException(400, "Нет вопроса")
    try:
        answer = await run_in_threadpool(reply, role, turns, context[-5:])
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
