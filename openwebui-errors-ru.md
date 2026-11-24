# Диагностика Open WebUI по слоям

[К репозиторию](README.md) · [Open WebUI для разработки](openwebui-coding/README.md) · [systemd](systemd/README.md)

Документ отвечает на три вопроса по каждой ошибке: **что произошло**, **где слабое место**, **чем проверить**. Ссылки на файлы и переменные — по реальному стеку: браузер → Open WebUI (aiohttp) → прокси (nginx / Cloudflare / Traefik) → бэкенд (llm-host / vLLM / llama.cpp / Ollama).

---

## Слои и признаки сбоя

```
[браузер] --WS/SSE--> [Open WebUI: aiohttp] --HTTP stream--> [прокси] --HTTP--> [backend]
   1                        2                                  3              4
```

| Слой | Что рвётся | Типичный симптом |
|---|---|---|
| 1. Браузер ↔ WebUI | WebSocket, SSE, socket.io | ответ встал, спиннер, «Connection lost» |
| 2. WebUI ↔ прокси/бэкенд | aiohttp-таймауты, чтение тела | `TransferEncodingError`, `ClientPayloadError`, `ServerDisconnectedError` |
| 3. Прокси | буферизация, таймауты, размер тела, gzip | 502 / 504 / 413 / обрезанный стрим |
| 4. Бэкенд | VRAM, контекст, слоты, рейт-лимит | SSE-чанк с `error`, OOMKilled, 429 |

**Правило деления вины:** воспроизвели `curl`-ом напрямую в бэкенд → виноват бэкенд. Не воспроизвели → виноват слой 2 или 3.

---

## Ошибки транспорта

### 1.1 `TransferEncodingError: Not enough data to satisfy transfer length header`

- **Что:** сервер объявил `Content-Length: N` (или `Transfer-Encoding: chunked`), клиент прочитал меньше N байт, соединение закрылось. aiohttp фиксирует расхождение фрейминга.
- **Слабое место:** таймаут `AIOHTTP_CLIENT_TIMEOUT` в WebUI, `proxy_buffering on` в nginx, `gzip` на потоковом ответе, смерть бэкенда посреди генерации.
- **Проверка:**
  ```bash
  curl -N -v --max-time 900 -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"llm-host","messages":[{"role":"user","content":"3000 слов"}],"stream":true}' \
    http://<backend>:8080/v1/chat/completions
  ```
  Рвётся здесь → слой 3/4. Цело здесь, рвётся в браузере → слой 2.
- **Фикс:** `AIOHTTP_CLIENT_TIMEOUT=600`, `proxy_buffering off`, `gzip off`, `proxy_read_timeout 3600s`.

### 1.2 `ClientPayloadError: Response payload is not completed`

- **Что:** тот же корень, что 1.1, но без явного заголовка длины — оборвался chunked-поток или тело пришло пустым при `200 OK`.
- **Слабое место:** бэкенд вернул `200` с пустым телом (упал воркер, отменённый запрос, `parallel` исчерпан), либо прокси закрыл поток по таймауту простоя.
- **Проверка:** в логах бэкенда искать обрыв ровно в момент ошибки; `curl -N` с `--max-time 900` и `-w '\nHTTP %{http_code} bytes %{size_download}\n'`.
- **Фикс:** поднять `--parallel`/`-np`, увеличить `proxy_read_timeout`, убрать `Connection: close` со стороны прокси.

### 1.3 `ServerDisconnectedError`

- **Что:** сокет закрыт до получения заголовков ответа.
- **Слабое место:** бэкенд не слушает нужный адрес (`127.0.0.1` вместо `0.0.0.0` в контейнере), worker перезапустился, upstream keepalive истёк, health-check убил процесс.
- **Проверка:**
  ```bash
  docker ps                      # рестарты контейнера
  docker inspect --format '{{.RestartCount}}' <backend>
  curl -v http://<backend>:8080/health
  ```

### 1.4 `ClientConnectorError: Cannot connect to host ... [Errno 111]`

- **Что:** TCP-соединение не установилось вообще.
- **Слабое место:** DNS внутри docker-сети, неверный порт, `OLLAMA_BASE_URL` / `OPENAI_API_BASE_URL` указывает на `localhost` изнутри контейнера, бэкенд на другом хосте и не проброшен.
- **Проверка:** из контейнера WebUI — `docker exec -it openwebui sh -c "curl -v http://<backend>:8080/health"`.

### 1.5 `ClientConnectorCertificateError` / `SSLCertVerificationError`

- **Что:** самоподписанный или истёкший сертификат.
- **Слабое место:** прокси/бэкенд с TLS, WebUI не знает CA.
- **Фикс:** `REQUESTS_CA_BUNDLE=/path/ca.pem`, `SSL_CERT_FILE=/path/ca.pem`. Понижать проверку (`verify=False`) — только в изолированной сети.

### 1.6 `TimeoutError` / `asyncio.TimeoutError` / `ConnectionResetError 10054`

- **Что:** таймаут на чтении или RST со стороны сервера.
- **Слабое место:** `total`-таймаут aiohttp, `sock_read` не задан, промежуточный файрвол режет долгие соединения (типично 60/120/300 с), Windows-стек сбрасывает keepalive.
- **Фикс:** `aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)`; в WebUI — `AIOHTTP_CLIENT_TIMEOUT` в секундах.

### 1.7 HTTP-коды прокси

