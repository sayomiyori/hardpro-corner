# Документы, изображения, видео и голос в чате Open WebUI

[Документация](README.md) · [Текущий статус](../STATUS.md)

Проверка 21 ноября 2026 года. Отчёт о том, как в Open WebUI включили работу с
документами, мультимодальность и голос на локальном стеке. Настройки
окружения с реальными ключами хранятся на сервере вне репозитория.

## Результаты контрольных запросов 21 ноября

| Задача | Модель | Результат |
| --- | --- | --- |
| Текст и код | Qwen3.8-Flash-Next | Проверено |
| Текст, код и документы (RAG) | Qwen3.8-Flash-Next | Проверено |
| Изображения и видео | Qwen3.8-27B с mmproj («медиа-модель») | Проверено |
| Голосовой ввод | faster-whisper + whisper-small (CTranslate2) | Проверено |
| Озвучка ответов | браузерный синтез на стороне клиента | Проверено |

Flash-Next — текстовая модель: в её весах нет проектора mmproj, поэтому
изображения и видео принимает отдельная 27B-модель с mmproj. Для неё
подойдёт только проектор от той же основы модели — универсального
проектора не существует.

## Роутер llama.cpp

При проверке 21 ноября чат-модели работали через роутер `llama serve` с пресетом `--models-preset`:
три модели, `--models-max 1` держит в памяти одну. Запрос к другой модели
автоматически выгружает текущую и загружает нужную. Пресет задаёт только пути,
общие параметры — в drop-in юнита, см. [drop-in роутера](../systemd/hardpro-coding.service.d/95-router.conf).
После 5 минут простоя модель выгружается (`--sleep-idle-seconds 300`).

Замер контрольной генерации со сменой модели:

| Модель | Время ответа со сменой |
| --- | --- |
| Qwen3.8-Flash-Next | 12,5 с |
| Qwen3.8-27B-UD-Q5_K_M | 7,2 с |
| Huihui-Qwen3.8-27B-abliterated-Q5_K | 5,5 с |
| возврат на Qwen3.8-Flash-Next | 12,9 с |

Отдельно зафиксировали холодную загрузку. Первый токен занимал от 15 секунд (27B читает ~20 ГБ)
до 90 секунд (Flash-Next читает 93 ГБ с NVMe). У Flash-Next эксперты
MoE выгружаются в ОЗУ через tensor override, поэтому модель помещается
в связке из двух RTX 3090 вместе с остальными сервисами. На проверке 24 ноября юнит роутера был выключен. [Текущий статус](../STATUS.md)

## Документы (RAG)

При загрузке документа Open WebUI извлекает текст, режет на фрагменты и
записывает в векторное хранилище. В offline-режиме (`OFFLINE_MODE=True`,
`HF_HUB_OFFLINE=1`) SentenceTransformer требует полный снапшот в кэше:
частичная загрузка даёт молчаливый отказ «No embedding model is loaded»
на шаге векторизации.

Решение: полный снапшот мультиязычной модели
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (28 файлов)
в локальный кэш на модельном NVMe и переменные окружения службы:

```dotenv
HF_HOME=/srv/models/huggingface
RAG_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RAG_EMBEDDING_MODEL_AUTO_UPDATE=False
```

Мультиязычная модель выбрана потому, что каталог и запросы на русском;
`all-MiniLM-L6-v2` ориентирован на английский. Префиксы запросов не нужны.

Проверка: загруженный файл прошёл обработку со статусом `completed`,
контрольный поиск по векторному хранилищу вернул нужный фрагмент
с расстоянием 0,22. Файлы, загруженные до исправления, нужно перезалить —
векторы у них не создавались.

## Изображения и видео

Медиа-модель запущена как `llama serve` с `--mmproj` и принимает
OpenAI-совместимые запросы с содержимым `image_url` и `video_url`.
Рантайм llama.cpp декодирует видео сам (флаги `--video-fps`,
`--video-ffmpeg-dir`), для этого нужен ffmpeg и ffprobe. Установлен
статический дистрибутив ffmpeg без прав администратора в `~/.local/bin`,
путь добавлен в окружение юнита (`systemd/hardpro-media-vision.service.d/40-ffmpeg.conf`).

Проверка запросами к API: изображение PNG — ответ по содержимому;
тестовый MP4 2 секунды — модель описала содержимое кадров.

## Голос

Транскрипция работает на faster-whisper 1.2.1 с локальной моделью
whisper-small в формате CTranslate2 (`WHISPER_MODEL` в окружении службы,
VAD-фильтр включён). Контрольный WAV вернул статус 200 по эндпоинту
транскрипции. Озвучка ответов — браузерный Web Speech API на стороне
клиента, серверного движка не требует.

## Как повторить на отдельном стенде

Команды рассчитаны на уже существующие юниты и проверенные пути к весам. Для медиа нужен ffmpeg в пути, заданном [drop-in](../systemd/hardpro-media-vision.service.d/40-ffmpeg.conf).

```bash
# включить роутер чат-моделей
mkdir -p ~/.config/systemd/user/hardpro-coding.service.d
cp systemd/hardpro-coding.service.d/95-router.conf ~/.config/systemd/user/hardpro-coding.service.d/
cp systemd/qwen38-models.ini /srv/models/hard-pro-content/coding/work/
systemctl --user daemon-reload && systemctl --user restart hardpro-coding.service
curl --fail http://127.0.0.1:8081/v1/models | jq -r '.data[].id'

# ffmpeg и ffprobe установите заранее, затем проверьте путь
test -x ~/.local/bin/ffmpeg && test -x ~/.local/bin/ffprobe
mkdir -p ~/.config/systemd/user/hardpro-media-vision.service.d
cp systemd/hardpro-media-vision.service.d/40-ffmpeg.conf ~/.config/systemd/user/hardpro-media-vision.service.d/
systemctl --user daemon-reload && systemctl --user restart hardpro-media-vision.service
```

## Ограничения и следующий шаг

После смены модели первый токен занимает до 90 секунд; при частых
переключениях стоит поднять паузу выгрузки. Медиа-модель работает на CPU
и отвечает медленно — это плата за совмещение с текстовым движком на двух
картах. RAG, видео и голос проверены контрольными запросами; следующий шаг —
приёмка на реальных вопросах менеджеров в браузере.
