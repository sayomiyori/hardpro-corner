# Два транспорта onec-mcp

Сервер умеет говорить с 1С двумя путями. Что включено — решает окружение.

## 1. OData (`ONEC_URL`)

`src/server.mjs` — стандартный интерфейс OData: `Catalog_*`, `Document_*`,
`$filter`, PATCH/DELETE, HTTP-сервис для проведения.
Работает, когда база опубликована на веб-сервере и сеть до неё есть.
Инструменты: `onec_query` с `$filter`, `onec_create`, `onec_update`, `onec_delete`,
`onec_bulk_create`, `onec_post_document`, `onec_service_call`.

## 2. COM-мост (`ONEC_PIPE_SOCK`)

`src/pipe.mjs` — байтовый прокси в UNIX-сокет; по ту сторону SSH-канала живёт
PowerShell-MCP с `V83.COMConnector`. Нужен, когда OData не опубликован, а база
доступна только из сети Windows. Полная схема, ограничения и эксплуатация —
[`docs/BRIDGE.md`](docs/BRIDGE.md).

Оба транспорта несовместимы по именам аргументов `onec_query`: OData берёт `$filter`,
COM-мост берёт язык запросов 1С. Включай что-то одно на инсталляцию.
