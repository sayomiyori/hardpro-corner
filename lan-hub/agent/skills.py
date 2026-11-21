"""Skills index: every SKILL.md under workspace/skills becomes a callable card."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

WORKSPACE = Path(os.environ.get("HP_WORKSPACE", Path.home() / "hardpro" / "workspace"))
SKILLS_DIR = WORKSPACE / "skills"
MAX_BODY = 24000

_cache: dict[str, dict[str, str]] = {}
_loaded = False


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end]
    body = text[end + 4:]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, body.lstrip("\n")


def load() -> None:
    global _loaded
    _cache.clear()
    _loaded = True
    if not SKILLS_DIR.is_dir():
        return
    for path in SKILLS_DIR.rglob("SKILL.md"):
        rel = path.relative_to(SKILLS_DIR)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = _front_matter(text)
        name = meta.get("name") or rel.parent.name or rel.stem
        desc = meta.get("description") or ""
        if not desc:
            m = re.search(r"^#\s+(.+)$", body, re.M)
            desc = m.group(1).strip() if m else ""
        _cache[name] = {"name": name, "description": desc[:400],
                        "path": str(path), "body": body[:MAX_BODY]}


def ensure() -> None:
    if not _loaded:
        load()


def index() -> list[dict[str, str]]:
    ensure()
    return sorted(({"name": v["name"], "description": v["description"]}
                   for v in _cache.values()), key=lambda x: x["name"])


def read(name: str) -> str:
    ensure()
    item = _cache.get(name)
    if not item:
        low = name.lower()
        for key, val in _cache.items():
            if key.lower() == low:
                item = val
                break
    return item["body"] if item else ""


def prompt_block(limit: int = 80) -> str:
    items = index()[:limit]
    if not items:
        return ""
    lines = ["Доступные скиллы (читай телом через skill_read, если нужен):"]
    for it in items:
        lines.append(f"- {it['name']}: {it['description']}")
    return "\n".join(lines)


def write_json(path: Path) -> None:
    ensure()
    path.write_text(json.dumps(index(), ensure_ascii=False, indent=2), encoding="utf-8")
