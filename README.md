# HARD-PRO Corner — локальный ИИ-помощник менеджера на Ubuntu Server

Как собрать сервер локального инференса на NVIDIA GeForce RTX 3090, скачивать файлы моделей на отдельный NVMe, запускать генерацию на одной видеокарте и следить за температурой из SSH-консоли. Проект предназначен для разработчиков, которые хотят подключить русскоязычного помощника к каталогу товаров и остаткам 1С. Этот README описывает настройку с нуля и отмечает, какие части уже проверены.

| Компонент | Что готово |
| --- | --- |
| Ubuntu Server 24.04 LTS, два NVMe | Системный диск смонтирован в `/`, диск моделей в `/srv/models`. Проверена запись с правами обычного пользователя. |
| SSH и консоль | Вход по ключу, фильтрация входящих соединений UFW, постоянные датчики в tmux и полноэкранный мониторинг. |
| vLLM | На одной RTX 3090 с 24 ГиБ проверены vLLM 0.34.0, PyTorch 2.14.0+cu130 и генерация через локальный OpenAI-совместимый API. Рабочую модель для менеджера ещё предстоит выбрать. |
| Помощник и 1С | Ниже приведена предлагаемая схема чтения каталога и остатков. Интеграцию предстоит проверить на данных магазина. |

## Где хранятся веса и где работает модель

**Файлы модели на NVMe 4 ТБ → загрузка весов в видеопамять (VRAM) RTX 3090 → генерация токенов на GPU.** После остановки сервиса файлы остаются в `/srv/models`. При следующем запуске vLLM снова загружает их на выбранную карту. В этой конфигурации для инференса используется одна RTX 3090. Вторая карта доступна другим задачам.

Ёмкость SSD определяет, сколько моделей можно сохранить. Хватит ли для запуска 24 ГиБ VRAM, зависит от формата весов, длины контекста и числа одновременных запросов. vLLM также выделяет память под KV-кеш и рабочие буферы. `df -h /srv/models` показывает место для файлов, а `nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv -l 2` позволяет увидеть, на какой карте при запуске выросло потребление VRAM.

## Два SSD в одной Ubuntu

`Usage of /` в приветствии Ubuntu относится к корневой файловой системе. Второй SSD становится доступен приложениям как каталог после монтирования. Посмотри источники обеих точек монтирования следующими командами.

```bash
lsblk -o NAME,MODEL,SIZE,FSTYPE,MOUNTPOINTS
findmnt --mountpoint /srv/models
df -hT / /srv/models
```

Для диска моделей использована файловая система ext4. При подготовке пустого SSD сначала сверь его модель, ёмкость, серийный номер и источник `/`. Форматирование уничтожает прежнее содержимое. В `/etc/fstab` укажи UUID уже созданной файловой системы. Имена устройств вида `nvme1n1` могут меняться между загрузками.

```fstab
UUID=<UUID_ФАЙЛОВОЙ_СИСТЕМЫ> /srv/models ext4 defaults,nofail,nodev,nosuid,x-systemd.device-timeout=10s 0 2
```

После `sudo mount /srv/models` проверь `findmnt` и создай на этом томе каталог `weights` и каталоги кеша с правами нужного пользователя. Для Hugging Face задай `HF_HOME=/srv/models/huggingface`, для PyTorch Hub — `TORCH_HOME=/srv/models/torch`. Если используется Ollama, укажи `OLLAMA_MODELS=/srv/models/ollama` в его systemd-службе и дай доступ к каталогу пользователю службы. После переноса сравни список моделей. Для сервиса инференса условие `ConditionPathIsMountPoint=/srv/models` защищает корневой диск от записи весов при отсутствии второго SSD. [Переменные Hugging Face Hub](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables)

## Рабочая консоль и живые температуры

