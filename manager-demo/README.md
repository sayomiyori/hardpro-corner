# Публичное демо каталога HARD-PRO

Модуль поиска и покупательский HTTP-обработчик используют опубликованные карточки и проверенную справку. Тестовый набор в репозитории создан на синтетических товарных данных.

## Проверка

```bash
PYTHONPATH=manager-demo:manager-demo/web python -m unittest discover -s manager-demo/web -p 'test_*.py'
PYTHONPATH=manager-demo python -m unittest discover -s manager-demo -p 'test_*.py'
```

Для HTTP-проверок установите `fastapi` и `httpx`. Запуск примера с `uvicorn` доступен после настройки источника.

```bash
PYTHONPATH=manager-demo:manager-demo/web python -m uvicorn widget:app --host 127.0.0.1 --port 8766
```

Параметр `STOREFRONT_FEED_URL` задаёт прямой адрес JSON-снимка опубликованного каталога. Параметр `STOREFRONT_ORIGIN` задаёт точный origin витрины. Формат снимка показан в [тестовом примере](web/test_shop_dialog.py). Для локального предпросмотра отдельный ключ `STOREFRONT_PREVIEW_KEY` позволяет передать этот снимок в теле запроса вместе с диалогом.

[Справка покупателя](../docs/storefront-knowledge-2026-11-25.md) · [Защитная проверка](../docs/storefront-security-2026-11-25.md) · [Карточки, фото и маршруты](../docs/storefront-widget-cards-2026-11-25.md)
