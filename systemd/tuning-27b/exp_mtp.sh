#!/bin/bash
# Эксперимент: включить speculative decoding (draft-mtp) на 27B и перезапустить
# только этот сервис штатным переключателем моделей (hp-model stop + launch).
# Использование: exp_mtp.sh <spec-type> [n_max]
#   exp_mtp.sh draft-mtp 2     # MTP-голова из GGUF, черновик до 2 токенов
#   exp_mtp.sh none            # вернуться к обычному декоду
# Требования: в GGUF есть nextn-тензоры (arch qwen35, nextn_predict_layers=1),
# сборка llama.cpp поддерживает --spec-type draft-mtp (проверить --help).
# Перед рестартом убедись, что сервер простаивает: curl .../slots -> is_processing=false.
set -e
TAG="${1:?spec-type, например draft-mtp}"
NMAX="${2:-2}"
HP="${HP_PYTHON:-$HOME/.local/share/hardpro/vllm/bin/python}"
export HP_MODELS_FILE="${HP_MODELS_FILE:-$HOME/hardpro/models/profiles.json}"
MODEL_ID="hardpro-abliterated-27b"
PORT="8084"

cp "$HP_MODELS_FILE" "$HP_MODELS_FILE.bak.$(date +%s)"

# правка каталога: n_ctx 65536 + spec-флаги (idempotent: args собираем заново)
"$HP" - "$TAG" "$NMAX" "$MODEL_ID" <<'PYEOF'
import json, os, sys
tag, nmax, mid = sys.argv[1], sys.argv[2], sys.argv[3]
path = os.environ["HP_MODELS_FILE"]
with open(path, encoding="utf-8") as fh:
    cat = json.load(fh)
p = cat["profiles"][mid]
p["n_ctx"] = 65536
base = ["--alias", p["model_path"].rsplit("/", 1)[-1][:-5],
        "--jinja", "--no-webui", "--offline",
        "--flash-attn", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--threads", "16", "--parallel", "1"]
if tag != "none":
    base += ["--spec-type", tag]
    if tag == "draft-mtp":
        base += ["--spec-draft-n-max", str(nmax)]
p["args"] = base
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(cat, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("profile updated:", tag, nmax)
PYEOF

# рестарт только 27B штатным способом
"$HP" "$HOME/hardpro/bin/hp-model.py" stop "$MODEL_ID"
"$HP" "$HOME/hardpro/bin/hp-model.py" launch "$MODEL_ID"

# проверка
echo "=== health/slots ==="
curl -s -m 5 "http://127.0.0.1:$PORT/health"; echo
curl -s -m 5 "http://127.0.0.1:$PORT/slots"; echo
