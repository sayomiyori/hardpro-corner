#!/usr/bin/env python3
"""Поиск по импортированному контексту Codex без sqlite3-CLI.

  hp-search.py search "webui 8776"     полнотекст по переписке
  hp-search.py sessions --limit 20     последние сессии
  hp-search.py skills [--grep web]     каталог скиллов
  hp-search.py mcp                     MCP-серверы и их состояние
  hp-search.py projects                проекты с размерами
  hp-search.py show <path>             первые строки сессии
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HP_ROOT", Path.home() / "hardpro"))
WS = Path(os.environ.get("HP_WORKSPACE", ROOT / "workspace"))
IDX = WS / "index.sqlite"


def con() -> sqlite3.Connection:
    if not IDX.is_file():
        sys.exit(f"нет индекса: {IDX} — сначала codex-import.py")
    c = sqlite3.connect(f"file:{IDX}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def human(n: int | None) -> str:
    val = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024:
            return f"{val:.0f}{unit}"
        val /= 1024
    return f"{val:.1f}PB"


def fts_query(raw: str) -> str:
    """FTS5-safe запрос: только слова, каждое в кавычках (AND по умолчанию)."""
    words = re.findall(r"\w+", raw, re.UNICODE)
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words)


def cmd_search(args) -> None:
    c = con()
    rows = c.execute(
        "select s.path, s.mtime, snippet(session_fts, 0, '[', ']', ' … ', 14) as frag "
        "from session_fts join sessions s on s.id = session_fts.rowid "
        "where session_fts match ? order by s.mtime desc limit ?",
        (fts_query(args.query), args.limit)
    ).fetchall()
    for r in rows:
        stamp = __import__("datetime").datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
        print(f"--- {stamp}  {r['path']}")
        print("   ", r["frag"].replace("\n", " ")[:500])
    if not rows:
        print("ничего не найдено")
    c.close()


def cmd_sessions(args) -> None:
    c = con()
    rows = c.execute("select path, size, lines, mtime, first from sessions "
                     "order by mtime desc limit ?", (args.limit,)).fetchall()
    for r in rows:
        stamp = __import__("datetime").datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
        print(f"{stamp}  {human(r['size']):>8}  {r['lines']:>6} стр  {r['path']}")
        if r["first"]:
            print("     ", r["first"].replace("\n", " ")[:160])
    c.close()


def cmd_skills(args) -> None:
    c = con()
    sql = "select name, description, path from skills"
    params: tuple = ()
    if args.grep:
        sql += " where name like ? or description like ?"
        params = (f"%{args.grep}%", f"%{args.grep}%")
    for r in c.execute(sql + " order by name", params):
        print(f"{r['name']:<38} {r['description'][:90]}")
    c.close()


def cmd_mcp(_: argparse.Namespace) -> None:
    c = con()
    for r in c.execute("select name, command, args, disabled, note from mcp_servers order by name"):
        if r["command"].startswith("http"):
            state = "http, нужен транспорт"
        elif r["disabled"]:
            state = "нет на Linux"
        else:
            state = "готов"
        print(f"{r['name']:<18} {state:<19} {r['command']} {r['args'][:50]} {r['note'] or ''}")
    c.close()


def cmd_projects(_: argparse.Namespace) -> None:
    c = con()
    for r in c.execute("select name, files, bytes, kind, remote from projects order by name"):
        print(f"{r['name']:<44} {r['kind']:<8} {human(r['bytes']):>8} "
              f"{r['files']:>5} файлов  {r['remote'] or ''}")
    c.close()


def cmd_show(args) -> None:
    path = WS / args.path
    if not path.is_file():
        sys.exit(f"нет файла: {path}")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= args.lines:
                break
            print(line.rstrip()[:400])


def main() -> int:
    ap = argparse.ArgumentParser(prog="hp-search", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("sessions")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("skills")
    s.add_argument("--grep")
    s.set_defaults(fn=cmd_skills)

    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    sub.add_parser("projects").set_defaults(fn=cmd_projects)

    s = sub.add_parser("show")
    s.add_argument("path")
    s.add_argument("--lines", type=int, default=20)
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
