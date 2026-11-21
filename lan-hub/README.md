# LAN-обвязка вокруг локальных моделей

Самодостаточный контур для своей сети: доска моделей с замерами, агент-разработка
с доступом к серверу, след модели в каждом ответе и перенос контекста из Codex.
Собрано и проверено на Ubuntu 24.04 + 2×RTX 3090, но привязано только к
OpenAI-совместимым бэкендам (llama.cpp) и Open WebUI.

## 1. Что внутри

| Путь | Назначение |
|---|---|
| `hub/` | доска: карточки моделей, замеры скорости, GPU, счётчики workspace |
| `agent/` | агент: shell, файлы, сеть, MCP, сессии и трейс вызовов |
| `gateway/` | один LAN-порт: `/hub`, `/agent`, остальное — Open WebUI |
| `traceproxy/` | OpenAI-прокси, дописывает след модели в ответ |
| `migrate/` | выгрузка Codex с рабочей машины и импорт на сервер |
| `openwebui/` | фильтр Open WebUI (опционально, если хвост не нужен) |
| `bin/` | `hp-search.py` (поиск по индексу), `sync-mcp.sh` |
| `install.sh` | установщик: venv, Node, TLS, systemd-юниты |

Порты по умолчанию: `9090` — единственный вход (шлюз), `9443`/`9444` — доска и агент
на loopback, `8775` — Open WebUI на loopback.

## 2. Установка

```bash
scp -r lan-hub user@llm-host:~/
ssh llm-host 'cd ~/lan-hub && LAN_IP=192.0.2.10 bash install.sh'
```

`LAN_IP` — адрес сервера в локальной сети. Установщик создаёт `~/hardpro`,
поднимает venv, ставит Node, генерит TLS и пароль, пишет четыре user-юнита и
печатает пути. Логин и пароль попадают в `~/hardpro/HUB-CREDENTIALS.txt` (0600).

Проверка:

```bash
systemctl --user is-active hardpro-hub hardpro-agent hardpro-gateway
curl -s -u "$USER:$PASS" http://192.0.2.10:9090/hub/healthz
```

## 3. Доска

`http://<адрес>:9090/hub/` — карточки бэкендов, кнопки замера (prompt tok/s,
генерация tok/s, TTFT), история прогонов в `~/hardpro/hub/bench.sqlite`, GPU, счётчики
workspace. Замер — реальная генерация, а не оценка: цифры видны в интерфейсе.

## 4. Агент

`http://<адрес>:9090/agent/` — чат с трейсом. Инструменты: `shell` (bash на сервере),
`read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `http_fetch`, `web_search`,
`model_list`, `gpu_status`, `skill_*` и MCP-серверы как `mcp__<server>__<tool>`.

Тот же агент отдаётся как OpenAI-модель, поэтому его можно выбрать прямо в Open WebUI:

```
GET  /v1/models            -> [{"id": "hardpro-agent"}]
POST /v1/chat/completions  -> обычный чат-формат, внутри работает tool-loop
```

## 5. След модели

`traceproxy/aggregator.py` притворяется OpenAI-эндпоинтом: опрашивает бэкенды,
строит реестр «модель → бэкенд» и дописывает в конец ответа строку с цифрами.

```
---
⚙ **след** · `Qwen3.8 Flash Next` · **1.05 с** · **27.7 tok/s** · 29 ток · TTFT 401 мс

prompt 51.1 tok/s · генерация 49.5 tok/s · стоп `stop`
```

Реестр обновляется сам (раз в минуту и при промахе), поэтому новая модель в том же
llama.cpp попадает под хвост без правок конфигов. Новый бэкенд добавляется строкой
в `trace.env`:

```
TA_UPSTREAMS=http://127.0.0.1:8081;http://127.0.0.1:8083;http://127.0.0.1:8767/manager|ТОКЕН
TA_LABEL_MAP={"Qwen3.8-Flash-Next":"Qwen3.8 Flash Next"}
TA_SKIP_TAIL=hardpro-agent
```

## 6. Переключатель моделей

`models/profiles.json` — каталог профилей: какая модель, на каких GPU, на каком
порту и с какими аргументами. Роли (`agent`, `chat`, `media`) — ярлык «кто сейчас
обслуживает вход». Смена роли поднимает нужную модель на её картах и гасит
профили, конфликтующие по GPU или порту (`policy.evict`).

Два локальных движка запуска: `llamacpp` (llama.cpp `serve`: `-ngl`/`-c`/`--mmproj`,
`--fit` для моделей крупнее одной карты, `--no-kv-offload`, если KV-кэш нужно
держать в RAM ради больших контекстов) и `vllm` (`vllm serve`:
`--served-model-name`, `--max-model-len`, `--gpu-memory-utilization`,
`--api-key` из переменной `token_env`). Профили `external` каталог только
опрашивает — их процессами он не управляет.

Доска и агент дают то же самое по HTTP: `GET /api/models` плюс
`POST /api/models/switch|launch|stop|restart|role|profile`. В карточке модели
правятся карты, порт, контекст, слои на GPU (или утилизация VRAM для vLLM),
флаги и автостарт; «перезапустить» применяет правки к живому процессу.
Из терминала:

```bash
~/hardpro/bin/hp-model.py list
~/hardpro/bin/hp-model.py switch agent <модель>
~/hardpro/bin/hp-model.py check
```

Состояние процессов — `~/hardpro/models/state.json`, логи загрузки —
`~/hardpro/logs/model-<имя>.log`. Перезапуск hub/agent не гасит поднятые модели
(`KillMode=process`), автозапуск при загрузке сервера делает юнит `hardpro-models`
(`hp-model.py autostart` по профилям с `"autostart": true`).

## 7. Перенос Codex

```powershell
powershell -ExecutionPolicy Bypass -File .\migrate\codex-export.ps1 -SshHost llm-host
```

Собирает `~/.codex` (sessions, memories, skills, plugins, config.toml) и проекты,
пакует, кладёт на сервер и запускает импорт. Импорт раскладывает всё в
`~/hardpro/workspace`, собирает `index.sqlite` (проекты, сессии, скиллы, MCP,
полнотекстовый индекс переписки) и пишет `AGENTS.md`.

```bash
~/hardpro/bin/hp-search.py search "open-webui"
~/hardpro/bin/hp-search.py sessions --limit 10
~/hardpro/bin/hp-search.py skills --grep wp
```

## 8. Безопасность

- Наружу открыт один порт; внутренние сервисы слушают `127.0.0.1`.
- Доска и агент закрыты HTTP Basic, пароль генерируется при установке.
- `Agent` исполняет команды на сервере от своего пользователя: это его смысл.
  Ограничивать доступ нужно на уровне сети и пароля, а не только промптом.
- TLS-сертификат самоподписанный и опционален: шлюз по умолчанию отдаёт http
  внутри сети, как и Open WebUI за ним.

## 9. Ограничения

- Браузерная автоматизация не проверялась: бинарь Chromium качается с внешнего CDN,
  в закрытых сетях его нужно ставить отдельно (`apt install chromium-browser`).
- MCP-серверы, завязанные на Windows-бинари, помечаются `disabled` при импорте —
  им нужен Linux-аналог.
- Доска измеряет бэкенды синтетическим прогоном, а хвост в чате — реальные запросы:
  цифры могут расходиться при разной длине контекста.
