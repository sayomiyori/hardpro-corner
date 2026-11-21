"""
title: Bench Context Guard
author: absolute ollie
version: 1.0.0
required_open_webui_version: 0.5.0
description: >
  Live context-window meter under every reply, plus a hard prompt trimmer
  that drops the oldest turns before the model gets flooded.
  Also strips stale meters on regenerate so they never stack.
"""

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

MARKER = "\n\n---\n`ctx"

# Prompt tokens each model will accept. Matched on substring, first hit wins.
DEFAULT_WINDOWS = {
    "gpt-5": 400000,
    "gpt-4.1": 1047576,
    "gpt-4o": 128000,
    "gpt-4-turbo": 128000,
    "o3": 200000,
    "o4-mini": 200000,
    "claude": 200000,
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini": 1048576,
    "deepseek": 131072,
    "qwen3-coder": 262144,
    "qwen2.5-coder": 32768,
    "qwen3": 131072,
    "qwen": 32768,
    "llama3.3": 131072,
    "llama3.2": 131072,
    "llama3.1": 131072,
    "codestral": 32768,
    "mistral": 32768,
    "devstral": 131072,
    "phi4": 16384,
    "gemma3": 131072,
    "glm": 200000,
    "kimi": 262144,
}


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    out.append(" " * 1000)  # rough image budget
        return "\n".join(out)
    return ""


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class Filter:
    class Valves(BaseModel):
        SHOW_STATUS: bool = Field(
            default=False,
            description="Append the context meter under each reply. Off: the plaque above the input does this.",
        )
        SHOW_BAR: bool = Field(default=True, description="Draw the 20-cell usage bar.")
        ENABLE_TRIM: bool = Field(
            default=True, description="Drop oldest turns when the prompt is too big."
        )
        MAX_PROMPT_TOKENS: int = Field(
            default=48000,
            description="Hard ceiling on prompt tokens. Keep below the model window.",
        )
        KEEP_LAST_MESSAGES: int = Field(
            default=24,
            description="Minimum recent messages never trimmed, if they fit.",
        )
        TRIM_HEADROOM: int = Field(
            default=8192, description="Tokens reserved for the reply."
        )
        WINDOWS_JSON: str = Field(
            default="", description='Override windows, e.g. {"qwen3-coder": 131072}'
        )

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True
        self._enc = None
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self._enc = None

    # ---------- helpers ----------

    def _count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            try:
                return len(self._enc.encode(text, disallowed_special=()))
            except Exception:
                pass
        return max(1, int(len(text) / 3.6))

    def _windows(self) -> dict:
        table = dict(DEFAULT_WINDOWS)
        if self.valves.WINDOWS_JSON.strip():
            try:
                extra = json.loads(self.valves.WINDOWS_JSON)
                if isinstance(extra, dict):
                    table.update({str(k).lower(): int(v) for k, v in extra.items()})
            except Exception:
                pass
        return table

    def _window(self, model: str) -> int:
        m = (model or "").lower()
        for key, size in self._windows().items():
            if key in m:
                return size
        return 32768

    def _strip_marker(self, text: str) -> str:
        if MARKER in text:
            idx = text.rfind(MARKER)
            return text[:idx].rstrip()
        return text

    def _msg_tokens(self, msg: dict) -> int:
        total = 4
        total += self._count(_as_text(msg.get("content")))
        for tc in msg.get("tool_calls") or []:
            try:
                total += self._count(json.dumps(tc, ensure_ascii=False))
            except Exception:
                total += 64
        return total

    def _trim(self, messages: list, budget: int) -> tuple[list, int]:
        """Drop oldest non-system turns until the prompt fits the budget."""
        sizes = [self._msg_tokens(m) for m in messages]
        total = sum(sizes)
        if total <= budget:
            return messages, total

        system = [m for m in messages if m.get("role") == "system"]
        rest = [(m, s) for m, s in zip(messages, sizes) if m.get("role") != "system"]
        if not rest:
            return messages, total

        keep = max(0, int(self.valves.KEEP_LAST_MESSAGES))
        head = sum(self._msg_tokens(m) for m in system)
        tail: list = []
        acc = head
        for msg, size in reversed(rest):
            if len(tail) >= keep and acc + size > budget:
                break
            if acc + size > budget and tail:
                break
            tail.append(msg)
            acc += size
        tail.reverse()

        # Never start the tail on an assistant/tool turn - orphans tool results.
        while tail and tail[0].get("role") not in ("user", "system"):
            acc -= self._msg_tokens(tail[0])
            tail.pop(0)

        return system + tail, acc

    # ---------- hooks ----------

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages") or []
        if not messages:
            return body

        # Scrub meters left behind by earlier regenerations.
        for msg in messages:
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                msg["content"] = self._strip_marker(msg["content"])

        if not self.valves.ENABLE_TRIM:
            return body

        budget = max(1024, int(self.valves.MAX_PROMPT_TOKENS) - int(self.valves.TRIM_HEADROOM))
        kept, used = self._trim(messages, budget)
        if len(kept) != len(messages):
            body["messages"] = kept
            body["_bench_trimmed"] = {
                "dropped": len(messages) - len(kept),
                "kept": len(kept),
                "tokens": used,
            }
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.SHOW_STATUS:
            return body
        messages = body.get("messages") or []
        if not messages:
            return body
        last = messages[-1]
        if last.get("role") != "assistant":
            return body

        usage = last.get("usage") or {}
        prompt_t = int(usage.get("prompt_tokens") or 0)
        out_t = int(usage.get("completion_tokens") or 0)
        total_t = int(usage.get("total_tokens") or (prompt_t + out_t))

        # Providers that report nothing: fall back to a local estimate.
        if not prompt_t:
            prompt_t = sum(self._msg_tokens(m) for m in messages[:-1])
        if not out_t:
            out_t = self._count(_as_text(last.get("content")))
        if not total_t:
            total_t = prompt_t + out_t

        window = self._window(body.get("model", ""))
        pct = min(999, round(100 * total_t / window)) if window else 0

        if self.valves.SHOW_BAR and window:
            cells = 20
            filled = min(cells, max(0, round(cells * total_t / window)))
            bar = "#" * filled + "-" * (cells - filled)
            bar_txt = f" `[{bar}]`" + (" **!!**" if pct >= 80 else "")
        else:
            bar_txt = ""

        line = (
            f"\n\n---\n`ctx {_fmt(total_t)} / {_fmt(window)} ({pct}%)`{bar_txt}"
            f"  ·  `in {_fmt(prompt_t)}`  ·  `out {_fmt(out_t)}`"
        )

        content = last.get("content")
        if isinstance(content, str):
            last["content"] = self._strip_marker(content) + line
        return body
