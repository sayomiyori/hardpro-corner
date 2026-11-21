#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex2owui.py — переносит сессии Codex CLI в Open WebUI, раскладывая их по папкам проектов.

Источник: rollout-*.jsonl из ~/.codex/sessions (или $CODEX_HOME/sessions).
Приёмник: JSON для Settings -> Data Controls -> Import Chats,
либо сразу REST API Open WebUI — тогда скрипт сам создаёт папки
(папка = проект, по session_meta.cwd) и импортирует чаты внутрь.

Использование:
    python codex2owui.py
    python codex2owui.py -o chats.json
    python codex2owui.py --api http://localhost:8080 --token sk-...
    python codex2owui.py --api ... --parent-folder Codex
    python codex2owui.py --folders single --folder-name "Codex archive"
    python codex2owui.py --folders none
    python codex2owui.py --folder-map projects.json

folder-map: {"C:\\work\\shop": "Магазин", "C:\\work\\api": "API"}

Зависимости: стандартная библиотека. .zst читается при `pip install zstandard`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Служебные user-сообщения, которые Codex подмешивает в протокол.
SCAFFOLD_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<permissions instructions>",
    "<turn_aborted>",
    "<user_shell_command>",
)

TEXT_TYPES = {"input_text", "output_text", "text", "summary_text"}
IMAGE_TYPES = {"input_image", "image"}
PROJECT_KEY = "__project"


# ---------------------------------------------------------------- парсинг Codex

