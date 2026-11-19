# HARD-PRO Corner — локальный ИИ-помощник менеджера на Ubuntu Server

Как собрать сервер локального инференса на NVIDIA GeForce RTX 3090, скачивать файлы моделей на отдельный NVMe, запускать генерацию на одной видеокарте и следить за температурой из SSH-консоли. Проект предназначен для разработчиков, которые хотят подключить русскоязычного помощника к каталогу товаров и остаткам 1С. Этот README описывает настройку с нуля и отмечает, какие части уже проверены.

| Компонент | Что готово |
| --- | --- |
| Ubuntu Server 24.04 LTS, два NVMe | Системный диск смонтирован в `/`, диск моделей в `/srv/models`. Проверена запись с правами обычного пользователя. |
| SSH и консоль | Вход по ключу, фильтрация входящих соединений UFW, постоянные датчики в tmux и полноэкранный мониторинг. |
| vLLM | На одной RTX 3090 с 24 ГиБ проверены vLLM 0.34.0, PyTorch 2.14.0+cu130 и генерация через локальный OpenAI-совместимый API. Для первого прогона использована Qwen3-14B-AWQ. |
| Помощник и 1С | Ниже приведена предлагаемая схема чтения каталога и остатков. Интеграцию предстоит проверить на данных магазина. |

## Где хранятся веса и где работает модель

```mermaid
flowchart TB
    disk["Файлы модели на NVMe 4 ТБ"] -->|"vLLM загружает веса при старте"| weights
    subgraph card["Одна RTX 3090"]
        weights["Веса в видеопамяти"] --> infer["Генерация токенов на GPU"]
    end
    request["Запрос к API vLLM"] --> infer
    infer -->|"Токены через API"| reply["Ответ клиенту"]
```

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
set -g mouse off
set -g status 2
set -g status-interval 2
set -g 'status-format[1]' '#[align=left,fg=colour117,bold] #($HOME/.local/bin/hp-temps)'
```

При подключении из Windows Terminal параметр `mouse off` оставляет выделение и правый клик самому терминалу. Для вставки текста из буфера Windows используй `Ctrl+Shift+V`. В уже открытой сессии достаточно выполнить `tmux set -g mouse off`. [Сочетания клавиш Windows Terminal](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/actions#paste)

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

## Обновления безопасности Ubuntu

На Ubuntu Server с `unattended-upgrades` сначала обнови индексы APT и проверь план установки. Источники обновлений и разрешение на автоматическую перезагрузку определяются настройками APT. Перед работой по SSH проверь, что автоматическая перезагрузка отключена. [Документация Ubuntu по unattended-upgrades](https://ubuntu.com/server/docs/how-to/software/automatic-updates/)

```bash
sudo apt-get --error-on=any update
sudo unattended-upgrade -v --dry-run
```

После проверки запусти установку отдельной системной службой. Тогда разрыв SSH-соединения не прервёт обновление. По журналу можно проследить установку до завершения.

```bash
sudo systemd-run --unit=llm-security-update --no-block /usr/bin/unattended-upgrade -v
journalctl -u llm-security-update -f
```

После завершения открой **новое** SSH-соединение с рабочей станции. Проверь результат установки и состояние оборудования.

```bash
dpkg --audit
/usr/lib/update-notifier/apt-check 2>&1; echo
nvidia-smi --query-gpu=name,pci.bus_id,memory.total,driver_version --format=csv
findmnt --mountpoint /srv/models -o SOURCE,TARGET,FSTYPE,UUID
findmnt --fstab --mountpoint /srv/models -o SOURCE,TARGET,FSTYPE
if test -e /run/reboot-required; then cat /run/reboot-required /run/reboot-required.pkgs; fi
```

В выводе `apt-check` второе число показывает количество доступных обновлений безопасности. Сверь обе карты по адресам PCI и UUID смонтированной файловой системы с UUID в `fstab`. Если Ubuntu запросила перезагрузку, согласуй время остановки и повтори проверки после загрузки.

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

Для обращения с рабочей станции открой `ssh -N -L 8000:127.0.0.1:8000 llm-lab`. Клиент сможет использовать `http://127.0.0.1:8000/v1` и ключ приложения. Проверка после запуска — запрос к `/v1/models` с ключом и без него, затем короткая генерация. Для сквозной проверки базы мы создали на SSD миниатюрную Llama со случайными весами. Сервис вернул `401` без ключа и при неверном ключе, `200` со списком моделей и `200` с одним сгенерированным токеном через `/v1/chat/completions`. Этот тест проверяет загрузку с диска, вычисления на GPU и работу API. Проверка ответов на товарные вопросы приведена ниже, в разделе об учебном каталоге.

