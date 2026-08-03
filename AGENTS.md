# AGENTS.md — Выгрузка касаний Bitrix24 для анализа ИИ

> Комментарии и документация проекта — на русском; новые комментарии в коде тоже пишите на русском.

## Обзор проекта

CLI-утилита на Python (без фреймворков, плоская структура модулей в корне), которая собирает по каждой сделке Bitrix24 **все каналы общения в одну хронологическую ленту «касаний»** (touchpoints): звонки (с транскрипциями), переписки открытых линий (Viber / Telegram / WhatsApp и др.), комментарии таймлайна, смены стадий. Результат используется как контекст для LLM / загрузка в БД / RAG.

Ключевая проектная идея: в каждом касании фиксируется **тот, кто фактически вёл диалог** (`actor` — оператор звонка, автор сообщения), отдельно от ответственного за сделку (`responsible_user_id`). См. раздел «Как решается "кто вёл диалог"» в README.md — не ломайте эту семантику при правках.

Это **не пакет и не библиотека**: нет `pyproject.toml`/`setup.py`, нет тестов, нет CI. Все модули импортируются напрямую из корня репозитория.

## Технологический стек

- Python 3.12 (в репозитории есть локальное venv в `venv/`, на других машинах создаётся вручную).
- Зависимости (`requirements.txt`): `requests` — обязательная; `faster-whisper` — опциональная (только для локальной транскрибации).
  - Внимание: `config.py` импортирует `python-dotenv` (`load_dotenv`), но её нет в `requirements.txt` — при чистой установке добавьте её или уберите импорт.
- Внешние API: Bitrix24 REST (входящий вебхук), AssemblyAI (облачная транскрибация с диаризацией) или локальный Faster Whisper.

## Настройка и запуск

Конфигурация — `config.py` (объём выгрузки, провайдер транскрибации, паузы, пути) плюс секреты в `.env` / переменных окружения:

- `B24_WEBHOOK` — URL входящего вебхука Bitrix24 (права: **crm, im, imopenlines, telephony, user**). Без него `B24Client` падает с `SystemExit`.
- `ASSEMBLYAI_API_KEY` — ключ AssemblyAI (нужен, если `TRANSCRIPTION_PROVIDER = "assemblyai"`).

Команды:

```bash
pip install -r requirements.txt
python sync.py --limit 3 --no-transcribe   # быстрый тест структуры на 3 сделках
python sync.py --deals 1234 5678           # конкретные сделки
python sync.py --skip-whisper              # только штатные расшифровки Bitrix
python sync.py                             # полная выгрузка
```

`--limit` и `--deals` перезаписывают `config.DEALS_LIMIT` / `config.DEAL_IDS` на время запуска.

Вспомогательный скрипт `my_test.py` — не тест: склеивает все `export/deals/*.md` в `export/deals/result.md`.

## Архитектура и модули

Точка входа и оркестратор — `sync.py`. Поток данных за один проход:

```
sync.py (main -> process_deal)
  ├─ fetch_deals.py      # crm.deal.list + crm.stagehistory.list; stage_at() — стадия сделки на момент ts
  ├─ fetch_activities.py # звонки как CRM-активности (TYPE_ID=2), комментарии, письма/встречи,
  │                      # штатная AI-расшифровка (crm.activity.call.getTranscript)
  ├─ fetch_openlines.py  # чаты открытых линий + история сообщений; classify_sender() — кто писал
  ├─ users.py            # справочники с кэшем в data/: пользователи, контакты (ContactBook), стадии, источники
  ├─ transcribe.py       # скачивание записей с Диска -> audio/, транскрибация -> transcripts/
  ├─ normalize.py        # всё -> единый формат «касание» (touchpoint)
  └─ export_llm.py       # export/touchpoints.jsonl, export/deals_index.jsonl, export/deals/deal_<id>.md
```

- `api_client.py` — единственная точка обращения к Bitrix24: `B24Client.call/result/call_list`. POST JSON на `{webhook}/{method}`, пауза `REQUEST_PAUSE` (облако ~2 запроса/сек), ретраи с экспоненциальной задержкой на 429 / `QUERY_LIMIT_EXCEEDED` / сетевых сбоях, пагинация через `start`/`next`. Ошибки API — исключение `BitrixError`.
- Логирование — стандартный `logging` с логгером `"b24"` (настраивается в `sync.py`).