def parse_ts(value) -> int | None:
    """RFC3339 -> unix seconds. Мусор не ломает импорт, просто без времени."""
    if not value or not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def flatten_content(content) -> str:
    """content = строка или массив {type, text}. Возвращает склейку текста."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in TEXT_TYPES:
            parts.append(item.get("text") or "")
        elif kind in IMAGE_TYPES:
            parts.append("[изображение]")
    return "\n".join(p for p in parts if p).strip()


def is_scaffold(text: str) -> bool:
    head = text.lstrip()
    return any(head.startswith(prefix) for prefix in SCAFFOLD_PREFIXES)


def _run_decompressor(cmd: list[str]) -> bytes | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def decompress_zst(path: Path) -> bytes | None:
    """zstandard -> CLI zstd -> 7-Zip. None, если распаковать нечем."""
    try:
        import zstandard  # type: ignore
    except ImportError:
        pass
    else:
        with open(path, "rb") as fh:
            return zstandard.ZstdDecompressor().decompress(fh.read(), max_output_size=1 << 31)

    zstd = shutil.which("zstd")
    if zstd:
        data = _run_decompressor([zstd, "-dc", str(path)])
        if data is not None:
            return data

    candidates = [shutil.which("7z"), shutil.which("7za"), shutil.which("7zr"),
                  r"C:\Program Files\7-Zip\7z.exe",
                  r"C:\Program Files (x86)\7-Zip\7z.exe"]
    for exe in candidates:
        if exe and Path(exe).exists():
            data = _run_decompressor([exe, "e", "-so", str(path)])
            if data is not None:
                return data
    return None


_ZST_WARNED = False


def read_records(path: Path) -> Iterable[dict]:
    """Построчный JSON. Битые строки пропускаются, а не валят файл."""
    if path.suffix == ".zst":
        global _ZST_WARNED
        raw = decompress_zst(path)
        if raw is None:
            if not _ZST_WARNED:
                _ZST_WARNED = True
                print("[skip] .jsonl.zst: нужен `pip install zstandard` "
                      "или zstd/7-Zip в PATH — такие сессии пропускаются",
                      file=sys.stderr)
            return
        lines = (line for line in raw.decode("utf-8", "replace").splitlines())
    else:
        lines = open(path, "r", encoding="utf-8", errors="replace")

    try:
        for line in lines:
            line = line.lstrip("\ufeff").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        if path.suffix != ".zst":
            lines.close()


def convert_session(path: Path, include_reasoning: bool = False) -> dict | None:
    meta: dict = {}
    collected: list[dict] = []
    current_model = "codex"
    fallback_messages: list[dict] = []

    for rec in read_records(path):
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ts = parse_ts(rec.get("timestamp"))

        if kind == "session_meta":
            meta = payload
            continue

        if kind == "turn_context":
            model = payload.get("model")
            if model:
                current_model = model
            continue

        if kind == "response_item":
            item_type = payload.get("type")

            if item_type == "message":
                role = payload.get("role")
                text = flatten_content(payload.get("content"))
                if role in ("user", "assistant") and text and not is_scaffold(text):
                    collected.append({"role": role, "text": text, "ts": ts,
                                      "model": current_model if role == "assistant" else None})
                continue

            if item_type == "reasoning" and include_reasoning:
                text = flatten_content(payload.get("summary"))
                if text:
                    collected.append({"role": "assistant", "text": text, "ts": ts,
                                      "model": current_model})
                continue

            if item_type == "function_call":
                name = payload.get("name") or "tool"
                args = payload.get("arguments")
                collected.append({"role": "assistant", "ts": ts, "model": current_model,
                                  "text": f"```\n{name}({args})\n```"})
                continue

            if item_type == "function_call_output":
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content") or json.dumps(output, ensure_ascii=False)
                if output:
                    collected.append({"role": "assistant", "ts": ts, "model": current_model,
                                      "text": f"```\n{str(output)[:4000]}\n```"})
                continue

        # Старый формат: сообщение прямо в top-level.
        if kind == "message" and rec.get("role") in ("user", "assistant"):
            text = flatten_content(rec.get("content"))
            if text and not is_scaffold(text):
                collected.append({"role": rec["role"], "text": text, "ts": ts, "model": None})
            continue

        # Резерв: если протокольных сообщений нет, берём display-лог.
        if kind == "event_msg":
            ev = payload.get("type")
            if ev == "user_message" and payload.get("message"):
                fallback_messages.append({"role": "user", "text": str(payload["message"]),
                                          "ts": ts, "model": None})
            elif ev == "agent_message" and payload.get("message"):
                fallback_messages.append({"role": "assistant", "text": str(payload["message"]),
                                          "ts": ts, "model": current_model})

    messages = collected or fallback_messages
    if not messages:
        return None

    # Склеиваем подряд идущие реплики одной роли.
    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["text"] += "\n\n" + msg["text"]
            if msg["ts"]:
                merged[-1]["ts"] = msg["ts"]
        else:
            merged.append(dict(msg))

    history: dict[str, dict] = {}
    models: list[str] = []
    prev_id = None
    created = merged[0]["ts"]
    updated = merged[-1]["ts"]

    for msg in merged:
        mid = uuid.uuid4().hex
        node = {
            "id": mid,
            "parentId": prev_id,
            "childrenIds": [],
            "role": msg["role"],
            "content": msg["text"],
            "timestamp": msg["ts"],
        }
        if msg["role"] == "assistant":
            model = msg.get("model") or current_model
            node["model"] = model
            node["done"] = True
            models.append(model)
        if prev_id:
            history[prev_id]["childrenIds"].append(mid)
        history[mid] = node
        prev_id = mid

    first_user = next((m["text"] for m in merged if m["role"] == "user"), "")
    title = first_user.splitlines()[0].strip()[:80] if first_user else ""
    if not title:
        stamp = created or 0
        title = f"Codex {datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M') if stamp else path.stem}"

    unique_models = list(dict.fromkeys(m for m in models if m)) or [current_model]

    return {
        "chat": {
            "title": title,
            "models": unique_models,
            "history": {"currentId": prev_id, "messages": history},
        },
        "meta": {
            "tags": ["codex"],
            "source": meta.get("id") or path.stem,
            "cwd": meta.get("cwd"),
        },
        "pinned": False,
        "folder_id": None,
        "created_at": created,
        "updated_at": updated,
        PROJECT_KEY: meta.get("cwd") or "",
    }


def iter_rollouts(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("rollout-*.jsonl")):
        yield path
    for path in sorted(root.rglob("rollout-*.jsonl.zst")):
        yield path


# ------------------------------------------------------- раскладка по проектам

def load_folder_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("folder-map должен быть объектом {cwd: имя_папки}")
    return {str(k).rstrip("\\/").casefold(): str(v) for k, v in raw.items()}


def path_parts(cwd: str) -> list[str]:
    """C:\\work\\shop -> ['work', 'shop']; диск и UNC-якоря отбрасываются."""
    cleaned = cwd.strip().replace("/", "\\").rstrip("\\")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("\\")
    return [p for p in cleaned.split("\\") if p]


def unique_names(cwds: Iterable[str]) -> dict[str, str]:
    """Наращивает глубину пути, пока имена папок не станут уникальными."""
    parts = {cwd: path_parts(cwd) for cwd in cwds}
    depth = {cwd: 1 for cwd in parts}
    while True:
        names = {cwd: " / ".join(parts[cwd][-depth[cwd]:]) or "Без проекта"
                 for cwd in parts}
        buckets: dict[str, list[str]] = {}
        for cwd, name in names.items():
            buckets.setdefault(name.casefold(), []).append(cwd)

        grew = False
        for group in buckets.values():
            if len(group) < 2:
                continue
            for cwd in group:
                if depth[cwd] < len(parts[cwd]):
                    depth[cwd] += 1
                    grew = True
        if not grew:
            return names


def plan_folders(chats: list[dict], mode: str, single_name: str,
                 mapping: dict[str, str]) -> list[tuple[str, list[dict]]]:
    """Возвращает [(имя_папки, [чаты])] с уникальными именами."""
    if mode == "none":
        return [("", chats)]

    if mode == "single":
        return [(single_name, chats)]

    groups: dict[str, list[dict]] = {}
    for chat in chats:
        cwd = chat.get(PROJECT_KEY) or ""
        groups.setdefault(cwd, []).append(chat)

    auto = unique_names(groups.keys())
    raw_names: dict[str, str] = {}
    for cwd in groups:
        mapped = mapping.get(cwd.rstrip("\\/").casefold())
        raw_names[cwd] = mapped or auto[cwd]

    # Ручные имена могут совпасть — добиваем индексом, чтобы не склеить проекты.
    seen: dict[str, int] = {}
    for cwd in raw_names:
        key = raw_names[cwd].casefold()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            raw_names[cwd] = f"{raw_names[cwd]} #{seen[key]}"

    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].casefold()))
    return [(raw_names[cwd], items) for cwd, items in ordered]


# ------------------------------------------------------------- REST Open WebUI

class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class Api:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, payload: Any = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace")[:400]) from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"[fail] {self.base}: {exc.reason}") from None

    def list_folders(self) -> list[dict]:
        return self.call("GET", "/api/v1/folders/") or []

    def ensure_folder(self, name: str, cache: dict[str, str]) -> str:
        key = name.casefold()
        if key in cache:
            return cache[key]
        try:
            folder = self.call("POST", "/api/v1/folders/",
                               {"name": name, "data": {}, "meta": {}})
        except ApiError as exc:
            if exc.status != 400:
                raise
            # Папка уже есть (в т.ч. создана параллельно) — перечитываем список.
            folder = next((f for f in self.list_folders()
                           if f["name"].casefold() == key), None)
            if not folder:
                raise
        cache[key] = folder["id"]
        return folder["id"]

    def set_parent(self, folder_id: str, parent_id: str) -> None:
        self.call("POST", f"/api/v1/folders/{folder_id}/update/parent",
                  {"parent_id": parent_id})

    def import_chats(self, chats: list[dict], batch: int = 50) -> int:
        total = 0
        for start in range(0, len(chats), batch):
            chunk = chats[start:start + batch]
            self.call("POST", "/api/v1/chats/import", {"chats": chunk})
            total += len(chunk)
            print(f"[ok] импортировано {total}/{len(chats)}")
        return total


def push(api: Api, groups: list[tuple[str, list[dict]]], parent_name: str | None) -> None:
    cache: dict[str, str] = {}
    parent_id = api.ensure_folder(parent_name, cache) if parent_name else None

    payload: list[dict] = []
    for folder_name, chats in groups:
        folder_id = None
        if folder_name:
            folder_id = api.ensure_folder(folder_name, cache)
            if parent_id and folder_id != parent_id:
                api.set_parent(folder_id, parent_id)
        for chat in chats:
            chat = dict(chat)
            chat.pop(PROJECT_KEY, None)
            chat["folder_id"] = folder_id
            payload.append(chat)

    api.import_chats(payload)


# ----------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Codex CLI -> Open WebUI chats + folders")
    parser.add_argument("-o", "--out", default="codex_openwebui.json")
    parser.add_argument("--root", default=None,
                        help="каталог sessions (по умолчанию $CODEX_HOME/sessions или ~/.codex/sessions)")
    parser.add_argument("--reasoning", action="store_true", help="включать summary reasoning")
    parser.add_argument("--folders", choices=["project", "single", "none"], default="project",
                        help="project — папка на каждый cwd (по умолчанию)")
    parser.add_argument("--folder-name", default="Codex", help="имя папки для --folders single")
    parser.add_argument("--parent-folder", default=None,
                        help="общий родитель, напр. Codex — все проекты лягут внутрь него")
    parser.add_argument("--folder-map", default=None,
                        help="JSON {cwd: имя_папки} для ручных названий")
    parser.add_argument("--api", default=None, help="базовый URL Open WebUI, напр. http://localhost:8080")
    parser.add_argument("--token", default=None, help="API-ключ Open WebUI (Bearer)")
    args = parser.parse_args()

    root = Path(args.root).expanduser() if args.root else Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser() / "sessions"

    if not root.is_dir():
        print(f"[fail] нет каталога сессий: {root}", file=sys.stderr)
        return 1

    chats: list[dict] = []
    skipped = 0
    for path in iter_rollouts(root):
        try:
            chat = convert_session(path, args.reasoning)
        except Exception as exc:  # одна битая сессия не должна ронять экспорт
            print(f"[warn] {path.name}: {exc}", file=sys.stderr)
            chat = None
        if chat:
            chats.append(chat)
        else:
            skipped += 1

    if not chats:
        print("[fail] не найдено ни одной сессии с сообщениями", file=sys.stderr)
        return 1

    groups = plan_folders(chats, args.folders, args.folder_name, load_folder_map(args.folder_map))
    if args.parent_folder and args.folders == "project":
        print(f"[plan] папка-родитель: {args.parent_folder}")

    for name, items in groups:
        print(f"[plan] {name or '<без папки>'}: {len(items)} чатов")

    flat = []
    for name, items in groups:
        for chat in items:
            chat = dict(chat)
            project = chat.pop(PROJECT_KEY, "")
            if name and args.folders == "project":
                chat["meta"]["project"] = name
            chat["folder_id"] = None
            flat.append(chat)

    out = Path(args.out).expanduser()
    out.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {len(flat)} чатов -> {out} (пропущено: {skipped})")

    if args.api:
        token = args.token or os.environ.get("OPENWEBUI_TOKEN")
        if not token:
            print("[fail] нужен --token или OPENWEBUI_TOKEN", file=sys.stderr)
            return 1
        try:
            push(Api(args.api, token), groups, args.parent_folder)
        except ApiError as exc:
            print(f"[fail] {exc}", file=sys.stderr)
            return 1
        print("[ok] готово: чаты разложены по папкам проектов")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
