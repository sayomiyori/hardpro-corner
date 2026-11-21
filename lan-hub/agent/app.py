"""HTTP front for the agent daemon: sessions, SSE chat, tool trace."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import core, store

try:
    from models import api as models_api
except ImportError:  # каталог моделей не установлен — живём на env
    models_api = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
HUB_USER = os.environ.get("HUB_USER", "hardpro")
HUB_PASS = os.environ.get("HUB_PASS", "")


def default_model() -> str:
    return core.default_model()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic(auto_error=False)


def auth(cred: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not HUB_PASS:
        raise HTTPException(503, "пароль не задан")
    if cred is None or not (hmac.compare_digest(cred.username.encode(), HUB_USER.encode())
                            and hmac.compare_digest(cred.password.encode(), HUB_PASS.encode())):
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})
    return cred.username


class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    model: str | None = None


def auth_openai(request: Request) -> str:
    """Bearer для Open WebUI-подключения, Basic — для браузера."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if HUB_PASS and hmac.compare_digest(token.encode(), HUB_PASS.encode()):
            return "openwebui"
        raise HTTPException(401, "bad token")
    basic = request.headers.get("authorization", "")
    if basic.lower().startswith("basic "):
        try:
            raw = base64.b64decode(basic[6:]).decode("utf-8", "replace")
            user, _, password = raw.partition(":")
        except (ValueError, TypeError):
            raise HTTPException(401, "bad basic") from None
        if (hmac.compare_digest(user.encode(), HUB_USER.encode())
                and hmac.compare_digest(password.encode(), HUB_PASS.encode())):
            return user
    raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})


AGENT_MODEL_ID = os.environ.get("HP_AGENT_MODEL_ID", "hardpro-agent")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": core.default_model()}


@app.get("/v1/models")
async def v1_models(request: Request) -> JSONResponse:
    auth_openai(request)
    return JSONResponse({"object": "list", "data": [{
        "id": AGENT_MODEL_ID, "object": "model", "created": 0,
        "owned_by": "hardpro", "name": "HARD-PRO Агент"}]})


@app.post("/v1/chat/completions")
async def v1_chat(request: Request) -> StreamingResponse:
    auth_openai(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json") from None
    messages = body.get("messages") or []
    history: list[dict] = []
    text = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            continue
        if role in ("user", "assistant"):
            if role == "user":
                text = content
                history.append({"role": "user", "content": content})
            else:
                history.append({"role": "assistant", "content": content})
    history = history[:-1] if history and history[-1].get("role") == "user" else history
    if not text.strip():
        raise HTTPException(400, "нет сообщения пользователя")

    sid = store.new_session(default_model())
    stream = bool(body.get("stream"))
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def worker() -> None:
        try:
            await core.run_turn(sid, text, emit, default_model(), history)
        except Exception as exc:
            await queue.put({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(None)

    asyncio.create_task(worker())
    created = int(time.time())
    cid = "chatcmpl-" + sid

    if not stream:
        final = ""
        while True:
            item = await queue.get()
            if item is None:
                break
            if item["type"] in ("assistant", "done"):
                final = item.get("text") or final
            elif item["type"] == "error":
                final = final or f"ошибка: {item.get('text')}"
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created,
                             "model": AGENT_MODEL_ID,
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": final}}]})

    async def events():
        def chunk(delta: dict, finish: str | None = None) -> str:
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": AGENT_MODEL_ID,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        yield chunk({"role": "assistant", "content": ""})
        while True:
            item = await queue.get()
            if item is None:
                break
            kind = item["type"]
            if kind == "tool":
                note = f"\n⚙ {item['name']}: {json.dumps(item['args'], ensure_ascii=False)[:300]}\n"
                yield chunk({"content": note})
            elif kind == "assistant":
                yield chunk({"content": item["text"]})
            elif kind == "error":
                yield chunk({"content": f"\nошибка: {item['text']}"})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(auth)) -> HTMLResponse:
    return HTMLResponse((HERE / "ui.html").read_text(encoding="utf-8"))


@app.get("/api/mcp")
async def api_mcp(_: str = Depends(auth)) -> JSONResponse:
    info = await core.init_mcp()
    names = sorted(n for n in core.tools.REGISTRY if n.startswith("mcp__"))
    return JSONResponse({**info, "tools": names})


@app.get("/api/sessions")
async def api_sessions(_: str = Depends(auth)) -> JSONResponse:
    return JSONResponse({"sessions": store.sessions()})


@app.post("/api/sessions")
async def api_new(_: str = Depends(auth), model: str = "") -> JSONResponse:
    return JSONResponse({"id": store.new_session(model or default_model())})


@app.delete("/api/sessions/{sid}")
async def api_drop(sid: str, _: str = Depends(auth)) -> JSONResponse:
    store.drop(sid)
    return JSONResponse({"ok": True})


@app.get("/api/sessions/{sid}/messages")
async def api_msgs(sid: str, _: str = Depends(auth)) -> JSONResponse:
    return JSONResponse({"messages": store.messages(sid)})


@app.post("/api/chat")
async def api_chat(body: ChatIn, _: str = Depends(auth)) -> StreamingResponse:
    sid = body.session_id or store.new_session(body.model or default_model())
    if not body.text.strip():
        raise HTTPException(400, "пустой запрос")
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def worker() -> None:
        try:
            await core.run_turn(sid, body.text, emit, body.model)
        except Exception as exc:  # daemon must stay up
            await queue.put({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(worker())

    async def events():
        yield "data: " + json.dumps({"type": "session", "id": sid}, ensure_ascii=False) + "\n\n"
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        finally:
            if not task.done():
                task.cancel()
        yield "data: " + json.dumps({"type": "eof"}) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


if models_api is not None:
    app.include_router(models_api.router, dependencies=[Depends(auth)])
