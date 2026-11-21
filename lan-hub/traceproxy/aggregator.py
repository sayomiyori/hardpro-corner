"""Единый OpenAI-прокси с хвостом-следом: под все модели, включая новые.

Один порт (по умолчанию 9090) притворяется OpenAI-эндпоинтом:
- GET  /v1/models            — собирает модели со всех бэкендов
- POST /v1/chat/completions  — маршрутизирует по имени модели и дописывает хвост

Маршруты обновляются сами: каждые 60 с (и при промахе) прокси перечитывает
/v1/models у всех бэкендов, поэтому модель, добавленная в llama.cpp позже,
попадает под хвост без правок конфигов.

ENV:
  TA_UPSTREAMS      список через ';' вида url|токен  (токен можно опустить)
  TA_MANAGER_TOKEN  токен каталога 1С
  TA_AGENT_TOKEN    пароль агента (Bearer)
  TA_LABEL_MAP      JSON {id: "красивое имя"}
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TIMEOUT = httpx.Timeout(connect=10, read=3600, write=60, pool=10)
MANAGER_TOKEN = os.environ.get("TA_MANAGER_TOKEN", "")
AGENT_TOKEN = os.environ.get("TA_AGENT_TOKEN", "")

DEFAULT_UPSTREAMS = ";".join([
    "http://127.0.0.1:8081",
    "http://127.0.0.1:8083",
    f"http://127.0.0.1:8767/manager|{MANAGER_TOKEN}",
    f"http://127.0.0.1:9444|{AGENT_TOKEN}",
])
RAW_UPSTREAMS = os.environ.get("TA_UPSTREAMS", DEFAULT_UPSTREAMS)
UPSTREAMS: list[dict[str, str]] = []
for chunk in RAW_UPSTREAMS.split(";"):
    chunk = chunk.strip()
    if not chunk:
        continue
    base, _, token = chunk.partition("|")
    UPSTREAMS.append({"base": base.rstrip("/"), "token": token.strip()})

LABELS: dict[str, str] = {}
try:
    LABELS = json.loads(os.environ.get("TA_LABEL_MAP", "{}"))
except ValueError:
    LABELS = {}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_registry: dict[str, dict[str, str]] = {}
_registry_at = 0.0
_skip_tail = os.environ.get("TA_SKIP_TAIL", "hardpro-agent").split(",")
_skip_tail = {s.strip() for s in _skip_tail if s.strip()}


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _headers(up: dict[str, str]) -> dict[str, str]:
    out = {"Content-Type": "application/json"}
    if up.get("token"):
        out["Authorization"] = f"Bearer {up['token']}"
    return out


async def refresh_registry(force: bool = False) -> dict[str, dict[str, str]]:
    global _registry, _registry_at
    if not force and _registry and (time.time() - _registry_at) < 60:
        return _registry
    found: dict[str, dict[str, str]] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(8)) as cli:
        for up in UPSTREAMS:
            try:
                r = await cli.get(f"{up['base']}/v1/models", headers=_headers(up))
                if r.status_code != 200:
                    continue
                for item in r.json().get("data", []) or []:
                    mid = item.get("id") or item.get("name")
                    if mid:
                        found[str(mid)] = up
            except (httpx.HTTPError, ValueError):
                continue
    if found:
        _registry = found
        _registry_at = time.time()
    return _registry


def pick(model: str) -> dict[str, str]:
    if model in _registry:
        return _registry[model]
    for up in UPSTREAMS:
        if "8081" in up["base"]:
            return up
    return UPSTREAMS[0]


def _tail(model: str, elapsed: float, ttft: float | None, tokens: int,
          usage: dict | None, timings: dict | None, finish: str | None) -> str:
    label = LABELS.get(model, model)
    speed = (tokens / elapsed) if tokens and elapsed > 0 else None
    head = ["⚙ **след**", f"`{label}`", f"**{_fmt(elapsed, 2)} с**"]
    if speed:
        head.append(f"**{_fmt(speed)} tok/s**")
    if tokens:
        head.append(f"{tokens} ток")
    if ttft:
        head.append(f"TTFT {_fmt(ttft * 1000, 0)} мс")
    parts: list[str] = []
    usage = usage or {}
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        parts += [f"prompt {usage.get('prompt_tokens', '—')}",
                  f"completion {usage.get('completion_tokens', '—')}",
                  f"total {usage.get('total_tokens', '—')}"]
    timings = timings or {}
    if timings.get("prompt_per_second"):
        parts.append(f"prompt {_fmt(timings['prompt_per_second'])} tok/s")
    if timings.get("predicted_per_second"):
        parts.append(f"генерация {_fmt(timings['predicted_per_second'])} tok/s")
    if finish:
        parts.append(f"стоп `{finish}`")
    out = "\n\n---\n" + " · ".join(head)
    if parts:
        out += "\n\n" + " · ".join(parts)
    return out + "\n"


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.get("/healthz")
async def healthz(force: bool = False) -> dict:
    reg = await refresh_registry(force=force)
    return {"ok": True, "models": len(reg),
            "upstreams": [u["base"] for u in UPSTREAMS]}


@app.get("/v1/models")
async def models() -> JSONResponse:
    reg = await refresh_registry(force=True)
    data = []
    for mid, up in sorted(reg.items()):
        data.append({"id": mid, "object": "model", "created": 0,
                     "owned_by": "hardpro", "name": LABELS.get(mid, mid),
                     "backend": up["base"]})
    if not data:
        return JSONResponse({"object": "list", "data": []})
    return JSONResponse({"object": "list", "data": data})


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    model = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    await refresh_registry()
    up = pick(model)
    started = time.perf_counter()
    tail_on = model not in _skip_tail

    if not stream:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
            resp = await cli.post(f"{up['base']}/v1/chat/completions", content=raw,
                                  headers=_headers(up))
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            return JSONResponse(resp.json() if resp.content else {"detail": "upstream error"},
                                status_code=resp.status_code)
        data = resp.json()
        if tail_on:
            usage = data.get("usage") or {}
            timings = data.get("timings") or {}
            tokens = int(usage.get("completion_tokens") or 0)
            for ch in data.get("choices", []):
                finish = ch.get("finish_reason")
                msg = ch.get("message") or {}
                msg["content"] = (msg.get("content") or "") + _tail(
                    model, elapsed, None, tokens, usage, timings, finish)
        return JSONResponse(data)

    async def relay() -> AsyncIterator[bytes]:
        ttft: float | None = None
        tokens = 0
        usage: dict = {}
        timings: dict = {}
        finish: str | None = None
        sent = False
        async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
            async with cli.stream("POST", f"{up['base']}/v1/chat/completions", content=raw,
                                  headers=_headers(up)) as resp:
                if resp.status_code != 200:
                    payload = (await resp.aread())[:400].decode("utf-8", "replace")
                    yield _sse({"error": {"message": payload}}).encode()
                    yield b"data: [DONE]\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        if tail_on and not sent:
                            sent = True
                            yield _sse({"choices": [{"index": 0, "finish_reason": None,
                                "delta": {"content": _tail(model, time.perf_counter() - started,
                                                           ttft, tokens, usage, timings, finish)}}]}
                            ).encode()
                        yield b"data: [DONE]\n\n"
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        yield ("data: " + data + "\n\n").encode()
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("timings"):
                        timings = obj["timings"]
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta") or {}
                        if (delta.get("content") or "") or (delta.get("reasoning_content") or ""):
                            tokens += 1
                            if ttft is None:
                                ttft = time.perf_counter() - started
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                    yield ("data: " + data + "\n\n").encode()
        if tail_on and not sent:
            yield _sse({"choices": [{"index": 0, "finish_reason": None,
                "delta": {"content": _tail(model, time.perf_counter() - started, ttft, tokens,
                                           usage, timings, finish)}}]}).encode()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def passthrough(path: str, request: Request) -> Any:
    model = ""
    try:
        payload = json.loads(await request.body() or b"{}")
        model = str(payload.get("model") or "")
    except (ValueError, TypeError):
        pass
    await refresh_registry()
    up = pick(model) if model else UPSTREAMS[0]
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        resp = await cli.request(request.method, f"{up['base']}/{path}",
                                 content=await request.body(), headers=headers,
                                 params=request.query_params)
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return JSONResponse(resp.json(), status_code=resp.status_code)
    return JSONResponse({"ok": resp.status_code}, status_code=resp.status_code)


@app.on_event("startup")
async def _warm() -> None:
    asyncio.create_task(_loop())


async def _loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await refresh_registry(force=True)
        except Exception:
            pass
