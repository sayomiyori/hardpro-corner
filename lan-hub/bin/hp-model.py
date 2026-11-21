#!/usr/bin/env python3
"""hp-model — переключатель моделей из терминала сервера.

    hp-model list
    hp-model status
    hp-model switch agent hardpro-abliterated-27b
    hp-model switch media Qwen3.8-Flash-Next
    hp-model role chat hardpro-manager
    hp-model start Qwen3.8-Flash-Next
    hp-model stop hardpro-media-vision
    hp-model check

Доска и агент дергают тот же модуль через HTTP (`/api/models`), так что
состояние всегда одно: файл каталога плюс state.json под flock.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_env(path: Path) -> None:
    """Подхватить hub.env, чтобы CLI работал без systemd-окружения."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), os.path.expandvars(value.strip().strip('"').strip("'")))


_load_env(Path.home() / "hardpro" / "hub" / "hub.env")

from models import registry  # noqa: E402


def show(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "list":
            st = registry.status()
            for m in st["models"]:
                mark = "●" if m["online"] else ("◐" if m["running"] else "○")
                gpu = ",".join(str(g) for g in m["gpus"]) or "-"
                roles = ",".join(m["roles"]) or "-"
                print(f"{mark} {m['id']:<28} gpu {gpu:<7} port {str(m['port'] or '-'):<6} "
                      f"roles {roles:<12} {m['label']}")
            return 0
        if cmd == "status":
            show(registry.status())
            return 0
        if cmd == "check":
            res = registry.switch_check()
            show(res)
            return 0 if res["ok"] else 1
        if cmd == "switch":
            role, model = rest[0], rest[1]
            start = "--no-start" not in rest
            show(registry.switch(role, model, start))
            return 0
        if cmd == "role":
            show(registry.set_role(rest[0], rest[1]))
            return 0
        if cmd == "start":
            show(registry.launch(rest[0]))
            return 0
        if cmd == "stop":
            show(registry.stop(rest[0]))
            return 0
        if cmd == "autostart":
            out = []
            for mid, prof in registry.profiles().items():
                if prof.get("backend") != "llamacpp" or not prof.get("autostart"):
                    continue
                try:
                    out.append(registry.launch(mid))
                except registry.RegistryError as exc:
                    out.append({"ok": False, "model": mid, "error": str(exc)})
            show(out)
            return 0
        print(f"неизвестная команда: {cmd}", file=sys.stderr)
        return 2
    except registry.RegistryError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    except IndexError:
        print("не хватает аргументов, смотри hp-model --help", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
