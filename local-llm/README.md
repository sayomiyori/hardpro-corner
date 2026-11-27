# Утилиты LocalLLM

## Аппаратная сводка для SSH

На Linux команда `python3 hp-status` показывает процессор, память, накопители и отдельную карточку каждой NVIDIA GPU. Для определения датчиков используются hwmon, NVML и доступные системные утилиты. Если NVML недоступна, программа пробует nvidia-smi. Кеш в `$XDG_CACHE_HOME/hp-status` (по умолчанию `~/.cache/hp-status`) помогает заметить исчезнувшую карту.

~~~sh
python3 hp-status
python3 hp-status --full
python3 hp-status --temperatures
watch -n 2 ./hp-status --gpus
~~~