Важные решения, зафиксированные в docstring-ах (не откатывать без причины):

- Звонки берутся как CRM-активности (`crm.activity.list`, TYPE_ID=2), а не `voximplant.statistic.get`: работает с любой телефонией и надёжно привязано к сделке; `RESPONSIBLE_ID` активности = фактический участник разговора.
- Записи разговоров — id файлов Диска в поле `FILES` активности (часть порталов не отдаёт его в списочном методе — добирается точечно через `crm.activity.get`).
- Запись учитывается только для состоявшихся разговоров длиннее `MIN_RECORDING_DURATION_SEC` (15 с).
- Приоритет транскрипций: штатная расшифровка Bitrix (если `PREFER_BUILTIN_TRANSCRIPT`) → внешний провайдер (`TRANSCRIPTION_PROVIDER`: `assemblyai` / `whisper`). Статус пишется в `content.transcript_status` (`builtin|assemblyai|whisper|no_recording|no_transcript|pending`).
- История чатов: `imopenlines.session.history.get`, при недоступности — fallback на `im.dialog.messages.get`.

## Данные и артефакты (всё это в .gitignore, не коммитить)

| Путь | Содержимое |
|---|---|
| `export/touchpoints.jsonl` | Одна строка = одно касание (машинный формат). Схема — в docstring `normalize.py` и README |
| `export/deals/deal_<id>.md` | Хронологический протокол сделки — готовый контекст для LLM |
| `export/deals_index.jsonl` | Индекс: сделка → число касаний, звонков, чатов |
| `raw/deal_<id>.json` | Сырой JSON из API — позволяет пересобрать нормализацию без повторных запросов |
| `transcripts/`, `audio/` | Тексты транскрипций и записи звонков (имена: `deal<id>_call<activity_id>.txt/.mp3`) |
| `data/` | Кэши справочников (`users_cache.json`, `contacts_cache.json`, `stages.json`, `sources.json`) и `state.json` (время последнего прогона) |

Идемпотентность: скачанные записи и готовые транскрипты не перекачиваются/не пересоздаются (проверка по существованию файла); кэши контактов дозаполняются.

## Соглашения по коду

- Все модули начинаются с `# -*- coding: utf-8 -*-` и docstring-описания модуля на русском.
- Простой стиль: функции + dict-и, без классов там, где не нужно; `int()`-приведение ID из API.
- Конфигурация только через `config.py` — не хардкодить пороги/пути/паузы в логике.
- Все сетевые вызовы к Bitrix — только через `B24Client` (не дёргать `requests` напрямую, кроме скачивания файлов и AssemblyAI в `transcribe.py`).
- Файлы пишутся с `ensure_ascii=False`, UTF-8; JSON-запись атомарная через `tmp + os.replace` (см. `users._save_json`).
- Любые ошибки API по одной сделке/чату/звонку не должны ронять весь прогон — ловить, логировать, продолжать (см. паттерн в `sync.main`).

## Тестирование

Автотестов нет. Проверка изменений — прогон на малом объёме:

```bash
python sync.py --limit 3 --no-transcribe
```

и ручная сверка `export/touchpoints.jsonl` и `export/deals/deal_*.md`. Для правок нормализации/экспорта можно пересобирать результат из `raw/deal_*.json` без повторных запросов к API.

## Безопасность

- `.env` содержит секреты (вебхук с ключом, `ASSEMBLYAI_API_KEY`) — не читать без необходимости и никогда не коммитить; `.gitignore` уже исключает `.env`, `data/`, `export/`, `raw/`, `audio/`, `transcripts/`, `*.json`.
- Вебхук Bitrix — это полный доступ к порталу: `auth_key` из URL используется для скачивания файлов, не логировать его.
- Выгрузка содержит персональные данные и содержимое переписок — директории-артефакты поэтому исключены из git.
- Деплоя нет: скрипт запускается вручную локально; «развёртывание» = установить зависимости, положить `.env`, запустить `sync.py`.
