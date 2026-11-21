#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex2owui_context.py — переносит контекст Codex в Open WebUI.

Что берёт с диска:
  * ~/.codex/skills/*/SKILL.md            -> Workspace -> Skills (+ вложенные файлы)
  * ~/.codex/AGENTS.md, rules/*.rules      -> Prompt `codex-rules` + база знаний
  * <projects>/**/AGENTS.md                -> Prompt на проект + база знаний + пресет модели
  * ~/.codex/config.toml [mcp_servers]     -> Tool Servers (remote MCP) / mcp-proxy (stdio)
  * ~/.codex/plugins/cache/*/*/skills      -> Skills с тегом plugin:<name>

Режимы:
    python codex2owui_context.py                       # только план, без сети
    python codex2owui_context.py --apply --api ... --token ...
    python codex2owui_context.py --apply ... --only skills,prompts
    python codex2owui_context.py --apply ... --dry-run-files   # показать, что зальётся

Эндпоинты проверены по openapi сервера: /api/v1/skills/create, /api/v1/prompts/create,
/api/v1/knowledge/create, /api/v1/files/, /api/v1/knowledge/{id}/file/add,
/api/v1/models/create, /api/v1/configs/tool_servers.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "__pycache__",
             ".venv", "venv", "backup", "backups"}
TEXT_SUFFIXES = {".md", ".txt", ".rules", ".toml", ".json", ".yaml", ".yml", ".py",
                 ".js", ".ts", ".tsx", ".css", ".html", ".ps1", ".sql", ".sh"}
MAX_FILE_BYTES = 2 * 1024 * 1024

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slug(value: str, fallback: str = "item") -> str:
    lowered = "".join(TRANSLIT.get(ch, ch) for ch in value.casefold())
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned or fallback


# ------------------------------------------------------------------ файлы/текст

def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end]
    body = text[end + 4:].lstrip("\n")

    meta: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and key:
            meta[key] += " " + line.strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            meta[key] = value.strip().strip("'\"")
    return meta, body


