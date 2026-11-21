"""
title: Разбор ошибок Open WebUI
author: ollie (built by ☠ SYSTEM BREACHED ☠)
version: 1.0.0
required_open_webui_version: 0.3.30
description: Дописывает к ответу расшифровку технической ошибки — что произошло, где слабое место, чем проверить, как починить. Работает как Filter (outlet).
"""

from typing import Any, Awaitable, Callable, Optional
import re

MARKER = "<!-- owui-diag-v1 -->"

# Порядок важен: частное раньше общего.
RULES: list[dict] = [
    {
        "id": "transfer_encoding",
        "pattern": r"not enough data to satisfy transfer length header|transferencodingerror",
        "title": "Обрыв тела ответа (расхождение HTTP-фрейминга)",
        "layer": "транспорт: WebUI ↔ прокси ↔ бэкенд",
        "meaning": (
            "Сервер объявил Content-Length (или chunked-поток), клиент прочитал меньше байт, "
            "соединение закрылось. Ответ пришёл не целиком — это не ответ модели, а разорванный канал."
        ),
        "weak": "Таймаут клиента в WebUI (AIOHTTP_CLIENT_TIMEOUT), буферизация прокси, gzip на стриме, падение бэкенда посреди генерации.",
        "check": [
            "curl -N напрямую в бэкенд тем же телом запроса",
            "proxy_buffering / proxy_read_timeout в конфиге прокси",
            "docker inspect --format '{{.State.OOMKilled}}' <backend>",
        ],
        "fix": "AIOHTTP_CLIENT_TIMEOUT=600; proxy_buffering off; gzip off; proxy_read_timeout 3600s.",
    },
    {
        "id": "payload_incomplete",
        "pattern": r"response payload is not completed|clientpayloaderror|chunkedencodingerror",
        "title": "Тело ответа не дочитано",
        "layer": "транспорт: WebUI ↔ прокси ↔ бэкенд",
        "meaning": (
            "Поток закрылся до конца тела. Часто выглядит как 200 OK с пустым или обрезанным ответом — "
            "бэкенд принял запрос, но не довёл генерацию."
        ),
        "weak": "Отмена запроса на бэкенде, исчерпаны слоты параллельности, таймаут простоя на прокси.",
        "check": [
            "curl -N -w '\\nHTTP %{http_code} bytes %{size_download}\\n'",
            "логи бэкенда ровно в момент ошибки",
            "--parallel / OLLAMA_NUM_PARALLEL",
        ],
        "fix": "Поднять параллельность бэкенда; убрать Connection: close; proxy_read_timeout вверх.",
    },
    {
        "id": "server_disconnected",
        "pattern": r"serverdisconnectederror|remote end closed connection|connection aborted",
        "title": "Сервер закрыл соединение",
        "layer": "транспорт: прокси → бэкенд",
        "meaning": "Сокет закрыт до получения заголовков ответа. Upstream недоступен или перезапустился.",
        "weak": "Бэкенд слушает 127.0.0.1 вместо 0.0.0.0, рестарт воркера, истёкший keepalive.",
        "check": ["docker ps — рестарты", "docker inspect RestartCount", "curl -v http://<backend>/health"],
        "fix": "OLLAMA_HOST=0.0.0.0 / --host 0.0.0.0; стабилизировать health-check; proxy_http_version 1.1.",
    },
    {
        "id": "connector",
        "pattern": r"clientconnectorerror|cannot connect to host|errno 111|connection refused",
        "title": "TCP-соединение не установлено",
        "layer": "транспорт: WebUI → бэкенд",
        "meaning": "Адрес или порт недоступны: запрос не дошёл до бэкенда вообще.",
        "weak": "localhost внутри контейнера вместо имени сервиса, DNS docker-сети, неверный порт.",
        "check": [
            "docker exec -it openwebui curl -v http://<backend>:<port>/health",
            "значение OLLAMA_BASE_URL / OPENAI_API_BASE_URL",
        ],
        "fix": "Указать имя сервиса из docker-сети, а не localhost.",
    },
    {
        "id": "tls",
        "pattern": r"clientconnectorcertificateerror|sslcertverificationerror|certificate verify failed|self.signed certificate",
        "title": "Сертификат TLS не принят",
        "layer": "транспорт: TLS",
        "meaning": "Самоподписанный или истёкший сертификат на прокси/бэкенде.",
        "weak": "WebUI не знает CA-сертификат внутреннего прокси.",
        "check": ["openssl s_client -connect <host>:443 | openssl x509 -noout -dates"],
        "fix": "REQUESTS_CA_BUNDLE=/path/ca.pem и SSL_CERT_FILE=/path/ca.pem.",
    },
    {
        "id": "timeout_rst",
        "pattern": r"asyncio\.timeouterror|timeouterror|connectionreseterror|errno 10054|errno 104",
        "title": "Таймаут или сброс соединения",
        "layer": "транспорт",
        "meaning": "Соединение разорвано по времени ожидания или RST от промежуточного узла.",
        "weak": "Общий таймаут aiohttp, файрвол с лимитом длинных соединений, NAT-таблица.",
        "check": ["на какой секунде рвётся", "лимиты файрвола/балансировщика"],
        "fix": "aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None); таймауты прокси 3600s.",
    },
    {
        "id": "ctx_length",
        "pattern": r"maximum context length|context length exceeded|context window|too many tokens|exceeds the available context",
        "title": "Переполнен контекст",
        "layer": "бэкенд: окно модели",
        "meaning": "Промпт вместе с историей чата больше окна модели.",
        "weak": "num_ctx (Ollama), --ctx-size (llama.cpp), max_model_len (vLLM) меньше реального запроса.",
        "check": ["посчитать токены запроса", "текущее значение ctx в параметрах запуска"],
        "fix": "--ctx-size 32768 / num_ctx 32768 / max_model_len 32768; сократить историю или включить обрезку.",
    },
    {
        "id": "cuda_oom",
        "pattern": r"cuda out of memory|cuda error|out of memory|oomkilled|not enough memory|requires more system memory",
        "title": "Не хватило памяти (VRAM/RAM)",
        "layer": "бэкенд: память",
        "meaning": "Модель или батч не влезают в память на текущих параметрах.",
        "weak": "Размер модели, квантование, --parallel, KV-кэш под длинный контекст.",
        "check": ["nvidia-smi в момент генерации", "docker inspect OOMKilled", "текущее квантование модели"],
        "fix": "Квант Q4_K_M вместо полной; уменьшить --parallel и --ctx-size; освободить VRAM.",
    },
    {
        "id": "slots",
        "pattern": r"no slots available|no available slots|all slots are busy|server busy",
        "title": "Заняты все слоты обработки",
        "layer": "бэкенд: очередь",
        "meaning": "Запросов больше, чем одновременно обрабатываемых слотов.",
        "weak": "-np / --parallel, --max-num-seqs.",
        "check": ["число одновременных пользователей", "значение параллельности"],
        "fix": "Поднять --parallel (с оглядкой на VRAM) или ограничить конкурентность.",
    },
    {
        "id": "overloaded",
        "pattern": r"the model is overloaded|too many requests|rate limit|429 too many",
        "title": "Перегрузка / рейт-лимит",
        "layer": "бэкенд или прокси",
        "meaning": "Запрос отклонён по лимиту частоты или по длине очереди.",
        "weak": "limit_req в nginx, лимиты Cloudflare, --max-num-seqs во vLLM.",
        "check": ["заголовок Retry-After", "лимиты на прокси", "метрики очереди бэкенда"],
        "fix": "Поднять лимит или добавить повтор с экспоненциальной паузой на клиенте.",
    },
    {
        "id": "model_missing",
        "pattern": r"model not found|unknown model|no such model|model .* not installed|pull model manifest",
        "title": "Модель не найдена",
        "layer": "бэкенд: реестр моделей",
        "meaning": "Запрошенный тег модели отсутствует на бэкенде.",
        "weak": "Опечатка в теге, модель не скачана, раздельные хранилища у контейнеров.",
        "check": ["ollama list", "curl /v1/models", "точное имя в настройках провайдера"],
        "fix": "ollama pull <model>; имя модели в WebUI должно совпадать с тегом один в один.",
    },
    {
        "id": "kv_cache",
        "pattern": r"kv cache is full|kv cache.*exhausted|cache is full",
        "title": "Переполнен KV-кэш",
        "layer": "бэкенд: память контекста",
        "meaning": "Кэш ключ-значение не вмещает текущую длину контекста.",
        "weak": "--ctx-size, тип кэша, число параллельных слотов.",
        "check": ["длина контекста", "cache-type-k/v"],
        "fix": "--cache-type-k q8_0 --cache-type-v q8_0; уменьшить ctx или параллельность.",
    },
    {
        "id": "empty_response",
        "pattern": r"empty response|empty message|no content returned|returned an empty",
        "title": "Пустой ответ модели",
        "layer": "бэкенд: генерация",
        "meaning": "Поток открылся и закрылся без единого токена.",
        "weak": "Chat-template, стоп-токен сработал на первом токене, отмена запроса.",
        "check": ["curl -N с тем же телом", "шаблон чата у модели", "стоп-последовательности в параметрах"],
        "fix": "Убрать стоп-токены из настроек модели; проверить chat-template; повторить прямым curl.",
    },
    {
        "id": "ollama_conn",
        "pattern": r"cannot connect to ollama|ollama: connection error|ollama.*not reachable",
        "title": "WebUI не видит Ollama",
        "layer": "конфигурация сервисов",
        "meaning": "Адрес Ollama недоступен из контейнера WebUI.",
        "weak": "OLLAMA_BASE_URL и OLLAMA_HOST.",
        "check": ["docker exec -it openwebui curl http://<ollama>:11434/api/tags"],
        "fix": "OLLAMA_BASE_URL=http://<service>:11434, OLLAMA_HOST=0.0.0.0 на стороне Ollama.",
    },
    {
        "id": "websocket",
        "pattern": r"connection lost|websocket|socket\.io|disconnected from server",
        "title": "Потеря канала реального времени",
        "layer": "клиент ↔ WebUI",
        "meaning": "Оборвался WebSocket/SSE, при этом сам HTTP-запрос мог завершиться.",
        "weak": "ENABLE_WEBSOCKET_SUPPORT, redis, прокси без поддержки upgrade.",
        "check": ["вкладка Network на upgrade-запрос", "proxy_set_header Upgrade/Connection", "redis доступен"],
        "fix": "Прокси должен пропускать Upgrade; WEBSOCKET_REDIS_URL корректен; либо отключить WS.",
    },
    {
        "id": "secret",
        "pattern": r"jwt expired|invalid token|unauthorized|session expired|401",
        "title": "Сессия недействительна",
        "layer": "аутентификация",
        "meaning": "Токен протух или подпись не сходится — всех разлогинило.",
        "weak": "WEBUI_SECRET_KEY меняется при перезапуске, расхождение часов.",
        "check": ["env контейнера на WEBUI_SECRET_KEY", "date на хостах", "NTP"],
        "fix": "Задать стабильный WEBUI_SECRET_KEY и синхронизировать время.",
    },
    {
        "id": "embeddings",
        "pattern": r"embeddings?|rag.*error|chromadb|sentence.transformers|vector store",
        "title": "Сбой индексации / RAG",
        "layer": "WebUI: эмбеддинги",
        "meaning": "Модель эмбеддингов или векторная база недоступна — знания и файлы не индексируются.",
        "weak": "RAG_EMBEDDING_ENGINE/MODEL, сеть до репозитория моделей, права на каталог данных.",
        "check": ["лог при старте контейнера", "доступность внешнего embedding-сервиса"],
        "fix": "Локальная модель эмбеддингов или внешний сервис; предзагрузка модели в образ.",
    },
    {
        "id": "gateway_502",
        "pattern": r"\b502\b|bad gateway|upstream prematurely closed",
        "title": "502 — прокси не достучался до бэкенда",
        "layer": "прокси",
        "meaning": "Upstream недоступен или упал на этапе установки соединения.",
        "weak": "Неверный upstream-адрес, бэкенд лежит, DNS контейнера.",
        "check": ["docker ps", "curl upstream из контейнера прокси", "лог nginx upstream"],
        "fix": "Проверить имя сервиса и порт в proxy_pass; поднять бэкенд.",
    },
    {
        "id": "gateway_504",
        "pattern": r"\b504\b|gateway timeout",
        "title": "504 — прокси дождался своего таймаута",
        "layer": "прокси",
        "meaning": "Бэкенд не ответил за отведённое прокси время.",
        "weak": "proxy_read_timeout / proxy_send_timeout слишком малы для длинной генерации.",
        "check": ["время в логе прокси", "длительность ответа при прямом curl"],
        "fix": "proxy_read_timeout 3600s; proxy_send_timeout 3600s.",
    },
    {
        "id": "too_large",
        "pattern": r"\b413\b|request entity too large|payload too large|body.*too large",
        "title": "413 — тело запроса больше лимита",
        "layer": "прокси",
        "meaning": "Запрос (файл, длинная история) превысил допустимый размер.",
        "weak": "client_max_body_size, RAG_FILE_MAX_SIZE.",
        "check": ["размер вложения", "лимиты в конфиге прокси и WebUI"],
        "fix": "client_max_body_size 100m; поднять RAG_FILE_MAX_SIZE.",
    },
]

