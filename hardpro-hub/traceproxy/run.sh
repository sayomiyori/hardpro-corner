#!/usr/bin/env bash
# Запуск trace-прокси для конкретного порта.
#   run.sh 9081  -> upstream 127.0.0.1:8081 (Qwen3.8 Flash Next)
#   run.sh 9083  -> upstream 127.0.0.1:8083 (HARD-PRO Медиа)
#   run.sh 9067  -> upstream 127.0.0.1:8767/manager (каталог 1С)
set -euo pipefail
PORT="${1:?нужен порт}"
ROOT="${HP_ROOT:-$HOME/hardpro}"
PY="${PY:-$(command -v python3)}"

case "$PORT" in
  9081) UP="http://127.0.0.1:8081"; LABEL="Qwen3.8 Flash Next" ;;
  9083) UP="http://127.0.0.1:8083"; LABEL="HARD-PRO Медиа" ;;
  9067) UP="http://127.0.0.1:8767/manager"; LABEL="hardpro-manager" ;;
  *) echo "неизвестный порт $PORT" >&2; exit 2 ;;
esac

cd "$ROOT"
export TP_UPSTREAM="$UP" TP_LABEL="$LABEL" TP_TAIL="1"
exec "$PY" -m uvicorn traceproxy.trace_proxy:app --host 127.0.0.1 --port "$PORT" --no-access-log