Ключ vLLM защищает маршруты инференса, поэтому адрес прослушивания и правила сети важны и при включённой авторизации. [Безопасность vLLM](https://docs.vllm.ai/en/stable/usage/security/)

## Первый запуск помощника на учебном каталоге

Для первого текстового прогона выбрана **Qwen3-14B-AWQ**. Команда Qwen выпускает готовую четырёхбитную версию с лицензией Apache 2.0. Два файла весов занимают 9,29 ГиБ на диске. Такая модель помещается на одной RTX 3090 и оставляет часть 24 ГиБ VRAM для рабочего контекста. В пилоте использован контекст 4096 токенов и один одновременный запрос. [Карточка модели и режимы ответа](https://huggingface.co/Qwen/Qwen3-14B-AWQ)

В окружении с `huggingface_hub` скачай зафиксированный снимок на смонтированный диск. Проверка точки монтирования предотвращает запись весов на системный SSD при недоступности второго диска.

```bash
(
  set -e
  mountpoint -q /srv/models
  export HF_HOME=/srv/models/huggingface
  hf download Qwen/Qwen3-14B-AWQ \
    --revision 31c69efc29464b6bb0aee1398b5a7b50a99340c3 \
    --local-dir /srv/models/weights/Qwen3-14B-AWQ
)
```

В службе vLLM выше укажи `/srv/models/weights/Qwen3-14B-AWQ` вместо каталога-примера и оставь имя API-модели `manager`. Для обычных вопросов менеджера используется `enable_thinking=false`. Следующий пример обращается к своему локальному API. Перед запуском экспортируй `VLLM_API_KEY` из закрытого окружения службы. Текст примера и каталог вымышлены.

```python
import json
import os
import urllib.request

physical, reserved, requested = 8, 5, 4
available = physical - reserved
messages = [
    {"role": "system", "content": "Ответь менеджеру кратко по-русски по учебной сводке. "
     f"Перчатки «Гранит», 14 oz, склад Север. Свободно {available} пары, "
     f"запрошено {requested}, недостаёт {max(0, requested - available)}."},
    {"role": "user", "content": "Хватит ли четырёх пар со склада Север?"}
]
body = {"model": "manager", "messages": messages, "temperature": 0, "max_tokens": 160,
        "chat_template_kwargs": {"enable_thinking": False}}
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/chat/completions", data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["VLLM_API_KEY"]})
with urllib.request.urlopen(req, timeout=90) as response:
    print(json.load(response)["choices"][0]["message"]["content"])
```

### Что получилось на учебном каталоге

Приложение вычисляет свободное количество по формуле `физически − резерв` для конкретного SKU и склада. Оно передаёт модели явный статус, когда замер старше пяти минут или в выборке нет складской строки. В первом прогоне Qwen дважды приняла отсутствие сведений за отсутствие товара. В настройку добавили явные статусы и правило сохранять размер и вес при поиске на другом складе.

| Вопрос менеджера | Ответ модели на тестовых данных |
| --- | --- |
| Сколько стоят «Гранит» 12 oz и сколько свободно в Центре? | «Пара «Гранит» 12 oz стоит 3 900 руб. В Центре свободно 5 пар.» |
| Где взять «Гранит» 14 oz, если в Центре нет свободных пар? | «На складе Центр свободно: 0 пар «Гранит» 14 oz. В наличии только на складе Север — свободно 3 пары.» |
| Можно подтвердить клиенту наличие «Вектора» размера L в Центре? | «Нет, подтвердить наличие рашгарда «Вектор» размера L на складе Центр нельзя — данные устарели. Нужна сверка.» |

После правок проверили десять заранее записанных вопросов, четыре дополнительные формулировки и диалог с уточнением. На этом наборе ответы совпали с учебным каталогом. Медиана полного ответа на десять отдельных вопросов после запуска модели составила 0,58 с при одном запросе за раз. Проверка на обезличенных вопросах менеджеров и подключение 1С остаются следующими этапами.

## Следующий этап — каталог и остатки 1С

Менеджер спрашивает «Есть ли этот товар в размере M?». Сервис поиска находит карточку и характеристику по артикулу, названию и размеру. Затем отдельная операция чтения получает доступный остаток по этой характеристике и выбранному складу. Модель формулирует ответ из результата запроса. Если источник недоступен, помощник сообщает об этом. При ответе из кеша указывает время сверки.

Перед подключением зафиксируем значение «доступно» в конкретной конфигурации 1С, правила учёта резервов и разрешённые склады. Приёмочные примеры должны включать две характеристики одного товара, остаток ноль, устаревший ответ и сбой соединения. Реализация API и проверка на рабочем контуре вынесены в следующий этап.

Правила публикации и ведения истории описаны в [AGENTS.md](AGENTS.md).
