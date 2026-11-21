"""Agent loop: local model + tools, streaming to the UI, full server access."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from . import mcp as mcp_mod
from . import skills as skills_mod
from . import store
from . import tools

try:  # каталог моделей лежит в корне hardpro, пакет — отдельный
    from models import registry as model_registry
except ImportError:  # без каталога агент живёт на HP_MODEL_BASE/HP_MODEL_NAME
    model_registry = None  # type: ignore[assignment]

ROOT = Path(os.environ.get("HP_ROOT", Path.home() / "hardpro"))
WORKSPACE = Path(os.environ.get("HP_WORKSPACE", ROOT / "workspace"))
AGENTS_MD = WORKSPACE / "AGENTS.md"
MODEL_BASE = os.environ.get("HP_MODEL_BASE", "http://127.0.0.1:8081")
MODEL_NAME = os.environ.get("HP_MODEL_NAME", "Qwen3.8-Flash-Next")
MAX_STEPS = int(os.environ.get("HP_MAX_STEPS", "24"))
MAX_TOKENS = int(os.environ.get("HP_MAX_TOKENS", "3072"))
TEMPERATURE = float(os.environ.get("HP_TEMPERATURE", "0.6"))
MCP_CONFIG = ROOT / "agent" / "mcp.json"

Emit = Callable[[dict[str, Any]], Awaitable[None]]

_mcp_servers: list[mcp_mod.McpServer] = []
_mcp_ready = False


def default_model() -> str:
    """Модель роли `agent` из каталога, иначе — что в env."""
    if model_registry is not None:
        try:
            bound = model_registry.model_for_role("agent")
            if bound:
                return bound
        except model_registry.RegistryError:
            pass
    return MODEL_NAME


def endpoint(model: str | None) -> tuple[str, str, str]:
    """(base, имя модели, токен) для одного круга. Каталог главнее env."""
    name = (model or "").strip() or default_model()
    if model_registry is None:
        return MODEL_BASE, name, ""
    try:
        return model_registry.base_for(name), name, model_registry.token_for(name)
    except model_registry.RegistryError:
        return MODEL_BASE, name, ""


def _system_prompt() -> str:
    parts = [
        "Ты — инженерный агент HARD-PRO на локальном сервере hardpro-llm.",
        f"Рабочий каталог: {WORKSPACE}. У тебя полный доступ к машине через shell, "
        "файловые инструменты, веб и MCP-серверы. Команды исполняются на сервере.",
        "Работай итеративно: сначала осмотрись (list_dir, read_file, shell), затем правь файлы, "
        "затем проверяй результат командой. Не выдумывай содержимое файлов — читай.",
        "Никогда не заканчивай работу, не проверив её: запусти сборку/тесты/сервис командой.",
        f"Сегодня {datetime.now().strftime('%Y-%m-%d')}.",
    ]
    if AGENTS_MD.is_file():
        try:
            body = AGENTS_MD.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            body = ""
        if body:
            parts.append("Контекст проекта (AGENTS.md):\n" + body)
    block = skills_mod.prompt_block()
    if block:
        parts.append(block)
    return "\n\n".join(parts)


async def init_mcp() -> dict[str, Any]:
    """Start stdio MCP servers once, expose their tools as mcp__<server>__<tool>."""
    global _mcp_ready
    if _mcp_ready:
        names = sorted(n for n in tools.REGISTRY if n.startswith("mcp__"))
        return {"servers": [s.name for s in _mcp_servers], "tools": len(names), "errors": []}
    _mcp_ready = True
    servers = mcp_mod.load_servers(MCP_CONFIG)
    registered: dict[str, Callable[..., dict]] = {}
    errors: list[str] = []
    for srv in servers:
        try:
            listed = await asyncio.to_thread(srv.initialize)
        except (mcp_mod.McpError, OSError) as exc:
            errors.append(f"{srv.name}: {exc}")
            continue
        skip = set(srv.name and [t for t in (srv.env.get("HP_SKIP") or "").split(",") if t] or [])
        for tool in listed:
            tname = tool.get("name")
            if not tname or tname in skip:
                continue
            full = f"mcp__{srv.name}__{tname}"
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}

            def _call(_srv=srv, _name=tname, **kwargs):
                return _srv.call(_name, kwargs)

            registered[full] = _call
            tools.SCHEMA.append({"type": "function", "function": {
                "name": full,
                "description": (tool.get("description") or "")[:500],
                "parameters": schema}})
        _mcp_servers.append(srv)
    tools.register_mcp(registered)
    return {"servers": [s.name for s in _mcp_servers], "tools": len(registered), "errors": errors}


def shutdown_mcp() -> None:
    for srv in _mcp_servers:
        srv.stop()


async def _chat_stream(messages: list[dict], schema: list[dict],
                       base: str, name: str, token: str = "") -> tuple[str, str, list[dict], dict]:
    """One model round. Returns (content, reasoning, tool_calls, usage)."""
    body: dict[str, Any] = {
        "model": name,
        "messages": messages,
        "stream": True,
        "temperature": TEMPERATURE,
        "top_p": 0.95,
        "max_tokens": MAX_TOKENS,
        "cache_prompt": True,
    }
    if schema:
        body["tools"] = schema
        body["tool_choice"] = "auto"
    content: list[str] = []
    reasoning: list[str] = []
    calls: list[dict] = []
    usage: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=900)) as cli:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with cli.stream("POST", f"{base}/v1/chat/completions", json=body,
                              headers=headers) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:400].decode("utf-8", "replace")
                raise RuntimeError(f"model HTTP {resp.status_code}: {raw}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices", []):
                    delta = ch.get("delta") or {}
                    piece = delta.get("content") or ""
                    think = delta.get("reasoning_content") or ""
                    if think:
                        reasoning.append(think)
                    if piece:
                        content.append(piece)
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        while len(calls) <= idx:
                            calls.append({"id": "", "type": "function",
                                          "function": {"name": "", "arguments": ""}})
                        slot = calls[idx]
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
    return "".join(content), "".join(reasoning), calls, usage


AUTO_START = os.environ.get("HP_AUTO_START", "1") not in ("0", "false", "no")


async def _ensure_model_ready(name: str) -> str | None:
    """Если профиль каталога не поднят — поднять. Возвращает заметку или ошибку."""
    if model_registry is None or not AUTO_START:
        return None
    try:
        if model_registry.online(name):
            return None
        prof = model_registry.profiles().get(name) or {}
        if prof.get("backend") != "llamacpp" or not prof.get("autostart"):
            return None
        await asyncio.to_thread(model_registry.launch, name)
        return f"{name}: поднята на GPU {','.join(str(g) for g in prof.get('gpus') or [])}"
    except model_registry.RegistryError as exc:
        return f"{name}: не поднялась — {exc}"


async def run_turn(session_id: str, text: str, emit: Emit, model: str | None = None,
                   history: list[dict] | None = None) -> None:
    base, model_name, token = endpoint(model)
    note = await _ensure_model_ready(model_name)
    if note:
        await emit({"type": "note", "text": note})
    skills_mod.ensure()
    tools.register_skills(skills_mod)
    try:
        await init_mcp()
    except (OSError, mcp_mod.McpError) as exc:
        await emit({"type": "note", "text": f"MCP недоступен: {exc}"})

    store.append(session_id, "user", text)
    store.touch(session_id, text.strip().splitlines()[0][:90] if text.strip() else None)

    messages: list[dict] = [{"role": "system", "content": _system_prompt()}]
    if history:
        messages.extend(history)
    else:
        for item in store.history(session_id):
            messages.append(item)
    messages.append({"role": "user", "content": text})

    schema = tools.schemas()
    started = time.perf_counter()
    for step in range(MAX_STEPS):
        await emit({"type": "status", "text": f"шаг {step + 1}: {model_name} думает"})
        try:
            content, reasoning, calls, usage = await _chat_stream(messages, schema,
                                                                  base, model_name, token)
        except (httpx.HTTPError, RuntimeError) as exc:
            await emit({"type": "error", "text": f"{model_name} не ответила: {exc}"})
            return
        if reasoning:
            await emit({"type": "reasoning", "text": reasoning[-4000:]})
        if content:
            await emit({"type": "assistant", "text": content})
        if usage:
            await emit({"type": "usage", "text": json.dumps(usage, ensure_ascii=False)})

        if not calls:
            store.append(session_id, "assistant", content, {"usage": usage})
            await emit({"type": "done", "text": content,
                        "wall_s": round(time.perf_counter() - started, 2)})
            return

        assistant_msg = {"role": "assistant", "content": content, "tool_calls": calls}
        messages.append(assistant_msg)
        store.append(session_id, "assistant", content, {"tool_calls": calls, "usage": usage})

        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            raw_args = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("аргументы не объект")
            except ValueError as exc:
                args = {}
                result = {"ok": False, "error": f"битый JSON аргументов: {exc}: {raw_args[:200]}"}
            else:
                await emit({"type": "tool", "name": name, "args": args})
                result = await asyncio.to_thread(tools.call, name, args)
            await emit({"type": "tool_result", "name": name, "result": result})
            payload = json.dumps(result, ensure_ascii=False)[:24000]
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "name": name, "content": payload})
            store.append(session_id, "tool", payload,
                         {"tool_call_id": call.get("id", ""), "name": name})

    await emit({"type": "error", "text": f"лимит шагов {MAX_STEPS} — работа не завершена"})