FALLBACK = {
    "id": "fallback",
    "title": "Техническая ошибка без точного совпадения",
    "layer": "не определён по тексту",
    "meaning": "Текст ошибки не совпал с известными шаблонами — расшифровка ниже общая.",
    "weak": "Смотреть полный стектрейс в логах контейнера WebUI.",
    "check": [
        "docker logs --tail 200 openwebui | grep -Ei 'error|traceback'",
        "повторить запрос прямым curl в бэкенд",
    ],
    "fix": "Разделить вину прямым curl: рвётся там — бэкенд/прокси; цело — таймауты WebUI.",
}


class Filter:
    class Valves:
        def __init__(self):
            self.priority: int = 0
            self.enabled: bool = True
            self.collapsed: bool = False
            self.show_fix: bool = True
            self.show_check: bool = True
            self.show_layer: bool = True
            self.max_rules: int = 3
            self.unknown_fallback: bool = False
            self.notify: bool = True
            self.trigger_pattern: str = (
                r"error|ошибк|failed|exception|traceback|timeout|refused|unavailable|\b5\d\d\b|\b429\b"
            )

    def __init__(self):
        self.valves = self.Valves()
        self._compiled = [(r, re.compile(r["pattern"], re.IGNORECASE)) for r in RULES]
        self._trigger = re.compile(self.valves.trigger_pattern, re.IGNORECASE)

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or content)
        return ""

    def _matches(self, text: str) -> list[dict]:
        found: list[dict] = []
        seen: set[str] = set()
        for rule, rx in self._compiled:
            if rule["id"] in seen:
                continue
            if rx.search(text):
                found.append(rule)
                seen.add(rule["id"])
                if len(found) >= self.valves.max_rules:
                    break
        if not found and self.valves.unknown_fallback and self._trigger.search(text):
            found.append(FALLBACK)
        return found

    def _render(self, rules: list[dict]) -> str:
        out: list[str] = [MARKER, ""]
        open_tag = "" if self.valves.collapsed else " open"
        title = "🩺 Разбор ошибки" if len(rules) == 1 else "🩺 Разбор ошибки (несколько причин)"
        out.append(f"<details{open_tag}>")
        out.append(f"<summary>{title}</summary>")
        out.append("")
        for i, r in enumerate(rules, 1):
            head = r["title"] if len(rules) == 1 else f"{i}. {r['title']}"
            out.append(f"**{head}**")
            out.append("")
            if self.valves.show_layer:
                out.append(f"- **Слой:** {r['layer']}")
            out.append(f"- **Что произошло:** {r['meaning']}")
            out.append(f"- **Слабое место:** {r['weak']}")
            if self.valves.show_check and r.get("check"):
                checks = "; ".join(r["check"])
                out.append(f"- **Проверить:** {checks}")
            if self.valves.show_fix and r.get("fix"):
                out.append(f"- **Быстрый фикс:** {r['fix']}")
            out.append("")
        out.append("</details>")
        return "\n".join(out)

    # ------------------------------------------------------------------ hooks

    async def inlet(self, body: dict, **kwargs) -> dict:
        return body

    async def outlet(self, body: dict, **kwargs) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages") or []
        target = None
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                target = msg
                break
        if target is None and messages:
            target = messages[-1]
        if target is None:
            return body

        text = self._text(target.get("content"))
        if not text or MARKER in text:
            return body
        if not self._trigger.search(text):
            return body

        rules = self._matches(text)
        if not rules:
            return body

        block = self._render(rules)

        if isinstance(target.get("content"), list):
            target["content"].append({"type": "text", "text": "\n\n" + block})
        else:
            target["content"] = f"{text}\n\n{block}"

        emitter = kwargs.get("__event_emitter__")
        if self.valves.notify and callable(emitter):
            try:
                await emitter(
                    {
                        "type": "notification",
                        "data": {
                            "type": "info",
                            "content": f"Ошибка разобрана: {rules[0]['title']}",
                        },
                    }
                )
            except Exception:
                pass

        return body
