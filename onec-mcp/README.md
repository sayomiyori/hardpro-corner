# MCP для 1С:Предприятия

[К репозиторию](../README.md) · [Транспорты](docs/TRANSPORTS.md) · [COM-мост](docs/BRIDGE.md)

Агент подключается к 1С:Предприятию 8 через стандартный OData или через COM на Windows. Ниже настройки OData. Для COM используйте [инструкцию по мосту](docs/BRIDGE.md).

Для OData нужен Node.js 20+. Точка входа — `node src/server.mjs`.

## Что доступно агенту

| Инструмент | Что делает | Нужен режим |
|---|---|---|
| `onec_status` | связь, режим, число объектов, сервис | любой |
| `onec_metadata` | опубликованные объекты, поля, фильтр по имени | любой |
| `onec_describe` | поля объекта + пример строки | любой |
| `onec_query` | чтение с `$filter/$select/$orderby/$top/$skip/$expand` | любой |
| `onec_get` | одна запись по `Ref_Key` | любой |
| `onec_create` | создание (POST) | write |
| `onec_update` | правка полей (PATCH) | write |
| `onec_bulk_create` | пакет до 50 записей | write |
| `onec_post_document` | провести документ | write |
| `onec_unpost_document` | отменить проведение | write |
| `onec_service_call` | любой метод HTTP-сервиса | write |
| `onec_delete` | удаление (DELETE) | admin |

В OData `dryRun: true` поддерживают создание, изменение, пакетное создание и удаление. Вызовы проведения и HTTP-сервиса выполняются напрямую. Для пробного подключения используйте `readonly`.

## Подготовка 1С

- Включить **стандартный интерфейс OData** у объектов: в конфигураторе у справочника/документа/регистра
  в свойствах — «Использование в стандартном интерфейсе OData» (галочка публикации).
- Опубликовать базу на веб-сервере (IIS/Apache) с включённым OData и HTTP-сервисами.
- Завести отдельного пользователя `hardpro_mcp` с ролью только на нужные объекты; полные права не давать.
- Проверить руками: `https://onec.example.invalid/demo/odata/standard.odata/$metadata` открывается в браузере под этим пользователем.
- Добавить HTTP-сервис `hardpro` (Общие → HTTP-сервисы), корневой URL `hardpro`,
  шаблоны `/post`, `/unpost`, `/version`, и вставить код из
  [`onec/HTTPService-hardpro.bsl`](onec/HTTPService-hardpro.bsl).
- Для HTTP-сервиса согласуйте `ОжидаемыйТокен()` с `ONEC_SERVICE_TOKEN`, используйте TLS и ограничьте доступ к тестовой публикации.

## Установка

```bash
cd onec-mcp
node --version
npm test
```

`npm test` запускает `test-integration.mjs` с mock-OData на localhost. Тест проверяет метаданные, чтение, создание, правку, удаление, проведение и отказы по режимам.

`smoke.mjs` проверяет сеть, авторизацию и публикацию OData до всякого MCP:

```bash
export ONEC_URL=https://onec.example.invalid/demo/odata/standard.odata
# ONEC_USER и ONEC_PASSWORD загрузите из закрытого окружения.
node smoke.mjs Catalog_Номенклатура
```

## Подключение к агенту

Файл `~/hardpro/workspace/mcp/servers.json` — блок из
[`mcp-servers.snippet.json`](mcp-servers.snippet.json), затем:

```bash
~/hardpro/bin/sync-mcp.sh
```

Скрипт собирает `~/hardpro/agent/mcp.json`, проверяет наличие `node` в PATH и перезапускает
`hardpro-agent.service`. Дальше инструменты видны агенту как `mcp__onec__onec_query` и т.д.
Пути `args` и `cwd` — по факту установки, `FILL_*` — свои значения.

## Переменные окружения

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `ONEC_URL` | — (обязательна) | `.../odata/standard.odata` |
| `ONEC_USER` / `ONEC_PASSWORD` | — | пользователь 1С, Basic-авторизация |
| `ONEC_SERVICE_URL` | — | `.../hs/hardpro`, без неё нет проведения |
| `ONEC_SERVICE_TOKEN` | — | уходит в `X-Hardpro-Token` |
| `ONEC_MODE` | `readonly` | `readonly` / `write` / `admin` |
| `ONEC_ALLOW` | все | глобы имён объектов через запятую |
| `ONEC_DENY` | — | глобы-исключения, приоритет выше allow |
| `ONEC_TIMEOUT_MS` | `30000` | таймаут одного запроса |
| `ONEC_SERVICE_TIMEOUT_MS` | `120000` | таймаут проведения (1С думает долго) |
| `ONEC_MAX_ROWS` | `200` | потолок `$top` за один вызов |
| `ONEC_BULK_MAX` | `50` | потолок пакетного создания |
| `ONEC_MAX_FILTER` | `4000` | длина `$filter` |
| `ONEC_META_TTL_MS` | `300000` | кэш `$metadata` |
| `ONEC_AUDIT` | — | путь JSONL-журнала вызовов |

`ONEC_READONLY=1` жёстко режет запись, что бы ни лежало в `ONEC_MODE`.

## Режимы и аудит

- Режим `readonly` блокирует POST/PATCH/DELETE на уровне обработчика, до сети.
- `delete` недоступен без `ONEC_MODE=admin`.
- Имя объекта проверяется по синтаксису, `ONEC_DENY`, `ONEC_ALLOW` и наличию в `$metadata`;
  при опечатке возвращаются похожие имена — модель не гадает.
- Вызовы идут строго последовательно: 1С плохо переносит параллельные записи в один объект.
- При настроенном `ONEC_AUDIT` журнал получает имя инструмента, аргументы, результат и время. Храните журнал локально с ограниченным доступом.

## Границы OData

- Проведение документов и движения — только через HTTP-сервис (`onec_post_document`).
- Транзакций нет: пакетная запись — это N независимых запросов, откат делать руками.
- Не все объекты публикуемы: если объекта нет в `$metadata` — включать публикацию в конфигураторе.
- Регистры накопления по OData пишутся только при определённых настройках; для движений документов
  правильный путь — проведение, а не прямая запись регистра.
