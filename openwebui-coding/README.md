# Open WebUI — coding desk

Всё, что ниже, ставится поверх твоей инсталляции. Docker-Compose — единственное,
что нельзя сделать кликами; остальное — либо env, либо галочки в UI.

## 1. Поднять

```powershell
cd "C:\Users\user\Documents\Проект по умолчанию\openwebui-coding"
Copy-Item .env.example .env
# правишь .env под своё окно, потом:
docker compose up -d
# локальный ollama рядом:
docker compose --profile local up -d
```

Адрес: `http://localhost:8080`.

Если Open WebUI уже стоит иначе (pip, другой compose, чужой сервер) — переноси
только `environment:` из `docker-compose.yml`, значения те же.

## 2. Контекст — то, ради чего всё

Три уровня, работают вместе.

**a) Окно модели** (`num_ctx`) Ставь `DEFAULT_NUM_CTX` в `.env` равным реальному
окну минус запас на ответ. В UI **не включай** `num_ctx` вручную: тумблер
подставляет `2048` и молча режет промпт.

| окно модели | DEFAULT_NUM_CTX | CONTEXT_COMPACTION_TOKEN_THRESHOLD |
|---|---|---|
| 32k  | `24576`  | `20000` |
| 64k  | `49152`  | `40000` |
| 128k | `98304`  | `80000` |
| 200k | `163840` | `140000` |

**b) Context Compaction.** Включён в compose. Когда чат переваливает порог —
старые сообщения сворачиваются в саммари, свежие остаются. Модель не падает с
`prompt is too long`, и ты не теряешь нить.

UI-путь: `Settings → Admin → Interface → Context Compaction`.

**c) Плашка + жёсткая обрезка.**

Плашка над полем ввода — `context-plaque.user.js` (см. раздел 7).
Функция `functions/bench_context_guard.py` только режет промпт: кладёшь в
`Admin Panel → Functions → +`, включаешь. `SHOW_STATUS` выключен — статус в
сообщениях больше не нужен, плашка рисует всё сама.

Valves: `ENABLE_TRIM`, `MAX_PROMPT_TOKENS`, `KEEP_LAST_MESSAGES`,
`WINDOWS_JSON` (переопределить окно: `{"qwen3-coder": 131072}`).

## 3. Интерфейс под код

`Settings → Interface` (дефолты уже в compose, это если правишь руками):

- **Widescreen Mode** — вкл. Код не переносится по 60 символам.
- **Chat Bubble UI** — выкл. Пузыри съедают ширину.
- **Collapse Code Blocks** — выкл. Блоки раскрыты.
- **Expand Details** — вкл.
- **Rich Text Input** — выкл. Вставляешь код как есть, без экранирования.
- **Enter Key Behavior** — `Ctrl+Enter to Send`. Enter = перенос строки.
- **Paste Large Text as File** — вкл. Простыня кода уходит файлом, не в тело.
- **Prompt Autocompletion** — вкл.
- **Copy Formatted** — вкл.
- **Fade Effect for Streaming Text** — выкл. Глазам легче.
- **Title Auto-Generation / Tags / Follow-Ups** — выкл. Экономит токены и время.
- **Render Markdown in User Messages** — выкл. Твой код не превращается в разметку.

## 4. Модели

`Settings → Admin → Models → Model Defaults`:

- **Temperature** `0.2`, **top_p** `0.95`, **top_k** `40`, **repeat_penalty** `1.05`.
- **File Context** — вкл, но `Citations` выкл: цитаты переписывают кэш каждый ход.
- **Stream Chat Response** — вкл.

Системный промпт на модель (Workspace → Models → карандаш):

```
You are a senior engineer. Answer in the user's language.
Code first, explanation after. No preamble, no apologies.
Never guess an API - say FILL: and what is missing.
Keep diffs minimal; show full file only when asked.
```

Для локального Ollama **не** трогай `OLLAMA_CONTEXT_LENGTH` в UI — он живёт в
`docker-compose.yml`, профиль `local`.

## 5. Файлы и код в контексте

`Admin → Documents` (RAG):

- `CHUNK_SIZE` `1200`, `CHUNK_OVERLAP` `200` — код рвётся по границам функций,
  а не по 500 символов.
- `RAG_TOP_K` `6`.
- Для целых репозиториев: `BYPASS_EMBEDDING_AND_RETRIEVAL=true` и грузи файлы
  как контекст напрямую. RAG не нужен, когда файл и так влезает.

## 7. Плашка над полем ввода

`context-plaque.user.js` — тонкая полоса прямо над «Отправить сообщение»:

```
3.4k / 128k  ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▭▭▭▭▭▭  27%
```

Цифры настоящие: скрипт читает `context_usage` из `GET /api/v1/chats/<id>`
(там `tokens`, `estimated_tokens`, `threshold`, `percent`). Если эндпоинт молчит —
считает по отрисованным сообщениям и помечает это в тултипе.

Установка, любой из двух путей:

1. **Tampermonkey** → *Create a new script* → стереть шаблон → вставить
   `context-plaque.user.js` целиком → Ctrl+S. Дальше грузится само.
2. **Без установки** — DevTools → Console → вставить тело скрипта (без
   `==UserScript==` шапки) → Enter. Живёт до перезагрузки страницы.

Скрипт хост-независимый: `@match *://*/*` + тихая проверка `/api/config`.
Работает на `llm-host`, на любом порту, по http и https, под реверс-прокси
с подпутём (`/openwebui/c/<id>` — префикс вычисляется из `location.pathname`).
На чужих сайтах скрипт сам выключается через 10 секунд, если `#chat-input`
так и не появился, а `/api/config` не ответил как Open WebUI.

Что править в шапке файла:

| поле | смысл |
|---|---|
| `CFG.fallbackWindow` | окно, если модель не распознана (по умолчанию 32768) |
| `CFG.warnPct` / `dangerPct` | где бар желтеет и краснеет (70 / 90) |
| `WINDOWS` | таблица `подстрока → окно`. Добавь свою модель первой строкой |

Если плашка не появилась — `#chat-input` не найден. Проверь, что открыт именно
чат (не `/admin`), и что в консоли нет ошибки от Tampermonkey.

Удалённый сервер: цифры идут с того же origin, что и чат, — плашка не знает про
`localhost`. Единственное, что живёт локально, — таблица `WINDOWS`; если окно
модели на `llm-host` отличается, поправь строку в таблице или
`CFG.fallbackWindow`.

## 8. Чеклист после первого запуска

1. `Admin Panel → Functions` — вставить `bench_context_guard.py`, включить.
   `SHOW_STATUS` оставь выключенным, `ENABLE_TRIM` включённым.
2. `Admin → Interface` — Context Compaction on, порог из таблицы.
3. `Admin → Models → Model Defaults` — temperature 0.2, citations off.
4. `Settings → Interface` — проверить widescreen / bubble / rich text.
5. Поставить `context-plaque.user.js`, открыть чат, увидеть полосу над вводом.
6. Отправить длинный чат, убедиться что плашка растёт и обрезка срабатывает.

## Гочи

| Симптом | Причина |
|---|---|
| Пустой ответ модели | `num_ctx` слишком мал, обрезает tool-схемы |
| Метр не появился | функция выключена глобально, или модель не прислала `usage` |
| `prompt is too long` | порог компакции выше реального окна модели |
| Саммари врёт | `CONTEXT_COMPACTION_MODEL` слабый — поставь модель побольше |
| Код ломается на вставке | `Rich Text Input` не выключен |

`AIOHTTP_CLIENT_TIMEOUT=600` уже в конфиге — долгие reasoning-модели не рвутся
на 5 минутах.
