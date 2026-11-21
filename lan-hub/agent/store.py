"""Session store for the agent daemon: chats, turns, tool traces."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("HP_ROOT", Path.home() / "hardpro"))
DB = ROOT / "agent" / "sessions.sqlite"


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '', extra TEXT NOT NULL DEFAULT '{}',
        created REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS msg_session ON messages(session_id, id)")
    con.commit()
    return con


def new_session(model: str, title: str = "") -> str:
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    con = _con()
    con.execute("insert into sessions values(?,?,?,?,?)", (sid, title, model, now, now))
    con.commit()
    con.close()
    return sid


def touch(session_id: str, title: str | None = None) -> None:
    con = _con()
    con.execute("update sessions set updated=? where id=?", (time.time(), session_id))
    if title:
        con.execute("update sessions set title=? where id=? and (title='' or title is null)",
                    (title[:120], session_id))
    con.commit()
    con.close()


def append(session_id: str, role: str, content: str, extra: dict | None = None) -> None:
    con = _con()
    con.execute("insert into messages(session_id, role, content, extra, created) values(?,?,?,?,?)",
                (session_id, role, content, json.dumps(extra or {}, ensure_ascii=False), time.time()))
    con.commit()
    con.close()


def history(session_id: str, limit: int = 40) -> list[dict]:
    con = _con()
    rows = con.execute("select role, content, extra from messages where session_id=? "
                       "order by id desc limit ?", (session_id, limit)).fetchall()
    con.close()
    out: list[dict] = []
    for row in reversed(rows):
        extra = json.loads(row["extra"] or "{}")
        msg = {"role": row["role"], "content": row["content"]}
        if extra.get("tool_calls"):
            msg["tool_calls"] = extra["tool_calls"]
        if extra.get("tool_call_id"):
            msg["tool_call_id"] = extra["tool_call_id"]
        if extra.get("name"):
            msg["name"] = extra["name"]
        out.append(msg)
    return out


def sessions(limit: int = 30) -> list[dict]:
    con = _con()
    rows = con.execute("select id, title, model, created, updated from sessions "
                       "order by updated desc limit ?", (limit,)).fetchall()
    con.close()
    return [{"id": r["id"], "title": r["title"] or "(без названия)", "model": r["model"],
             "updated": r["updated"]} for r in rows]


def messages(session_id: str, limit: int = 200) -> list[dict]:
    con = _con()
    rows = con.execute("select role, content, extra, created from messages where session_id=? "
                       "order by id limit ?", (session_id, limit)).fetchall()
    con.close()
    return [{"role": r["role"], "content": r["content"], "extra": json.loads(r["extra"] or "{}"),
             "created": r["created"]} for r in rows]


def drop(session_id: str) -> None:
    con = _con()
    con.execute("delete from messages where session_id=?", (session_id,))
    con.execute("delete from sessions where id=?", (session_id,))
    con.commit()
    con.close()
