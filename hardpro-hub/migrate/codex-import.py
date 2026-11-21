#!/usr/bin/env python3
"""Import a Codex export tarball into the HARD-PRO workspace and build the index.

Layout produced under ~/hardpro/workspace:
  codex/{sessions,skills,plugins,memories,rules,plans,automations,config.toml,...}
  projects/<name>
  skills/            (объединённый каталог скиллов, включая codex/skills)
  mcp/servers.json   (нормализованные MCP-серверы под Linux)
  index.sqlite       (проекты, сессии, FTS по переписке, скиллы, MCP)
  AGENTS.md          (общий контекст для агента)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("HP_ROOT", Path.home() / "hardpro"))
WORKSPACE = Path(os.environ.get("HP_WORKSPACE", ROOT / "workspace"))
FTS_CHARS = int(os.environ.get("HP_FTS_CHARS", "200000"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_extract(archive: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                log(f"  ! пропущен небезопасный путь: {member.name}")
                continue
            tar.extract(member, dest, set_attrs=False)
            count += 1
    return count


def dir_size(path: Path) -> tuple[int, int]:
    files = size = 0
    for cur, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            try:
                size += (Path(cur) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def read_front_matter(path: Path) -> tuple[dict[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[end + 4:]


def normalize_mcp(config: dict, platform_is_linux: bool = True) -> dict:
    servers: dict[str, dict] = {}
    raw = config.get("mcp_servers") or {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        entry: dict = {"disabled": False, "source": "codex"}
        url = spec.get("url")
        command = spec.get("command")
        if url:
            entry.update({"url": url, "command": None, "args": []})
            known = {".motionsites": "https://motionsites.ai/api/mcp"}
            if name in known:
                entry["url"] = known.pop("___", url)
            servers[name] = entry
            continue
        if not command:
            continue
        args = [str(a) for a in (spec.get("args") or [])]
        if command.lower().endswith(".exe") or command.startswith(("C:\\", "E:\\", "D:\\")):
            entry.update({"disabled": True, "reason": "windows-бинарь, нужен аналог на Linux",
                          "command": command, "args": args})
        elif command.lower() in {"npx", "node", "uvx", "python", "python3"}:
            entry.update({"command": command, "args": args})
        elif re.search(r"\b[Pp]ower[Ss]hell|\bps1\b", " ".join([command, *args])):
            entry.update({"disabled": True, "reason": "powershell-мост с Windows",
                          "command": command, "args": args})
        else:
            entry.update({"command": command, "args": args})
        env = spec.get("env")
        if isinstance(env, dict):
            entry["env"] = {str(k): str(v) for k, v in env.items()}
        if spec.get("cwd"):
            entry["cwd"] = spec["cwd"]
        servers[name] = entry
    # Linux-ready defaults that exist without extra hosting
    servers.setdefault("filesystem", {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", str(WORKSPACE)],
        "disabled": False, "source": "hardpro-default"})
    return {"servers": servers}


def session_record(path: Path) -> dict | None:
    stat = path.stat()
    mtime = stat.st_mtime
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})", path.name)
    if m:
        try:
            mtime = time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, -1))
        except (ValueError, OverflowError):
            pass
    lines = 0
    first_user = ""
    texts: list[str] = []
    budget = FTS_CHARS
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines += 1
                if not first_user or budget > 0:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    chunk = extract_text(obj)
                    if chunk:
                        if not first_user:
                            first_user = chunk[:400]
                        if budget > 0:
                            texts.append(chunk[:budget])
                            budget -= len(texts[-1])
    except OSError:
        return None
    return {"path": str(path.relative_to(WORKSPACE)), "size": stat.st_size,
            "lines": lines, "mtime": mtime, "first": first_user,
            "text": "\n".join(texts)}


def extract_text(obj, depth: int = 0) -> str:
    """Best-effort text pull from an unknown Codex session line shape."""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return " ".join(p for p in (extract_text(x, depth + 1) for x in obj) if p)
    if not isinstance(obj, dict):
        return ""
    for key in ("content", "text", "message", "prompt", "input", "output", "arguments"):
        if key in obj:
            val = obj[key]
            if isinstance(val, str) and val.strip():
                return val.strip()
            deep = extract_text(val, depth + 1)
            if deep:
                return deep
    for key in ("payload", "item", "data", "response", "request"):
        if key in obj:
            deep = extract_text(obj[key], depth + 1)
            if deep:
                return deep
    return ""


def build_index(manifest: dict, mcp_cfg: dict) -> dict:
    idx = WORKSPACE / "index.sqlite"
    idx.unlink(missing_ok=True)
    con = sqlite3.connect(idx)
    con.executescript("""
        create table projects(id integer primary key, name text, path text, files int, bytes int,
                              kind text, remote text);
        create table sessions(id integer primary key, path text, size int, lines int,
                              mtime real, first text);
        create table skills(id integer primary key, name text, description text, path text);
        create table mcp_servers(name text primary key, command text, args text,
                                 disabled int, note text);
        create virtual table session_fts using fts5(body, tokenize='unicode61');
    """)
    stats = {"projects": 0, "sessions": 0, "skills": 0, "mcp": 0}

    for proj in sorted((WORKSPACE / "projects").glob("*")) if (WORKSPACE / "projects").is_dir() else []:
        if not proj.is_dir():
            continue
        kind, remote = "tree", ""
        meta = proj / ".hardpro-remote.json"
        skipped = proj / ".hardpro-skipped.json"
        if meta.is_file():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                kind, remote = "git", data.get("remote", "")
            except (OSError, ValueError):
                pass
        if skipped.is_file():
            kind = "skipped"
        files, size = dir_size(proj)
        con.execute("insert into projects(name,path,files,bytes,kind,remote) values(?,?,?,?,?,?)",
                    (proj.name, str(proj), files, size, kind, remote))
        stats["projects"] += 1

    session_dirs = [WORKSPACE / "codex" / "sessions", WORKSPACE / "codex" / "archived_sessions"]
    session_files: list[Path] = []
    for sdir in session_dirs:
        if sdir.is_dir():
            session_files.extend(sorted(sdir.rglob("*.jsonl")))
    for sess in session_files:
        rec = session_record(sess)
        if not rec:
            continue
        cur = con.execute("insert into sessions(path,size,lines,mtime,first) values(?,?,?,?,?)",
                          (rec["path"], rec["size"], rec["lines"], rec["mtime"], rec["first"]))
        if rec["text"]:
            con.execute("insert into session_fts(rowid, body) values(?,?)", (cur.lastrowid, rec["text"]))
        stats["sessions"] += 1

    for path in sorted(WORKSPACE.rglob("SKILL.md")) if WORKSPACE.is_dir() else []:
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        meta, body = read_front_matter(path)
        name = meta.get("name") or path.parent.name
        desc = meta.get("description") or ""
        if not desc:
            m = re.search(r"^#\s+(.+)$", body, re.M)
            desc = m.group(1).strip() if m else ""
        con.execute("insert into skills(name,description,path) values(?,?,?)",
                    (name, desc[:400], str(path.relative_to(WORKSPACE))))
        stats["skills"] += 1

    for name, spec in (mcp_cfg.get("servers") or {}).items():
        con.execute("insert or replace into mcp_servers values(?,?,?,?,?)",
                    (name, spec.get("command") or spec.get("url") or "",
                     json.dumps(spec.get("args") or [], ensure_ascii=False),
                     1 if spec.get("disabled") else 0, spec.get("reason", "")))
        stats["mcp"] += 1

    con.commit()
    con.execute("insert into session_fts(session_fts) values('optimize')")
    con.commit()
    con.close()
    return stats


def write_agents_md(manifest: dict, stats: dict, mcp_cfg: dict) -> None:
    skills_top = []
    idx = WORKSPACE / "index.sqlite"
    if idx.is_file():
        con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
        skills_top = [r[0] for r in con.execute(
            "select name from skills order by name limit 40")]
        con.close()
    ready = [n for n, s in (mcp_cfg.get("servers") or {}).items() if not s.get("disabled")]
    blocked = [n for n, s in (mcp_cfg.get("servers") or {}).items() if s.get("disabled")]
    body = f"""# HARD-PRO workspace

