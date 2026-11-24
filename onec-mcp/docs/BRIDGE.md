# Мост к 1С через COM (Windows → hardpro-llm)

База: `Розница`, `Srvr="192.168.1.195";Ref="base";`
OData на этой базе не опубликован, а сеть 192.168.1.0/24 с сервера `hardpro-llm`
(192.168.88.202) недоступна — поэтому работа идёт через COMConnector на Windows
и SSH-канал, поднятый **со стороны Windows**.

## Схема

```
агент на hardpro-llm
  └─ node src/pipe.mjs                     (stdio <-> UNIX-сокет)
       └─ ~/.state/onec-bridge.sock
            └─ python3 ~/.local/bin/onec-bridge-pipe.py   (сокет <-> stdin/stdout SSH)
                 └─ ssh -T (инициирует Windows)
                      └─ node E:\Work\Tools\onec-bridge\link.mjs
                           └─ powershell onec-mcp.ps1  (MCP + V83.COMConnector)
                                └─ 1С: 192.168.1.195 / base
```

Почему так:

- на `hardpro-llm` в `/etc/ssh/sshd_config.d/00-hardpro.conf` стоит
  `AllowTcpForwarding local` — обратный проброс (`-R`) запрещён;
- входящие порты на Windows закрыты фаерволом (нужен админ), поэтому HTTP-мост не годится;
- соединение в 1С занимает ~165 с (кластер/лицензия), поэтому COM-сессия живёт
  постоянно: прогрев при старте + keep-alive каждые 120 с.

## Файлы на Windows (`E:\Work\Tools\onec-bridge\`)

| Файл | Роль |
|---|---|
| `onec-mcp.ps1` | MCP-сервер (stdio, JSON-RPC) поверх COMConnector |
| `link.mjs` | супервизор: `powershell` ↔ `ssh`, пауза до 60 с при падении |
| `config.json` | база, файл учётных данных, mode, allow/deny, аудит |
| `start-link.cmd` | ручной запуск |
| `audit.jsonl` | журнал вызовов: op, аргументы, ok, миллисекунды |

Учётные данные не копируются: используется DPAPI-файл
`E:\Work\Projects\website\1c_mcp_transfer\config\onec-work-credentials.secret`
(привязан к пользователю Windows).

Задача планировщика `onec-bridge-link` — триггер «при входе в систему», состояние `Ready`.

## Файлы на hardpro-llm

| Путь | Роль |
|---|---|
| `~/.local/bin/onec-bridge-pipe.py` | один UNIX-сокет, один клиент за раз |
| `~/.state/onec-bridge.sock` | сокет, права 0600 |
| `~/hardpro/util/onec-mcp/src/pipe.mjs` | агентская сторона, байтовый прокси |
| `~/hardpro/agent/mcp.json` | сервер `onec` (собирается `bin/sync-mcp.sh`) |

## Инструменты

`onec_status`, `onec_metadata`, `onec_describe`, `onec_search`, `onec_query`,
`onec_get`, `onec_tabular`, `onec_create`, `onec_update`, `onec_post_document`,
`onec_unpost_document`, `onec_mark_delete`, `onec_delete`.

- `onec_check_fill` — `ПроверитьЗаполнение` без записи: список проблем перед проведением.

- `onec_search` — поиск объектов метаданных по подстроке во всех категориях;
  первый шаг, когда имя объекта неизвестно.
- `onec_tabular` — строки табличной части документа (`Товары`, `Услуги`, ...)
  с колонками и ссылками; чужое имя возвращает список существующих частей.
- `onec_check_fill` — готовность документа к проведению без записи.
- `postingReady` в `onec_status` — доступно ли из COM системное перечисление
  режимов записи; `false` означает, что проведение на этой платформе отвалится.

- `onec_create` / `onec_update` принимают `dryRun: true` — поля проверяются на живом
  объекте, `Записать()` не вызывается. Проверено на `Справочник.Номенклатура`.
- `onec_query` — язык запросов 1С, только `ВЫБРАТЬ`; батчи и временные таблицы режутся.
- `onec_delete` — только при `mode=admin`; по умолчанию `mode=write`.
- `allow`: `Справочник.*`, `Документ.*`; `deny`: `Справочник.Пользователи`,
  `Справочник.ВнешниеПользователи`. Запись в регистры через мост закрыта.

## Проверено на живой базе

- `health`: `Розница`, версия `2.3.23.58`;
- `Catalog`: 263 объекта, `Document`: список отдаётся;
- `describe Справочник.Номенклатура`: реквизиты с синонимами;
- `query`: `ВЫБРАТЬ ... ИЗ Справочник.Номенклатура` со ссылками `{ref, presentation}`;
- полный цикл `create → get → update → delete` на тестовом элементе
  `ZZZ MCP ТЕСТ УДАЛИТЬ`: элемент создан, прочитан, изменён, удалён,
  контрольный запрос вернул 0 строк;
- `dryRun` на `create` и `update`: поля приняты без записи; несуществующее поле
  отбито с понятным текстом.

Не проверено: `onec_post_document` (проведение меняет остатки и проводки —
первый запуск делать на тестовом документе в копии базы).

## Эксплуатация

```bash
# состояние моста
ssh hardpro-llm 'pgrep -af onec-bridge-pipe; ls -l ~/.state/onec-bridge.sock'

# логи Windows-стороны
type E:\Work\Tools\onec-bridge\link.err.log

# перезапуск моста (Windows)
schtasks /End /TN onec-bridge-link
schtasks /Run /TN onec-bridge-link
```

После перезапуска первые ~165 с COM-сессия прогревается; клиентские вызовы в это
время ждут в сокете, MCP отвечает, как только сессия готова.

Если сервер перезагрузился — сокет поднимется сам, как только Windows-сторона
переподключится (супервизор `link.mjs` с паузой до 60 с).
