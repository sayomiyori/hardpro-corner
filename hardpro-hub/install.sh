#!/usr/bin/env bash
# HARD-PRO LAN hub installer. Idempotent. Run from the repo dir: ./install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HOME/hardpro"
WS="$ROOT/workspace"
LAN_IP="${LAN_IP:-192.168.88.202}"
HUB_PORT="${HUB_PORT:-8443}"
AGENT_PORT="${AGENT_PORT:-8444}"
DOMAIN="${DOMAIN:-hardpro.lan}"
UNIT_DIR="$HOME/.config/systemd/user"

say() { printf '%s\n' "$*"; }

say "== HARD-PRO hub install =="
mkdir -p "$ROOT"/{hub/tls,agent,workspace,bin,incoming,logs,tools,models}

# --- files ---
cp "$SRC/hub/app.py" "$ROOT/hub/app.py"
cp "$SRC/hub/style.css" "$ROOT/hub/style.css" 2>/dev/null || true
cp "$SRC/agent/"*.py "$ROOT/agent/"
cp "$SRC/agent/ui.html" "$ROOT/agent/ui.html"
mkdir -p "$ROOT/models"
cp "$SRC/models/"*.py "$ROOT/models/"
# каталог моделей — рабочий файл, чужую раскладку не затираем
[ -s "$ROOT/models/profiles.json" ] || cp "$SRC/models/profiles.json" "$ROOT/models/profiles.json"
cp "$SRC/gateway/gateway.py" "$ROOT/gateway/gateway.py" 2>/dev/null || {
  mkdir -p "$ROOT/gateway"; cp "$SRC/gateway/gateway.py" "$ROOT/gateway/gateway.py"; }
cp "$SRC/migrate/codex-import.py" "$ROOT/bin/codex-import.py"
cp "$SRC/bin/hp-search.py" "$ROOT/bin/hp-search.py"
cp "$SRC/bin/hp-model.py" "$ROOT/bin/hp-model.py"
cp "$SRC/bin/sync-mcp.sh" "$ROOT/bin/sync-mcp.sh"
chmod +x "$ROOT/bin/codex-import.py" "$ROOT/bin/hp-search.py" "$ROOT/bin/sync-mcp.sh" \
         "$ROOT/bin/hp-model.py"

# --- python ---
PY=""
if [ -x "$ROOT/venv/bin/python" ] && "$ROOT/venv/bin/python" -c 'import fastapi' 2>/dev/null; then
  PY="$ROOT/venv/bin/python"
else
  if python3 -m venv "$ROOT/venv" >/dev/null 2>&1; then
    "$ROOT/venv/bin/pip" -q install --upgrade pip >/dev/null 2>&1 || true
    if "$ROOT/venv/bin/pip" -q install fastapi uvicorn httpx >/dev/null 2>&1; then
      PY="$ROOT/venv/bin/python"
    fi
  fi
fi
if [ -z "$PY" ]; then
  FALLBACK="$HOME/.local/share/hardpro/vllm/bin/python"
  if [ -x "$FALLBACK" ] && "$FALLBACK" -c 'import fastapi, uvicorn, httpx' 2>/dev/null; then
    PY="$FALLBACK"
  fi
fi
[ -n "$PY" ] || { say "нет python с fastapi/uvicorn/httpx"; exit 1; }
say "python: $PY"

# --- node (нужен для MCP-серверов) ---
NODE_BIN=""
if command -v node >/dev/null 2>&1; then
  NODE_BIN="$(command -v node)"
elif [ -x "$ROOT/tools/node/bin/node" ]; then
  NODE_BIN="$ROOT/tools/node/bin/node"
else
  VER="v22.14.0"
  URL="https://nodejs.org/dist/$VER/node-$VER-linux-x64.tar.xz"
  if curl -fsSL --max-time 180 -o /tmp/node.tar.xz "$URL" 2>/dev/null; then
    tar -xJf /tmp/node.tar.xz -C "$ROOT/tools" 2>/dev/null && \
      mv "$ROOT/tools/node-$VER-linux-x64" "$ROOT/tools/node" 2>/dev/null || true
    rm -f /tmp/node.tar.xz
    [ -x "$ROOT/tools/node/bin/node" ] && NODE_BIN="$ROOT/tools/node/bin/node"
  fi
fi
if [ -n "$NODE_BIN" ]; then
  export PATH="$(dirname "$NODE_BIN"):$PATH"
  say "node: $NODE_BIN ($("$NODE_BIN" -v 2>/dev/null))"
else
  say "node: нет (MCP через npx будет пропущен)"
fi

# --- tls ---
CRT="$ROOT/hub/tls/cert.pem"; KEY="$ROOT/hub/tls/key.pem"
if [ ! -s "$CRT" ] || [ ! -s "$KEY" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CRT" -subj "/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN,DNS:hub.$DOMAIN,DNS:agent.$DOMAIN,DNS:chat.$DOMAIN,IP:$LAN_IP" \
    >/dev/null 2>&1
  chmod 600 "$KEY"; chmod 644 "$CRT"
  say "сертификат: $CRT"
fi

# --- env ---
ENV_FILE="$ROOT/hub/hub.env"
if [ ! -s "$ENV_FILE" ]; then
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-18)"
  MANAGER_TOKEN="$(grep -h '^MANAGER_TOKEN=' "$HOME/.local/share/hardpro/catalog-web/catalog.env" 2>/dev/null | cut -d= -f2- || true)"
  cat > "$ENV_FILE" <<EOF