Импорт Codex: {time.strftime('%Y-%m-%d %H:%M')} с машины {manifest.get('host', '?')}.

## Что здесь лежит

- `codex/` — chats, memories, rules, plans, plugins (источник: {manifest.get('codex_home', '?')})
- `projects/` — проекты из {manifest.get('projects_root', '?')}
- `skills/` — скиллы, индекс в `index.sqlite:skills` ({stats['skills']})
- `mcp/servers.json` — MCP-серверы, готовые к Linux ({len(ready)} активных, {len(blocked)} требуют порт)
- `index.sqlite` — проекты, сессии, полнотекстовый поиск по переписке (`session_fts`)

## Как искать контекст

    sqlite3 index.sqlite "select path,first from sessions order by mtime desc limit 20"
    sqlite3 index.sqlite "select snippet(session_fts,0,'[',']','…',12) from session_fts \\
        where session_fts match 'webui' limit 5"
    sqlite3 index.sqlite "select name,description from skills order by name"

## MCP без порта

{', '.join(ready) if ready else '—'}

## MCP, которые надо переписать под Linux

{', '.join(blocked) if blocked else '—'}

## Скиллы (первые 40)

{chr(10).join('- ' + s for s in skills_top) if skills_top else '—'}
"""
    (WORKSPACE / "AGENTS.md").write_text(body, encoding="utf-8")


def link_skills() -> None:
    target = WORKSPACE / "skills"
    source = WORKSPACE / "codex" / "skills"
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        dst = target / item.name
        if dst.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*SKIP_DIRS))
        else:
            shutil.copy2(item, dst)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: codex-import.py <archive.tgz>", file=sys.stderr)
        return 2
    archive = Path(sys.argv[1])
    if not archive.is_file():
        print(f"нет файла: {archive}", file=sys.stderr)
        return 2
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    log(f"распаковка {archive} -> {WORKSPACE}")
    count = safe_extract(archive, WORKSPACE)
    log(f"  извлечено объектов: {count}")

    manifest = {}
    mpath = WORKSPACE / "manifest.json"
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8-sig"))
        except ValueError:
            manifest = {}

    link_skills()

    cfg_path = WORKSPACE / "codex" / "config.toml"
    config: dict = {}
    if cfg_path.is_file():
        try:
            config = tomllib.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
            log(f"  config.toml: {len(config.get('mcp_servers') or {})} mcp, "
                f"{len(config.get('projects') or {})} project entries")
        except (tomllib.TOMLDecodeError, OSError) as exc:
            log(f"  ! config.toml не разобран: {exc}")

    mcp_cfg = normalize_mcp(config)
    (WORKSPACE / "mcp").mkdir(exist_ok=True)
    (WORKSPACE / "mcp" / "servers.json").write_text(
        json.dumps(mcp_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = build_index(manifest, mcp_cfg)
    write_agents_md(manifest, stats, mcp_cfg)
    log("итак: " + json.dumps(stats, ensure_ascii=False))
    log(f"workspace: {WORKSPACE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
