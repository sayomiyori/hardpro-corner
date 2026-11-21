#!/bin/bash
# Откат 27B к сохранённому профилю каталога моделей.
# Использование: rollback.sh [файл-профиля]   (по умолчанию последний .bak.*)
set -e
HP="${HP_PYTHON:-$HOME/.local/share/hardpro/vllm/bin/python}"
PF="${HP_MODELS_FILE:-$HOME/hardpro/models/profiles.json}"
MODEL_ID="hardpro-abliterated-27b"
PORT="8084"
SRC="${1:-$(ls -t "$PF".bak.* 2>/dev/null | head -1)}"
if [ -z "$SRC" ]; then echo "нет бэкапов $PF.bak.*"; exit 1; fi
cp "$PF" "$PF.before-rollback.$(date +%s)"
cp "$SRC" "$PF"
"$HP" "$HOME/hardpro/bin/hp-model.py" stop "$MODEL_ID"
"$HP" "$HOME/hardpro/bin/hp-model.py" launch "$MODEL_ID"
curl -s -m 5 "http://127.0.0.1:$PORT/health"; echo
curl -s -m 5 "http://127.0.0.1:$PORT/slots"; echo
echo "rollback done from: $SRC"