HUB_USER=hardpro
HUB_PASS=$PASS
CATALOG_MANAGER_TOKEN=$MANAGER_TOKEN
HP_ROOT=$ROOT
HP_WORKSPACE=$WS
HP_MODEL_BASE=http://127.0.0.1:8081
HP_MODEL_NAME=Qwen3.8-Flash-Next
HP_MODELS_DIR=$ROOT/models
HP_LLAMA_BIN=$ROOT/bin/llama-server
HP_AUTO_START=1
HP_MAX_STEPS=24
HP_MAX_TOKENS=3072
HP_TEMPERATURE=0.6
EOF
  chmod 600 "$ENV_FILE"
  {
    echo "HARD-PRO LAN hub"
    echo "user: hardpro"
    echo "pass: $PASS"
    echo "board:  https://$DOMAIN:$HUB_PORT/  (или https://$LAN_IP:$HUB_PORT/)"
    echo "agent:  https://$DOMAIN:$AGENT_PORT/"
  } > "$ROOT/HUB-CREDENTIALS.txt"
  chmod 600 "$ROOT/HUB-CREDENTIALS.txt"
  say "креды: $ROOT/HUB-CREDENTIALS.txt"
fi
# ключи переключателя моделей дописываются в старый env, не затирая рабочий
ensure_env() { grep -q "^$1=" "$ENV_FILE" || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }
ensure_env HP_MODELS_DIR "$ROOT/models"
ensure_env HP_LLAMA_BIN "$ROOT/bin/llama-server"
ensure_env HP_AUTO_START 1

set -a; . "$ENV_FILE"; set +a

# --- mcp config for the agent ---
mkdir -p "$ROOT/agent"
PY="$PY" "$ROOT/bin/sync-mcp.sh" || say "sync-mcp пропущен"

# --- units ---
mkdir -p "$UNIT_DIR"

# 8775 — единственный порт, открытый снаружи; его держит шлюз
systemctl --user disable --now hardpro-team-webui-lan.socket hardpro-team-webui-lan.service >/dev/null 2>&1 || true

cat > "$UNIT_DIR/hardpro-hub.service" <<EOF
[Unit]
Description=HARD-PRO LAN model board
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
Environment=PATH=$(dirname "$NODE_BIN"):/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m uvicorn hub.app:app --host 127.0.0.1 --port 9443 --no-access-log
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/hardpro-agent.service" <<EOF
[Unit]
Description=HARD-PRO agent daemon (tools, MCP, terminal)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
Environment=PATH=$(dirname "$NODE_BIN"):/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m uvicorn agent.app:app --host 127.0.0.1 --port 9444 --no-access-log
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/hardpro-gateway.service" <<EOF
[Unit]
Description=HARD-PRO single LAN entry on 8775 (hub, agent, Open WebUI)
After=network-online.target hardpro-hub.service hardpro-agent.service
Wants=hardpro-hub.service hardpro-agent.service

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
Environment=GW_HOST=$LAN_IP
Environment=GW_PORT=8775
Environment=GW_HUB_PORT=9443
Environment=GW_AGENT_PORT=9444
Environment=GW_WEBUI_PORT=8775
ExecStart=$PY $ROOT/gateway/gateway.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/hardpro-models.service" <<EOF
[Unit]
Description=HARD-PRO model switchboard boot loader (autostart roles)
After=network-online.target
ConditionPathIsMountPoint=/srv/models

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
ExecStart=$PY $ROOT/bin/hp-model.py autostart
TimeoutStartSec=900
KillMode=process

[Install]
WantedBy=default.target
EOF

# старые юниты моделей уступают порты 8081/8083 каталогу профилей
systemctl --user disable --now hardpro-coding.service hardpro-media-vision.service >/dev/null 2>&1 || true

systemctl --user daemon-reload
systemctl --user enable hardpro-models.service >/dev/null 2>&1 || true
systemctl --user enable hardpro-hub.service hardpro-agent.service hardpro-gateway.service >/dev/null 2>&1 || true
systemctl --user restart hardpro-hub.service
systemctl --user restart hardpro-agent.service
systemctl --user restart hardpro-gateway.service
sleep 3

say "== состояние =="
systemctl --user is-active hardpro-hub.service hardpro-agent.service hardpro-gateway.service || true
say "== проверка =="
curl -s -u "$HUB_USER:$HUB_PASS" "http://127.0.0.1:9443/healthz" || say "hub не ответил"
echo
curl -s -u "$HUB_USER:$HUB_PASS" "http://127.0.0.1:9444/healthz" || say "agent не ответил"
echo
curl -s -o /dev/null -w 'вход /hub/  -> %{http_code}\n' -u "$HUB_USER:$HUB_PASS" "http://$LAN_IP:8775/hub/"
curl -s -o /dev/null -w 'вход /agent/ -> %{http_code}\n' -u "$HUB_USER:$HUB_PASS" "http://$LAN_IP:8775/agent/"
curl -s -o /dev/null -w 'вход /      -> %{http_code}\n' "http://$LAN_IP:8775/"
say "доска: http://$LAN_IP:8775/hub/   агент: http://$LAN_IP:8775/agent/   чат: http://$LAN_IP:8775/"
