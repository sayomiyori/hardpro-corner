"""OpenAI-совместимый прокси, который дописывает след модели прямо в ответ.

Ставится между Open WebUI и бэкендом (llama.cpp / каталог). Каждый ответ получает
последнюю строку с реальными цифрами: время, генерация tok/s, TTFT, токены, стоп.

Один процесс = один порт = один upstream:
  TP_UPSTREAM=http://127.0.0.1:8081 TP_LABEL="Qwen3.8-Flash-Next" TP_PORT=9081
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("TP_UPSTREAM", "http://127.0.0.1:8081").rstrip("/")
LABEL = os.environ.get("TP_LABEL", "")
TAIL = os.environ.get("TP_TAIL", "1") not in ("0", "false", "no", "")
TIMEOUT = httpx.Timeout(connect=10, read=1800, write=60, pool=10)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _tail(model: str, elapsed: float, ttft: float | None, tokens: int,
          usage: dict | None, timings: dict | None, finish: str | None) -> str:
    speed = (tokens / elapsed) if tokens and elapsed > 0 else None
    head = ["⚙ **след**", f"`{LABEL or model}`", f"**{_fmt(elapsed, 2)} с**"]
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
async def healthz() -> dict:
    return {"ok": True, "upstream": UPSTREAM, "label": LABEL}


@app.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(f"{UPSTREAM}/v1/models", headers=headers)
            return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    stream = bool(body.get("stream"))
    started = time.perf_counter()
    model = str(body.get("model") or "")

    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth

    if not stream:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
            resp = await cli.post(f"{UPSTREAM}/v1/chat/completions", content=raw, headers=headers)
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            return JSONResponse(resp.json() if resp.content else {"detail": "upstream error"},
                                status_code=resp.status_code)
        data = resp.json()
        if TAIL:
            usage = data.get("usage") or {}
            timings = data.get("timings") or {}
            tokens = int(usage.get("completion_tokens") or 0)
            finish = None
            for ch in data.get("choices", []):
                finish = ch.get("finish_reason") or finish
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
        tail_sent = False
        async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
            async with cli.stream("POST", f"{UPSTREAM}/v1/chat/completions",
                                  content=raw, headers=headers) as resp:
                if resp.status_code != 200:
                    payload = (await resp.aread())[:400].decode("utf-8", "replace")
                    yield _sse({"error": {"message": payload}}).encode()
                    yield b"data: [DONE]\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        if TAIL and not tail_sent:
                            tail_sent = True
                            elapsed = time.perf_counter() - started
                            yield _sse({"choices": [{"index": 0,
                                                     "delta": {"content": _tail(
                                                         model, elapsed, ttft, tokens,
                                                         usage, timings, finish)},
                                                     "finish_reason": None}]}).encode()
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
                        piece = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
                        if piece:
                            tokens += 1
                            if ttft is None:
                                ttft = time.perf_counter() - started
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                    yield ("data: " + data + "\n\n").encode()
        if not tail_sent and TAIL:
            yield _sse({"choices": [{"index": 0,
                                     "delta": {"content": _tail(
                                         model, time.perf_counter() - started, ttft, tokens,
                                         usage, timings, finish)},
                                     "finish_reason": None}]}).encode()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def passthrough(path: str, request: Request) -> Any:
    url = f"{UPSTREAM}/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        resp = await cli.request(request.method, url, content=await request.body(),
                                 headers=headers, params=request.query_params)
    return JSONResponse(resp.json() if resp.headers.get("content-type", "").startswith("application/json")
                        else {"ok": resp.status_code},
                        status_code=resp.status_code)
