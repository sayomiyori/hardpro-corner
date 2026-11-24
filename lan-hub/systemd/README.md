# Systemd ранней версии LAN Hub

Этот каталог описывает первый вариант раздельного закрепления RTX 3090, где vLLM занимал GPU 0, а llama.cpp — GPU 1. Две модели на одном GPU приводили к OOM при запуске. Команды и конфигурации текущего стенда приведены в [основном руководстве systemd](../../systemd/README.md), результаты сверки — в [статусе проекта](../../STATUS.md).

## Размещение моделей

| Устройство | Назначение в ранней схеме |
| --- | --- |
| GPU 0 · RTX 3090 | vLLM, Qwen3-14B-AWQ |
| GPU 1 · RTX 3090 | llama.cpp, 27B |
| ComfyUI | Ручной запуск, когда под задачу свободна GPU |

`70-gpu-pin.conf` — пример закрепления vLLM. Получите UUID нужной карты через `nvidia-smi -L` на своей машине и укажите его локально. Для актуального развёртывания используйте файлы из [корневого каталога systemd](../../systemd/README.md).

## Проверка после запуска

```bash
systemctl --user list-units 'hardpro-*' --no-pager
journalctl --user -u hardpro-vllm -n 50 --no-pager
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

[К ранней версии LAN Hub](../README.md)
