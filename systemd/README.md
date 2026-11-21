# Systemd-юниты HARD-PRO LLM (Ubuntu, hp_local_llm)

Всё живёт в user-режиме systemd (`~/.config/systemd/user/`), linger для
`hp_local_llm` включён — юниты поднимаются на буте без логина.

## Карта карт

| GPU | PCI | Карта | UUID | Движок |
| --- | --- | --- | --- | --- |
| GPU 0 | 81:00.0 | Palit RTX 3090 | `GPU-e90cf1fc-81b5-35eb-7366-54f3b9c9afc3` | vLLM manager (Qwen3-14B-AWQ) |
| GPU 1 | c1:00.0 | ASUS RTX 3090 | `GPU-f9246b7a-b212-935f-4735-b3a0e68bbb24` | coding llama.cpp (27B Q5_K) |

Дофиксное состояние: оба юнита были запины в один UUID (GPU 1). coding-llama
занимает ~20.5 ГБ из 24, vLLM с `--gpu-memory-utilization 0.92` в OOM,
3 рестарта за 5 минут — и unit оставался в failed на каждый бут.
Фикс — drop-in `70-gpu-pin.conf`: vLLM навсегда на GPU 0, coding-llama на GPU 1.

## Юниты и автозапуск

| Unit | Порт | GPU | Автозапуск | Кто грузит |
| --- | --- | --- | --- | --- |
| hardpro-openwebui.service | 127.0.0.1:8765 | — | enabled | systemd |
| hardpro-gateway.service | 192.0.2.10:8775 | — | enabled | systemd (единственный LAN-вход) |
| hardpro-vllm.service | 127.0.0.1:8000 | GPU 0 | enabled | systemd |
| hardpro-coding (llama-роутер) | 127.0.0.1:8081 | обе, по очереди | enabled | пресет из 3 моделей, `--models-max 1` |
| hardpro-media-vision.service | 127.0.0.1:8083 | CPU | enabled | systemd + ffmpeg для видео |
| hardpro-flowise.service | 127.0.0.1:3000 | CPU | enabled | systemd |
| hardpro-hub.service | 127.0.0.1:9443 | — | enabled | systemd |
| hardpro-agent.service | 127.0.0.1:9444 | — | enabled | systemd |
| hardpro-catalog.service | 127.0.0.1:8767 | — | enabled | systemd |
| hardpro-widget.service | 127.0.0.1:8766 | — | enabled | systemd |
| hardpro-trace.service | 127.0.0.1:9090 | — | enabled | systemd |
| hardpro-onec-link.service | — | — | enabled | systemd (SSH-линк до SpaceWeb) |
| hardpro-stage-link.service | — | — | enabled | systemd (SSH-линк до staging) |
| hardpro-comfyui.service | 127.0.0.1:8188 | GPU | **manual** | руками — см. ниже |

## Почему ComfyUI не в автозапуске

Обе карты заняты под завязку на буте: vLLM берёт 0.92 × 24 ГБ ≈ 21.8 ГБ
(GPU 0), coding-llama ~20.5 ГБ (GPU 1). Свободной VRAM-полосы для ComfyUI
при старте нет — если включить его в default.target, он задушит либо vLLM,
либо coding-llama. Запуск вручную в окно, когда один из движков не нужен.

LAN-сокеты (`hardpro-team-webui-lan.socket`, `hardpro-team-comfy-lan.socket`,
`hardpro-owner-comfy-lan.socket`) оставлены disabled: внешний доступ идёт
через gateway на 8775, сокеты — точечные гейты для отдельных сценариев.

## Роутер чат-моделей (21.11.2026)

`hardpro-coding` переведён на роутер: пресет `qwen38-models.ini` из трёх
моделей (Qwen3.8-Flash-Next, две 27B Q5), `--models-max 1` — в памяти одна,
запрос к другой автоматически выгружает текущую и грузит нужную.
Tensor override выгружает экспертов Flash-Next в ОЗУ, поэтому модель живёт
в связке двух 3090 рядом с vLLM и медиа-юнитом. Обе карты видны юниту через
`CUDA_VISIBLE_DEVICES` в `hardpro-coding.service.d/95-router.conf`.

Установка:

```bash
cp systemd/hardpro-coding.service.d/95-router.conf ~/.config/systemd/user/hardpro-coding.service.d/
cp systemd/qwen38-models.ini /srv/models/hard-pro-content/coding/work/
cp systemd/hardpro-media-vision.service.d/40-ffmpeg.conf ~/.config/systemd/user/hardpro-media-vision.service.d/
systemctl --user daemon-reload
systemctl --user restart hardpro-coding.service hardpro-media-vision.service
```

Проверено контрольной генерацией по всем трём моделям со сменой (5,5–12,9 с
на ответ, до 90 с на первый токен после смены Flash-Next). Детали по RAG,
видео и голосу — в `docs/chat-model-stack.md`.

## Установка фикса

```bash
./install-autostart.sh
```

Скрипт копирует drop-in в `~/.config/systemd/user/hardpro-vllm.service.d/`,
включает flowise/media-vision, делает daemon-reload, сбрасывает failed-счётчик
и перезапускает vLLM. Идемпотентный — можно гонять повторно.

## Диагностика

```bash
# состояние всех юнитов
systemctl --user list-units 'hardpro-*' --no-pager

# почему vLLM упал (OOM виден в трейсах EngineCore)
journalctl --user -u hardpro-vllm -n 50 --no-pager

# кто сколько VRAM держит
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```
