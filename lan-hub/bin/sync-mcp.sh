#!/usr/bin/env bash
# Собрать ~/hardpro/agent/mcp.json из workspace/mcp/servers.json + локальные дефолты.
set -euo pipefail
ROOT="${HP_ROOT:-$HOME/hardpro}"
WS="$ROOT/workspace"
SRC="$WS/mcp/servers.json"
DST="$ROOT/agent/mcp.json"
PY="${PY:-$(command -v python3)}"

export PATH="$ROOT/tools/node/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

"$PY" - "$SRC" "$DST" "$WS" <<'PYEOF'
import json, os, shutil, sys
src, dst, ws = sys.argv[1], sys.argv[2], sys.argv[3]
servers = {}
try:
    servers = (json.load(open(src, encoding="utf-8")).get("servers") or {})
except (OSError, ValueError):
    servers = {}

out = {"servers": {}}
skipped = []
for name, spec in servers.items():
    if spec.get("disabled") or not spec.get("command"):
        skipped.append(f"{name}: {spec.get('reason') or 'выключен'}")
        continue
    cmd = spec["command"]
    if shutil.which(cmd) is None:
        skipped.append(f"{name}: нет бинаря {cmd}")
        continue
    out["servers"][name] = {"command": cmd, "args": spec.get("args") or [],
                            "env": spec.get("env") or {}}

# локальные дефолты, которые работают без внешней инфраструктуры
defs = {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", ws]},
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
}
if shutil.which("npx"):
    for name, spec in defs.items():
        out["servers"].setdefault(name, spec)
else:
    skipped.append("filesystem/memory: нет npx (node)")

json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("mcp активны:", ", ".join(out["servers"]) or "нет")
for item in skipped:
    print("  -", item)
PYEOF

if systemctl --user is-active hardpro-agent.service >/dev/null 2>&1; then
  systemctl --user restart hardpro-agent.service
  echo "agent перезапущен"
fi
