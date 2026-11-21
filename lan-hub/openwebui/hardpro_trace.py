"""
title: HARD-PRO Trace
author: HARD-PRO
author_url: https://llm.lan
version: 1.2
license: MIT
description: Показывает админу, как отработала каждая модель — время, токены, скорость, стоп-причина.
required_open_webui_version: 0.13.4
"""

from __future__ import annotations

import time
from typing import Any

try:
    from pydantic import BaseModel
except Exception:  # на случай песочницы без pydantic
    BaseModel = object  # type: ignore


def _is_admin(user: Any) -> bool:
    if isinstance(user, dict):
        return user.get("role") == "admin"
    return getattr(user, "role", None) == "admin"


def _key(body: dict, metadata: dict | None = None) -> str:
    return str(
        (body or {}).get("chat_id")
        or (metadata or {}).get("chat_id")
        or (metadata or {}).get("session_id")
        or "adhoc"
    )


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


class Filter:
    class Valves(BaseModel):
        admin_only: bool = True
        show_prompt_tokens: bool = True

    def __init__(self) -> None:
        try:
            self.valves = self.Valves()
        except Exception:
            self.valves = None
        self._start: dict[str, float] = {}

    async def inlet(self, body: dict, __user__: Any = None,
                    __metadata__: dict | None = None) -> dict:
        """Запоминаем старт и просим бэкенд вернуть usage."""
        if self.valves is not None and getattr(self.valves, "admin_only", True) and not _is_admin(__user__):
            return body
        self._start[_key(body, __metadata__)] = time.time()
        if isinstance(body, dict):
            opts = body.get("stream_options")
            if not isinstance(opts, dict):
                opts = {}
            opts["include_usage"] = True
            body["stream_options"] = opts
        return body

    def _block(self, model: str, elapsed: float, msg: dict) -> str:
        usage = msg.get("usage") or {}
        info = msg.get("info") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        speed = (float(completion) / elapsed) if (completion and elapsed > 0) else None
        details = usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        timings = info.get("timings") if isinstance(info, dict) else None

        head = ["⚙ **след**", f"`{model}`", f"**{_fmt(elapsed, 2)} с**"]
        if speed:
            head.append(f"**{_fmt(speed)} tok/s**")
        if completion:
            head.append(f"{completion} ток")
        parts = [f"prompt {prompt or '—'}", f"completion {completion or '—'}",
                 f"total {total or '—'}"]
        finish = msg.get("finish_reason")
        if finish:
            parts.append(f"стоп `{finish}`")
        if reasoning:
            parts.append(f"рассуждение {reasoning} ток")
        if isinstance(timings, dict):
            if timings.get("prompt_per_second"):
                parts.append(f"prompt {_fmt(timings['prompt_per_second'])} tok/s")
            if timings.get("predicted_per_second"):
                parts.append(f"генерация {_fmt(timings['predicted_per_second'])} tok/s")
        sources = msg.get("sources") or []
        if sources:
            parts.append(f"источников {len(sources)}")
        return "\n\n---\n" + " · ".join(head) + "\n\n" + " · ".join(parts) + "\n"

    async def outlet(self, body: dict, __user__: Any = None) -> dict:
        """Дописываем трассу к последнему ответу ассистента (только админу)."""
        if self.valves is not None and getattr(self.valves, "admin_only", True) and not _is_admin(__user__):
            return body
        try:
            started = self._start.pop(_key(body), None)
            if started is None:
                started = self._start.pop("adhoc", None)
            elapsed = (time.time() - started) if started else 0.0

            messages = body.get("messages") or []
            target_id = body.get("id")
            target = None
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and (target_id is None or msg.get("id") == target_id):
                    target = msg
                    break
            if target is None:
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        target = msg
                        break
            if target is None:
                return body

            content = target.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
            if "⚙ **след**" in content or "⚙ след модели" in content:
                return body
            target["content"] = content + self._block(str(body.get("model") or "?"), elapsed, target)
        except Exception:
            return body
        return body
