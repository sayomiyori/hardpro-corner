# Windows-сторона COM-моста

[Схема и эксплуатация](../docs/BRIDGE.md) · [Транспорты 1С](../docs/TRANSPORTS.md)

| Файл | Назначение |
| --- | --- |
| `onec-mcp.ps1` | MCP-сервер на `V83.COMConnector`, 14 инструментов |
| `link.mjs` | Держит PowerShell и SSH, повторяет подключение с паузой до 60 с |
| `start-link.cmd` | Ручной запуск |
| `config.example.json` | Шаблон локального `config.json` с режимом `readonly` |
| `onec-bridge-pipe.py` | Принимает SSH-сеанс на Linux и связывает его с UNIX-сокетом |

Нужны 64-битный PowerShell, зарегистрированный COMConnector, Node.js 20+, SSH-ключ и локальный DPAPI-файл для пользователя, от которого работает мост. Подставьте данные своей тестовой базы в копию шаблона и храните её вне Git.
