"""Tool belt for the HARD-PRO agent daemon.

Every tool is local to the server: shell, filesystem, web, GPU, skills, MCP.
All paths resolve inside WORKSPACE unless absolute and explicitly allowed.
"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable

WORKSPACE = Path(os.environ.get("HP_WORKSPACE", Path.home() / "hardpro" / "workspace"))
MODEL_BASE = os.environ.get("HP_MODEL_BASE", "http://127.0.0.1:8081")
SHELL_TIMEOUT = int(os.environ.get("HP_SHELL_TIMEOUT", "180"))
MAX_OUT = int(os.environ.get("HP_MAX_OUTPUT", "20000"))

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}


def _clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    return text[:MAX_OUT] + f"\n… обрезано, всего {len(text)} символов"


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    return p


def shell(cmd: str, cwd: str | None = None, timeout: int | None = None) -> dict:
    """Run a bash command on the server. Full device access."""
    workdir = _resolve(cwd) if cwd else WORKSPACE
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["bash", "-lc", cmd], cwd=str(workdir), capture_output=True,
            text=True, timeout=timeout or SHELL_TIMEOUT, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"timeout {timeout or SHELL_TIMEOUT}s",
                "stdout": _clip(exc.stdout or ""), "stderr": _clip(exc.stderr or "")}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": proc.returncode == 0, "exit": proc.returncode,
            "cwd": str(workdir), "stdout": _clip(proc.stdout), "stderr": _clip(proc.stderr)}


def read_file(path: str, offset: int = 0, limit: int = 400) -> dict:
    p = _resolve(path)
    if not p.is_file():
        return {"ok": False, "error": f"нет файла: {p}"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    chunk = lines[offset: offset + limit]
    body = "\n".join(f"{offset + i + 1}: {ln}" for i, ln in enumerate(chunk))
    return {"ok": True, "path": str(p), "total_lines": len(lines), "content": _clip(body)}


def write_file(path: str, content: str) -> dict:
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(p), "bytes": len(content.encode())}


def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> dict:
    p = _resolve(path)
    if not p.is_file():
        return {"ok": False, "error": f"нет файла: {p}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        return {"ok": False, "error": "фрагмент не найден"}
    if not replace_all and text.count(old) > 1:
        return {"ok": False, "error": f"фрагмент встречается {text.count(old)} раз"}
    p.write_text(text.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
    return {"ok": True, "path": str(p)}


def list_dir(path: str = ".", depth: int = 1) -> dict:
    base = _resolve(path)
    if not base.is_dir():
        return {"ok": False, "error": f"нет каталога: {base}"}
    depth = max(1, min(depth, 3))
    out: list[str] = []
    root_parts = len(base.parts)

    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel = Path(cur).parts[root_parts:]
        level = len(rel)
        if level >= depth:
            dirs[:] = []
        for name in sorted(files)[:60]:
            fp = Path(cur) / name
            try:
                size = fp.stat().st_size
            except OSError:
                size = -1
            out.append(f"{'/'.join(rel + (name,))}\t{size}")
        if len(out) > 800:
            out.append("… обрезано")
            break
    return {"ok": True, "root": str(base), "entries": _clip("\n".join(out))}


def grep(pattern: str, path: str = ".", glob: str = "*", limit: int = 120) -> dict:
    base = _resolve(path)
    args = ["rg", "-n", "--no-heading", "--color=never", "-g", glob,
            "-m", str(limit), pattern, str(base)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60, errors="replace")
    except FileNotFoundError:
        args[0] = "grep"
        args = ["grep", "-rn", "--include", glob, pattern, str(base)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    lines = proc.stdout.splitlines()[:limit]
    return {"ok": True, "matches": len(lines), "content": _clip("\n".join(lines))}


def http_fetch(url: str, max_chars: int = 12000) -> dict:
    import httpx

    try:
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 HARD-PRO-agent"}) as cli:
            r = cli.get(url)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype:
        body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = html.unescape(re.sub(r"[ \t\r\f\v]+", " ", body))
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    return {"ok": r.status_code < 400, "status": r.status_code, "url": str(r.url),
            "content": body[:max_chars]}


def web_search(query: str, max_results: int = 8) -> dict:
    import httpx

    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as cli:
            r = cli.get(url)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    hits = []
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', r.text, re.S):
        link, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        link = urllib.parse.unquote(link.split("uddg=")[-1].split("&")[0]) if "uddg=" in link else link
        hits.append({"title": html.unescape(title).strip(), "url": link})
        if len(hits) >= max_results:
            break
    return {"ok": bool(hits), "query": query, "results": hits}


def model_list() -> dict:
    import httpx

    out = {}
    sources = []
    try:  # каталог профилей: все модели, включая выключенные
        from models import registry

        for card in registry.status()["models"]:
            sources.append((card["id"], card["base"], card["online"], card["gpus"]))
    except (ImportError, RuntimeError):
        sources = [
            ("coding", MODEL_BASE, None, []),
            ("media", "http://127.0.0.1:8083", None, []),
            ("ollama", "http://127.0.0.1:11434", None, []),
        ]
    for name, base, known, gpus in sources:
        if not base:
            out[name] = None
            continue
        if known is not None:
            out[name] = {"base": base, "gpus": gpus, "online": known}
            continue
        try:
            with httpx.Client(timeout=6) as cli:
                r = cli.get(f"{base}/v1/models")
                out[name] = [m.get("id") for m in r.json().get("data", [])]
        except (httpx.HTTPError, ValueError):
            out[name] = None
    return {"ok": True, "backends": out}


def gpu_status() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,"
                            "temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    rows = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 6:
            rows.append({"index": int(p[0]), "name": p[1], "used_mib": int(p[2]),
                         "total_mib": int(p[3]), "util": int(p[4]), "temp": int(p[5])})
    return {"ok": True, "gpus": rows}


SCHEMA: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Выполнить bash-команду на сервере. Полный доступ к устройству: файлы, пакеты, systemd, сеть.",
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "команда bash"},
            "cwd": {"type": "string", "description": "рабочий каталог, по умолчанию workspace"},
            "timeout": {"type": "integer", "description": "секунды, по умолчанию 180"}},
            "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Прочитать файл с номерами строк.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer"},
            "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Создать или перезаписать файл целиком.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Точечная замена фрагмента в файле.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "Показать содержимое каталога с размерами.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "depth": {"type": "integer"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Поиск по регулярке в файлах проекта.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
            "glob": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "http_fetch",
        "description": "Загрузить страницу или API, вернуть текст без разметки.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск в интернете (DuckDuckGo html), заголовки и ссылки.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "model_list",
        "description": "Список моделей на локальных бэкендах и их доступность.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "gpu_status",
        "description": "Загрузка, память и температура GPU.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "skill_list",
        "description": "Список загруженных скиллов из workspace/skills.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "skill_read",
        "description": "Прочитать тело скилла по имени.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
]

REGISTRY: dict[str, Callable[..., dict]] = {
    "shell": shell,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "grep": grep,
    "http_fetch": http_fetch,
    "web_search": web_search,
    "model_list": model_list,
    "gpu_status": gpu_status,
}


def register_skills(skills_module) -> None:
    def _list() -> dict:
        return {"ok": True, "skills": skills_module.index()}

    def _read(name: str) -> dict:
        body = skills_module.read(name)
        return {"ok": bool(body), "name": name, "content": _clip(body)} if body else \
            {"ok": False, "error": f"скилл не найден: {name}"}

    REGISTRY["skill_list"] = _list
    REGISTRY["skill_read"] = _read


def register_mcp(mcp_tools: dict[str, Callable[..., dict]]) -> None:
    REGISTRY.update(mcp_tools)


def schemas() -> list[dict[str, Any]]:
    return SCHEMA


def call(name: str, args: dict[str, Any]) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        return {"ok": False, "error": f"неизвестный инструмент: {name}"}
    try:
        return fn(**args)
    except TypeError as exc:
        return {"ok": False, "error": f"неверные аргументы {name}: {exc}"}
    except Exception as exc:  # tool code must never kill the loop
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
