#!/usr/bin/env bash
# HARD-PRO autostart fix: vLLM pinned to GPU 0, flowise + media-vision enabled.
# Idempotent - safe to re-run. Run as the service user (hp_local_llm).

set -euo pipefail

UNIT_DIR="${HOME}/.config/systemd/user"
DROPIN_DIR="${UNIT_DIR}/hardpro-vllm.service.d"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -un)" != "hp_local_llm" ]; then
    echo "запускай от hp_local_llm, не от $(id -un)" >&2
    exit 1
fi

# linger: user-юниты должны подниматься на буте без логина
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
    echo "linger выключен - включи: sudo loginctl enable-linger $USER" >&2
    exit 1
fi

# 1. drop-in: vLLM на GPU 0 (Palit, PCI 81:00.0), coding-llama остаётся на GPU 1
mkdir -p "$DROPIN_DIR"
install -m 0644 "$SRC_DIR/hardpro-vllm.service.d/70-gpu-pin.conf" \
    "$DROPIN_DIR/70-gpu-pin.conf"

# 2. CPU-only юниты в автозапуск
systemctl --user enable hardpro-flowise.service hardpro-media-vision.service

# 3. перечитать юниты, сбросить failed-счётчик, поднять
systemctl --user daemon-reload
systemctl --user reset-failed hardpro-vllm.service || true
systemctl --user start hardpro-flowise.service hardpro-media-vision.service
systemctl --user restart hardpro-vllm.service

# 4. проверка: vLLM грузит Qwen3-14B-AWQ ~60-90 cек, ждём порт
echo "ждём vLLM на 127.0.0.1:8000 (модель грузится до полутора минут)..."
for _ in $(seq 1 60); do
    if ss -tln 2>/dev/null | grep -q '127.0.0.1:8000 '; then
        echo "vLLM поднялся:"
        ss -tln | grep '127.0.0.1:8000 '
        exit 0
    fi
    sleep 5
done
echo "vLLM не встал за 300 cек - смотри: journalctl --user -u hardpro-vllm -n 50" >&2
exit 1