| Код | Что значит | Слабое место |
|---|---|---|
| 502 | прокси не достучался до бэкенда | слой 4 лежит, неверный upstream, DNS |
| 504 | прокси дождался своего таймаута | `proxy_read_timeout`, `proxy_send_timeout` |
| 413 | тело запроса больше лимита | `client_max_body_size` (nginx), загрузка файлов в чат |
| 429 | рейт-лимит | `limit_req`, лимиты Cloudflare, лимиты бэкенда |
| 499 | клиент отвалился первым | браузер/WebUI закрыл поток, часто следствие 1.1 |
| 408 | прокси-таймаут запроса | `client_body_timeout`, `client_header_timeout` |

---

## Ошибки внутри SSE

Это отдельный класс: HTTP-статус `200`, стрим идёт, в одном из чанков лежит `{"error": {...}}`. WebUI часто показывает его как «пустой ответ» или общий `Connection Error`.

| Сообщение | Что значит | Слабое место |
|---|---|---|
| `maximum context length is N tokens` / `context length exceeded` | промпт + история больше окна | `num_ctx` (Ollama), `--ctx-size` (llama.cpp), `max_model_len` (vLLM) |
| `CUDA out of memory` | VRAM кончилась на текущем батче | размер модели, `--parallel`, `batch`, квантование |
| `no slots available` | все слоты заняты | `-np` / `--parallel` в llama.cpp |
| `the model is overloaded` / 429 от vLLM | очередь запросов | `--max-num-seqs`, автобатч, рейт-лимит клиента |
| `model not found` / `model requires more system memory` | тег модели отсутствует или не влезает | `ollama list`, `:Q4_K_M` вместо полной |
| `KV cache is full` | кэш ключ-значение переполнен | `--ctx-size`, `--cache-type-k/v` |
| `Empty response from model` | бэкенд закрыл поток без единого токена | chat-template, стоп-токен в первом же токене, отмена запроса |

**Проверка:** `curl -N` показывает чанки как есть:
```bash
curl -sN ... | grep -o '"error":{[^}]*}' | head
```

---

## Особенности Open WebUI

| Ошибка / симптом | Что значит | Слабое место |
|---|---|---|
| `Cannot connect to Ollama` / `Ollama: Connection Error` | WebUI не видит Ollama | `OLLAMA_BASE_URL`, `OLLAMA_HOST=0.0.0.0` |
| `Connection lost` после 30–60 с, чат живой | WebSocket отвалился, не HTTP | `ENABLE_WEBSOCKET_SUPPORT`, redis, `WEBSOCKET_REDIS_URL` |
| Все пользователи разлогинились разом | секрет сессии сменился/не задан | `WEBUI_SECRET_KEY` должен быть стабильным |
| `Error in generating embeddings` / RAG молчит | модель эмбеддингов или chromadb недоступны | `RAG_EMBEDDING_ENGINE`, `RAG_EMBEDDING_MODEL`, сеть до HF |
| Первый ответ 2–5 минут, потом нормально | скачивание модели/эмбеддингов при старте | предзагрузка, локальный кэш |
| Загрузка файла падает | лимит тела | `client_max_body_size`, `RAG_FILE_MAX_SIZE` |
| `401` / `JWT expired` | токен протух, часы на хостах разошлись | NTP, `WEBUI_AUTH` |
| Ответ приходит одним куском без стрима | буферизация в прокси или клиент просит `stream:false` | `proxy_buffering off`, настройки провайдера |

---

## Где смотреть журналы

```bash
# WebUI — тут видно исключение aiohttp целиком, со стектрейсом
docker logs -f --tail 200 openwebui 2>&1 | grep -Ei 'error|Traceback|aiohttp|TransferEncoding'

# прокси
docker logs -f --tail 200 nginx 2>&1 | grep -E ' 50[0-4] | 499 '

# бэкенд — тут видно OOM, контекст, слоты
docker logs -f --tail 200 llm-host

# смерть по памяти
docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}} {{.RestartCount}}' llm-host
```

---

## Порядок диагностики

```
Ошибка видна?
├─ Connection/Connector/Certificate ──────────► слой 4 недоступен или TLS. curl /health из контейнера.
├─ TransferEncoding / ClientPayload ───────────► curl -N напрямую.
│   ├─ рвётся и в curl ──┬─ 200 + пусто ───────► бэкенд упал/отменил. Логи, OOMKilled.
│   │                    └─ рвётся на N сек ───► прокси: proxy_read_timeout, buffering, gzip.
│   └─ в curl цело ────────────────────────────► AIOHTTP_CLIENT_TIMEOUT в WebUI.
├─ 502/504 ────────────────────────────────────► прокси↔бэкенд: upstream, таймауты.
├─ 413 ────────────────────────────────────────► client_max_body_size / RAG_FILE_MAX_SIZE.
├─ SSE error: context / CUDA / slots ──────────► параметры запуска бэкенда.
└─ «пустой ответ» без ошибки ──────────────────► chat-template, стоп-токен, буферизация.
```

---

## Настройки потоковой выдачи

```nginx
location / {
    proxy_pass http://llm-host:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    chunked_transfer_encoding on;
    gzip off;
    client_max_body_size 100m;
}
```

```env
AIOHTTP_CLIENT_TIMEOUT=600
WEBUI_SECRET_KEY=<стабильный, не менять>
OLLAMA_BASE_URL=http://<host-в-сети-docker>:11434
```

Бэкенд:

```bash
# llama.cpp
--ctx-size 32768 --parallel 4 --n-gpu-layers 999
# vLLM
--max-model-len 32768 --max-num-seqs 8
# Ollama
OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=-1
```