def skill_files(skill_dir: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.suffix and path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = read_text(path)
        if text is None:
            continue
        files.append({"path": rel.as_posix(), "content": text})
    return files


# ---------------------------------------------------------------- инвентарь

def collect_skills(codex_home: Path, include_system: bool) -> list[dict]:
    skills: list[dict] = []
    roots = [codex_home / "skills"]
    cache_root = codex_home / "plugins" / "cache"
    for pattern in ("*/*/skills", "*/*/*/skills", "*/*/*/*/skills"):
        roots.extend(cache_root.glob(pattern))

    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name == ".system":
                continue
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                continue
            text = read_text(md) or ""
            meta, body = parse_frontmatter(text)
            name = meta.get("name") or skill_dir.name
            key = slug(name)
            if key in seen:
                continue
            seen.add(key)
            plugin = "plugin" if "plugins" in root.parts else "core"
            skills.append({
                "id": key,
                "name": name,
                "description": meta.get("description", "").strip(),
                "content": body.strip(),
                "files": skill_files(skill_dir),
                "tags": ["codex", plugin],
                "source": str(md),
            })

    if include_system:
        system = codex_home / "skills" / ".system"
        if system.is_dir():
            for skill_dir in sorted(system.iterdir()):
                md = skill_dir / "SKILL.md"
                if not md.is_file():
                    continue
                meta, body = parse_frontmatter(read_text(md) or "")
                key = slug(meta.get("name") or skill_dir.name)
                if key in seen:
                    continue
                seen.add(key)
                skills.append({
                    "id": key,
                    "name": meta.get("name") or skill_dir.name,
                    "description": meta.get("description", "").strip(),
                    "content": body.strip(),
                    "files": skill_files(skill_dir),
                    "tags": ["codex", "system"],
                    "source": str(md),
                })
    return skills


def project_docs(project: Path, limit: int = 25) -> list[Path]:
    docs: list[Path] = []
    agents = project / "AGENTS.md"
    if agents.is_file():
        docs.append(agents)
    for pattern in ("README.md", "README.MD", "readme.md"):
        candidate = project / pattern
        if candidate.is_file():
            docs.append(candidate)
            break
    for path in sorted(project.rglob("*.md")):
        if len(docs) >= limit:
            break
        rel = path.relative_to(project)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.parent == project and path.name in ("AGENTS.md", "README.md"):
            continue
        if rel.parts[0] not in ("docs", "doc", "src", "backend", "frontend"):
            continue
        docs.append(path)
    return docs


SKIP_SEGMENTS = {"backup", "backups", "old", "stage", "releases", "fixtures",
                 "helper-fixtures", "node_modules", "dist", "build", ".tmp", "tmp",
                 ".git", "venv", ".venv", "src", "tools"}
DATE_SEGMENT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCRATCH_SEGMENT = re.compile(r"^(this-pc-|hp-restore-qa|backup-)", re.I)


def collect_projects(root: Path, exclude: list[str]) -> list[dict]:
    projects: list[dict] = []
    if not root.is_dir():
        return projects
    seen_slugs: dict[str, int] = {}
    for agents in sorted(root.rglob("AGENTS.md")):
        rel = agents.relative_to(root)
        parts = rel.parts[:-1]
        low = str(rel).casefold()
        if any(bad in low for bad in exclude):
            continue
        if any(part.casefold() in SKIP_SEGMENTS
               or DATE_SEGMENT.match(part)
               or SCRATCH_SEGMENT.match(part) for part in parts):
            continue
        project = agents.parent
        text = read_text(agents) or ""
        if not text.strip():
            continue
        base_slug = slug("-".join(parts) or project.name)
        if base_slug.startswith("codex-"):
            base_slug = base_slug[len("codex-"):]
        seen_slugs[base_slug] = seen_slugs.get(base_slug, 0) + 1
        if seen_slugs[base_slug] > 1:
            base_slug = f"{base_slug}-{seen_slugs[base_slug]}"
        label_parts = parts[1:] if parts and parts[0].casefold() == "codex" else parts
        projects.append({
            "slug": base_slug,
            "name": " / ".join(label_parts) if label_parts else project.name,
            "path": project,
            "agents": text,
            "docs": project_docs(project),
        })
    return projects


def collect_mcp(codex_home: Path) -> dict:
    cfg_path = codex_home / "config.toml"
    remote: list[dict] = []
    stdio: list[dict] = []
    skipped: list[dict] = []
    if not cfg_path.is_file():
        return {"remote": remote, "stdio": stdio, "skipped": skipped}

    with cfg_path.open("rb") as fh:
        config = tomllib.load(fh)

    for name, spec in (config.get("mcp_servers") or {}).items():
        if name.startswith("node_repl"):
            skipped.append({"name": name, "reason": "внутренний сервер Codex"})
            continue
        if spec.get("url"):
            remote.append({"name": name, "url": spec["url"]})
            continue
        command = spec.get("command")
        if not command:
            skipped.append({"name": name, "reason": "нет command/url"})
            continue
        stdio.append({
            "name": name,
            "command": command,
            "args": [str(a) for a in spec.get("args", [])],
            "cwd": spec.get("cwd"),
            "env": {k: str(v) for k, v in (spec.get("env") or {}).items()},
        })
    return {"remote": remote, "stdio": stdio, "skipped": skipped}


# ------------------------------------------------------------------- REST

class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class Api:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    # -- низкий уровень

    def _open(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ApiError(exc.code, detail) from None
        except urllib.error.URLError as exc:
            raise SystemExit(f"[fail] {self.base}: {exc.reason}") from None

    def json_call(self, method: str, path: str, payload: Any = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
        )
        return self._open(req)

    def upload(self, filename: str, content: bytes, metadata: dict | None = None) -> dict:
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(filename)[0] or "text/markdown"
        parts: list[bytes] = []
        if metadata is not None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n"
                f"{json.dumps(metadata, ensure_ascii=False)}\r\n".encode("utf-8"))
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        req = urllib.request.Request(
            self.base + "/api/v1/files/", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Authorization": f"Bearer {self.token}"},
        )
        return self._open(req)


# --------------------------------------------------------------- выгрузка

def ensure_knowledge(api: Api, name: str, description: str, cache: dict[str, str]) -> str:
    if name in cache:
        return cache[name]
    for kb in api.json_call("GET", "/api/v1/knowledge/") or []:
        if kb.get("name") == name:
            cache[name] = kb["id"]
            return kb["id"]
    created = api.json_call("POST", "/api/v1/knowledge/create",
                            {"name": name, "description": description})
    cache[name] = created["id"]
    return created["id"]


def add_document(api: Api, kb_id: str, path: Path) -> None:
    content = path.read_bytes()
    uploaded = api.upload(path.name, content)
    api.json_call("POST", f"/api/v1/knowledge/{kb_id}/file/add", {"file_id": uploaded["id"]})


def push_skill(api: Api, skill: dict) -> None:
    payload = {
        "id": skill["id"],
        "name": skill["name"],
        "description": skill["description"],
        "content": skill["content"],
        "files": [{"path": f["path"], "content": f["content"]} for f in skill["files"]],
        "meta": {"tags": skill["tags"]},
        "is_active": True,
        "commit_message": "import from Codex",
    }
    try:
        api.json_call("POST", "/api/v1/skills/create", payload)
    except ApiError as exc:
        if exc.status != 400:
            raise
        api.json_call("POST", f"/api/v1/skills/id/{skill['id']}/update", payload)


def push_prompt(api: Api, command: str, name: str, content: str, tags: list[str]) -> None:
    payload = {"command": command, "name": name, "content": content, "tags": tags}
    try:
        api.json_call("POST", "/api/v1/prompts/create", payload)
    except ApiError as exc:
        if exc.status != 400:
            raise
        for prompt in api.json_call("GET", "/api/v1/prompts/list") or []:
            if prompt.get("command") == command:
                api.json_call("POST", f"/api/v1/prompts/id/{prompt['id']}/update", payload)
                return


def push_model(api: Api, model_id: str, name: str, base_model: str,
               system_prompt: str, description: str, knowledge: list[dict]) -> None:
    payload = {
        "id": model_id,
        "name": name,
        "base_model_id": base_model,
        "meta": {"description": description, "knowledge": knowledge,
                 "tags": [{"name": "codex"}]},
        "params": {"system_prompt": system_prompt},
        "is_active": True,
        "commit_message": "import from Codex",
    }
    try:
        api.json_call("POST", "/api/v1/models/create", payload)
    except ApiError as exc:
        if exc.status != 400:
            raise
        api.json_call("POST", "/api/v1/models/model/update", payload)


def push_tool_servers(api: Api, remote: list[dict]) -> None:
    current = api.json_call("GET", "/api/v1/configs/tool_servers") or {}
    connections = current.get("TOOL_SERVER_CONNECTIONS") or []
    known = {(c.get("url") or "").rstrip("/") for c in connections}
    for server in remote:
        url = server["url"].rstrip("/")
        if url in known:
            continue
        connections.append({
            "url": url,
            "path": server.get("path", ""),
            "type": server.get("type", "mcp"),
            "auth_type": server.get("auth_type", "none"),
            "key": server.get("key", ""),
            "config": {},
            "info": {"name": server["name"]},
        })
    api.json_call("POST", "/api/v1/configs/tool_servers",
                  {"TOOL_SERVER_CONNECTIONS": connections})


def _ps_single(value: str) -> str:
    """Безопасная строка в одинарных кавычках PowerShell."""
    return "'" + value.replace("'", "''") + "'"


def lan_ip() -> str:
    """Первый не-loopback IPv4 — адрес, по которому сервер увидит этот ПК."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.10", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def proxy_script(stdio: list[dict], host: str, base_port: int = 8100) -> tuple[str, list[dict]]:
    """stdio MCP -> Streamable HTTP через mcp-proxy (mcpo ломается на mcp>=2)."""
    import secrets

    lines = [
        "# сгенерировано codex2owui_context.py",
        "# stdio MCP Codex -> Streamable HTTP, который понимает Open WebUI",
        "# запуск: powershell -ExecutionPolicy Bypass -File mcp-proxy.ps1",
        "# нужен uv (даёт uvx): https://docs.astral.sh/uv/",
        "",
    ]
    servers: list[dict] = []
    for index, server in enumerate(stdio):
        port = base_port + index
        token = secrets.token_urlsafe(24)
        argv = [server["command"], *server["args"]]
        arg_line = " ".join('"' + a.replace('"', '\\"') + '"'
                            for a in ["--host", "0.0.0.0", "--port", str(port),
                                      "--transport", "streamablehttp",
                                      "--auth-bearer-token", token, "--", *argv])
        lines.append(f"$argline{index} = {_ps_single(arg_line)}")
        if server.get("env"):
            for key, value in server["env"].items():
                lines.append(f"$env:{key} = {_ps_single(value)}")
        workdir = f" -WorkingDirectory {_ps_single(server['cwd'])}" if server.get("cwd") else ""
        lines.append(f"Start-Process -FilePath 'uvx' "
                     f"-ArgumentList (\"mcp-proxy \" + $argline{index}) "
                     f"-WindowStyle Hidden{workdir}")
        lines.append("")
        servers.append({"name": server["name"], "port": port,
                        "url": f"http://{host}:{port}", "path": "/mcp",
                        "auth_type": "bearer", "key": token})
    return "\n".join(lines), servers


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex context -> Open WebUI")
    parser.add_argument("--api", default="http://192.0.2.10:8775")
    parser.add_argument("--token", default=os.environ.get("OPENWEBUI_TOKEN"))
    parser.add_argument("--apply", action="store_true", help="писать на сервер (иначе только план)")
    parser.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME))
    parser.add_argument("--projects-root", default=r"E:\Work\Projects")
    parser.add_argument("--base-model", default=None,
                        help="id базовой модели для пресетов проектов")
    parser.add_argument("--only", default="skills,prompts,knowledge,models,mcp")
    parser.add_argument("--include-system-skills", action="store_true")
    parser.add_argument("--out", default="codex_context_report.json")
    parser.add_argument("--proxy-out", default="mcp-proxy.ps1")
    parser.add_argument("--register-proxy", action="store_true",
                        help="добавить сгенерированные mcp-proxy адреса в Tool Servers")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    only = {part.strip() for part in args.only.split(",") if part.strip()}

    skills = collect_skills(codex_home, args.include_system_skills)
    projects = collect_projects(Path(args.projects_root),
                                exclude=["backup-", "hp-restore-qa", "\\codex\\2026-", "/codex/2026-"])
    mcp = collect_mcp(codex_home)

    global_rules = read_text(codex_home / "AGENTS.md") or ""
    default_rules = read_text(codex_home / "rules" / "default.rules") or ""

    report = {
        "skills": [{"id": s["id"], "name": s["name"], "files": len(s["files"]),
                    "bytes": len(s["content"])} for s in skills],
        "projects": [{"slug": p["slug"], "name": p["name"], "path": str(p["path"]),
                      "docs": [d.name for d in p["docs"]]} for p in projects],
        "mcp": mcp,
        "global_rules_bytes": len(global_rules),
        "default_rules_bytes": len(default_rules),
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[plan] skills: {len(skills)}")
    print(f"[plan] проектов с AGENTS.md: {len(projects)}")
    print(f"[plan] MCP remote: {len(mcp['remote'])}, stdio: {len(mcp['stdio'])}, "
          f"пропущено: {len(mcp['skipped'])}")
    print(f"[plan] глобальные правила: {len(global_rules)} байт, default.rules: {len(default_rules)}")

    if args.apply:
        if not args.token:
            print("[fail] нужен --token или OPENWEBUI_TOKEN", file=sys.stderr)
            return 1
        api = Api(args.api, args.token)

        base_model = args.base_model
        if not base_model and ("models" in only or "knowledge" in only):
            available = api.json_call("GET", "/api/models") or {}
            rows = available.get("data") or []
            if rows:
                base_model = rows[0]["id"]
                print(f"[info] базовая модель: {base_model}")
            else:
                print("[warn] на сервере нет моделей — пресеты проектов пропущены")

        if "skills" in only:
            for skill in skills:
                push_skill(api, skill)
            print(f"[ok] skills: {len(skills)}")

        if "prompts" in only and global_rules:
            push_prompt(api, "codex-rules", "Codex — глобальные правила",
                        global_rules, ["codex", "rules"])
            print("[ok] prompt codex-rules")

        if "knowledge" in only and (global_rules or default_rules):
            kb_id = ensure_knowledge(api, "Codex — правила и контекст",
                                     "AGENTS.md и rules из Codex CLI", {})
            if global_rules:
                tmp = Path("_codex_AGENTS.md")
                tmp.write_text(global_rules, encoding="utf-8")
                add_document(api, kb_id, tmp)
                tmp.unlink()
            if default_rules:
                tmp = Path("_codex_default.rules")
                tmp.write_text(default_rules, encoding="utf-8")
                add_document(api, kb_id, tmp)
                tmp.unlink()
            print("[ok] knowledge: Codex — правила и контекст")

        kb_cache: dict[str, str] = {}
        for project in projects:
            prompt_command = f"codex-{project['slug']}"
            if "prompts" in only:
                push_prompt(api, prompt_command,
                            f"Codex — {project['name']}",
                            project["agents"] or f"# {project['name']}",
                            ["codex", "project"])
            kb_id = None
            if "knowledge" in only:
                kb_id = ensure_knowledge(api, f"Codex — {project['name']}",
                                         f"Контекст проекта {project['path']}", kb_cache)
                for doc in project["docs"]:
                    try:
                        add_document(api, kb_id, doc)
                    except ApiError as exc:
                        print(f"[warn] {doc}: {exc}")
            if "models" in only and base_model:
                push_model(api, f"codex-{project['slug']}", f"Codex — {project['name']}",
                           base_model, project["agents"],
                           f"Проект {project['path']} (импорт из Codex)",
                           [{"id": kb_id, "name": f"Codex — {project['name']}",
                             "type": "collection", "status": "verified"}] if kb_id else [])
            print(f"[ok] проект {project['name']}")

        if "mcp" in only:
            push_tool_servers(api, mcp["remote"])
            print(f"[ok] tool servers: {len(mcp['remote'])} remote")

    if mcp["stdio"]:
        host = lan_ip()
        script, servers = proxy_script(mcp["stdio"], host)
        Path(args.proxy_out).write_text(script, encoding="utf-8")
        print(f"[ok] stdio MCP -> {args.proxy_out} ({len(servers)} серверов, host {host})")
        for server in servers:
            print(f"      {server['name']}: {server['url']}{server['path']}")
        if args.apply and args.register_proxy and "mcp" in only and args.token:
            api = Api(args.api, args.token)
            push_tool_servers(api, [
                {"name": s["name"], "url": s["url"], "path": s["path"],
                 "type": "mcp", "auth_type": s["auth_type"], "key": s["key"]}
                for s in servers
            ])
            print(f"[ok] tool servers: {len(servers)} через mcp-proxy")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
