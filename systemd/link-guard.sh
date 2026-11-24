#!/usr/bin/env bash
# Проверяет канал к читателю 1С и перезапускает SSH-звено, если оно умерло молча.
# Запускается таймером hardpro-link-guard.timer каждые три минуты.
set -u

CFG=${ONEC_READER_CONFIG:-$HOME/.config/hardpro-onec-link/reader.json}
PY=$HOME/.local/share/hardpro/vllm/bin/python

status=$("$PY" - "$CFG" <<'PY'
import json, pathlib, socket, ssl, sys
cfg = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
ctx = ssl.create_default_context(cafile=cfg["ca"])
ctx.load_cert_chain(cfg["cert"], cfg["key"])
try:
    with socket.create_connection((cfg["host"], int(cfg["port"])), timeout=6) as sock:
        tls = ctx.wrap_socket(sock, server_hostname=cfg["host"])
        tls.settimeout(4)
        tls.sendall(b"GET /v1/catalog?after= HTTP/1.1\r\nHost: " + cfg["host"].encode() +
                    b"\r\nAccept: application/json\r\nConnection: close\r\n\r\n")
        head = tls.recv(64)
    if head.startswith(b"HTTP/1.1 200"):
        print("ok")
    elif head.startswith(b"HTTP/1."):
        # Читатель жив, ответил ошибкой: 1С ещё не готова. Туннель трогать не нужно.
        print("reader-up")
    else:
        print("down")
except Exception:
    print("down")
PY
)

case "$status" in
  ok)
    exit 0
    ;;
  reader-up)
    logger -t hardpro-link-guard "туннель жив, читатель ответил ошибкой: ждём 1С"
    exit 0
    ;;
esac

logger -t hardpro-link-guard "читатель 1С не ответил, перезапускаю hardpro-onec-link"
systemctl --user restart hardpro-onec-link.service
sleep 5
systemctl --user is-active hardpro-onec-link.service >/dev/null || \
  logger -t hardpro-link-guard "hardpro-onec-link не поднялся"