`btop` показывает нагрузку на CPU, память и процессы. В `nvtop` удобно видеть использование каждой карты, VRAM и процессы GPU. Для обычной работы достаточно нижней строки tmux с температурой CPU, обеих GPU и NVMe. Она обновляется каждые две секунды и оставляет командную строку свободной. [Руководство tmux](https://github.com/tmux/tmux/wiki/Getting-Started) · [Проект nvtop](https://github.com/Syllo/nvtop)

```bash
sudo apt-get update
sudo apt-get install tmux btop nvtop lm-sensors ripgrep fzf bat bash-completion
sensors
nvidia-smi --query-gpu=index,name,pci.bus_id,temperature.gpu --format=csv
```

Минимальный пример для AMD CPU и NVMe. Сохрани его в `~/.local/bin/hp-temps` и выполни `chmod +x ~/.local/bin/hp-temps`. На Intel имя драйвера датчиков обычно `coretemp`. Если датчик недоступен, проверь `sensors` и содержимое `/sys/class/hwmon`.

```sh
#!/bin/sh
for sensor in /sys/class/hwmon/hwmon*; do
    [ -r "$sensor/name" ] && [ -r "$sensor/temp1_input" ] || continue
    case "$(cat "$sensor/name")" in
        k10temp|coretemp) label=CPU ;;
        nvme) label=$(awk '{print $1}' "$sensor/device/model") ;;
        *) continue ;;
    esac
    awk -v label="$label" '{printf "%s %.0f°C  ", label, $1/1000}' "$sensor/temp1_input"
done
timeout 2 nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits 2>/dev/null |
    awk -F ', *' '{printf "GPU%s %s°C  ", $1, $2}'
printf '\n'
```

Добавь в `~/.tmux.conf` и запусти `tmux`. Чтобы применить изменения в открытой сессии, выполни `tmux source-file ~/.tmux.conf`.

```tmux
set -g status 2
set -g status-interval 2
set -g 'status-format[1]' '#[align=left,fg=colour117,bold] #($HOME/.local/bin/hp-temps)'
```

Для автозапуска добавь в конец `~/.profile` следующий блок. Условие проверяет интерактивную оболочку и терминал. Скрипты, `scp` и команды вида `ssh сервер команда` смогут использовать обычный текстовый поток.

```sh
case $- in
    *i*)
        if [ -t 0 ] && [ -t 1 ] && [ -z "${TMUX:-}" ] &&
           [ -n "${TERM:-}" ] && [ "$TERM" != dumb ] && command -v tmux >/dev/null; then
            exec tmux new-session
        fi
        ;;
esac
```

Для полноэкранного наблюдения подойдёт `watch -n 2 ~/.local/bin/hp-temps`. `Ctrl+C` вернёт приглашение. По умолчанию `Ctrl+B`, затем `D` отсоединяет сессию tmux. `tmux ls` показывает сохранённые сессии, `tmux attach -t ИМЯ` возвращает нужную. Для разработки `rg` ищет по коду, `fzf` помогает искать команды в истории после подключения Bash key bindings, а команда `batcat` подсвечивает исходники в Ubuntu.

## SSH из PowerShell

Чтобы Windows OpenSSH применял выделенный ключ по короткому имени, добавь профиль в пользовательский `.ssh/config`. Пример использует вымышленное DNS-имя. Подставь свою учётную запись и путь к закрытому ключу.

```sshconfig
Host llm-lab
    HostName llm.example.invalid
    User operator
    IdentityFile ~/.ssh/llm_lab_ed25519
    IdentitiesOnly yes
```

`ssh llm-lab` откроет сервер. При `Permission denied (publickey)` выполни `ssh -G llm-lab` и проверь `identityfile`, затем `ssh -v llm-lab` покажет предлагаемый ключ. На сервере в `authorized_keys` должна быть соответствующая открытая часть. При первом соединении сверь отпечаток ключа **сервера** через доверенный канал.

Перед отключением парольного входа оставь рабочее SSH-окно открытым, проверь ключ во втором окне и разреши в UFW входящий TCP-порт SSH со своей административной сети. Параметры OpenSSH для этой схемы — `PubkeyAuthentication yes`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`. Выполни `sudo sshd -t`, проверь эффективные настройки через `sudo sshd -T`, перезагрузи конфигурацию `sudo systemctl reload ssh` и повтори вход в новом окне. `sudo ufw status verbose` покажет действующие правила. [OpenSSH в Ubuntu](https://documentation.ubuntu.com/server/how-to/security/openssh-server/) · [UFW в Ubuntu](https://documentation.ubuntu.com/server/how-to/security/firewalls/)

## vLLM на одной RTX 3090

Для первой карты нужны рабочий драйвер NVIDIA, поддерживаемая версия Python и совместимая пара PyTorch/vLLM. На минимальной Ubuntu для сборки модулей Triton пригодятся `build-essential` и `python3-dev`. Проверяй совместимость для конкретного релиза. Для фиксации карты получи UUID через `nvidia-smi -L` и задай `CUDA_VISIBLE_DEVICES` локально, особенно если в машине стоят две одинаковые по названию карты. [Установка vLLM на GPU](https://docs.vllm.ai/en/stable/getting_started/installation/gpu/)

Проверенный набор версий для Python 3.12 можно установить в отдельное окружение с помощью `uv`. В команде ниже используется публичный индекс PyPI. При скачивании через зеркало проверяй SHA-256 файлов по опубликованным хешам PyPI.

```bash
sudo apt-get install --no-install-recommends build-essential python3-dev
uv venv --python /usr/bin/python3 ~/.local/share/vllm-venv
uv pip install --python ~/.local/share/vllm-venv/bin/python 'vllm==0.34.0'
uv pip check --python ~/.local/share/vllm-venv/bin/python
. ~/.local/share/vllm-venv/bin/activate
```

Для пробы одной карты укажи свой GPU и расположение файлов. В интерактивной оболочке задай `export CUDA_VISIBLE_DEVICES='<UUID выбранной GPU>'`, `export HF_HOME=/srv/models/huggingface` и `export TORCH_HOME=/srv/models/torch`. В службе systemd перенеси эти переменные в `Environment=`. Если весовые файлы и токенизатор уже скачаны, `HF_HUB_OFFLINE=1` позволяет загружать их локально. [Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

Пример запуска после загрузки совместимой модели в формате Hugging Face. Подставь каталог модели и передай случайный `VLLM_API_KEY` через закрытый файл окружения службы с правами `0600`. В примере контекст ограничен 4096 токенами и одним одновременным запросом. Доступный размер модели и KV-кеша нужно измерить на выбранных весах.

```bash
mountpoint -q /srv/models || exit 1
: "${VLLM_API_KEY:?Задай ключ API в закрытом окружении}"
export VLLM_API_KEY
vllm serve /srv/models/weights/MODEL_DIR --served-model-name manager \
  --host 127.0.0.1 --port 8000 --tensor-parallel-size 1 \
  --max-model-len 4096 --max-num-seqs 1 --gpu-memory-utilization 0.85
```

### Запуск через systemd

Создай `~/.config/llm-lab` с правами `0700`. Сгенерируй ключ один раз. `set -C` в примере защищает существующий файл от перезаписи, а `umask 077` ограничивает доступ владельцем.

```bash
install -d -m 700 ~/.config/llm-lab
(umask 077; set -C; python3 -c 'import secrets; print("VLLM_API_KEY=" + secrets.token_hex(32))' > ~/.config/llm-lab/api.env)
```

Сохрани пример как `~/.config/systemd/user/llm-lab.service`. Замени `MODEL_DIR` и значение `CUDA_VISIBLE_DEVICES` своими. В обычном unit-файле systemd спецификатор `%h` раскрывается в домашний каталог пользователя.

```ini
[Unit]
Description=Локальный API vLLM
ConditionPathIsMountPoint=/srv/models

[Service]
EnvironmentFile=%h/.config/llm-lab/api.env
Environment=CUDA_VISIBLE_DEVICES=GPU-UUID_ВЫБРАННОЙ_КАРТЫ
Environment=HF_HOME=/srv/models/huggingface
Environment=TORCH_HOME=/srv/models/torch
Environment=HF_HUB_OFFLINE=1
Environment=VLLM_HOST_IP=127.0.0.1
UMask=0077
NoNewPrivileges=yes
KillMode=mixed
ExecStart=%h/.local/share/vllm-venv/bin/vllm serve /srv/models/weights/MODEL_DIR --served-model-name manager --host 127.0.0.1 --port 8000 --tensor-parallel-size 1 --max-model-len 4096 --max-num-seqs 1 --gpu-memory-utilization 0.85

[Install]
WantedBy=default.target
```

Когда модель скачана, выполни `systemctl --user daemon-reload`, затем `systemctl --user start llm-lab`. Журнал доступен через `journalctl --user -u llm-lab -f`. Для постоянного запуска после выхода из консоли администратор может включить `loginctl enable-linger` для пользователя службы. Для старта службы при загрузке выполни `systemctl --user enable llm-lab`.

Для обращения с рабочей станции открой `ssh -N -L 8000:127.0.0.1:8000 llm-lab`. Клиент сможет использовать `http://127.0.0.1:8000/v1` и ключ приложения. Проверка после запуска — запрос к `/v1/models` с ключом и без него, затем короткая генерация. Для сквозной проверки базы мы создали на SSD миниатюрную Llama со случайными весами. Сервис вернул `401` без ключа и при неверном ключе, `200` со списком моделей и `200` с одним сгенерированным токеном через `/v1/chat/completions`. Этот тест проверяет загрузку с диска, вычисления на GPU и работу API. Качество ответов на вопросы о товарах будем оценивать отдельно.

Ключ vLLM защищает маршруты инференса, поэтому адрес прослушивания и правила сети важны и при включённой авторизации. [Безопасность vLLM](https://docs.vllm.ai/en/stable/usage/security/)

## Следующий этап — каталог и остатки 1С

Менеджер спрашивает «Есть ли этот товар в размере M?». Сервис поиска находит карточку и характеристику по артикулу, названию и размеру. Затем отдельная операция чтения получает доступный остаток по этой характеристике и выбранному складу. Модель формулирует ответ из результата запроса. Если источник недоступен, помощник сообщает об этом. При ответе из кеша указывает время сверки.

Перед подключением зафиксируем значение «доступно» в конкретной конфигурации 1С, правила учёта резервов и разрешённые склады. Приёмочные примеры должны включать две характеристики одного товара, остаток ноль, устаревший ответ и сбой соединения. Реализация API и проверка на рабочем контуре вынесены в следующий этап.

Правила публикации и ведения истории описаны в [AGENTS.md](AGENTS.md).
