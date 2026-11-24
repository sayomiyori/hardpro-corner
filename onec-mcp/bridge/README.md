# bridge — Windows-сторона моста к 1С (COM)

Копия того, что развёрнуто в `E:\Work\Tools\onec-bridge`. Правки вносятся здесь,
затем копируются на рабочую машину.

| Файл | Куда | Роль |
|---|---|---|
| `onec-mcp.ps1` | `E:\Work\Tools\onec-bridge\` | MCP-сервер на COMConnector, 14 инструментов |
| `link.mjs` | там же | держит `powershell` ↔ `ssh`, пауза до 60 с |
| `start-link.cmd` | там же | ручной старт |
| `config.example.json` | копия в `config.json` | база, mode, allow/deny, аудит |
| `onec-bridge-pipe.py` | `~/​.local/bin/` на hardpro-llm | UNIX-сокет ↔ stdio SSH |

Требования: 64-битный PowerShell, зарегистрированный `V83.COMConnector`
(здесь 8.3.27.1688), DPAPI-файл с учётными данными 1С, ключ SSH на hardpro-llm,
Node 20+ для `link.mjs`.

Схема и эксплуатация — [`../docs/BRIDGE.md`](../docs/BRIDGE.md).
