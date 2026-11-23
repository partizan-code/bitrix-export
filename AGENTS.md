# AGENTS.md — Выгрузка касаний Bitrix24 для анализа ИИ

> Комментарии и документация проекта — на русском; новые комментарии в коде тоже пишите на русском.

## Обзор проекта

CLI-утилита на Python (без фреймворков, плоская структура модулей в корне), которая собирает по каждой сделке и каждому лиду Bitrix24 **все каналы общения в одну хронологическую ленту «касаний»** (touchpoints): звонки (с транскрипциями), переписки открытых линий (Viber / Telegram / WhatsApp и др.), комментарии таймлайна, смены стадий, задачи, письма и встречи. Результат используется как контекст для LLM / загрузка в БД / RAG.

Ключевая проектная идея: в каждом касании фиксируется **тот, кто фактически вёл диалог** (`actor` — сотрудник, принявший звонок, автор сообщения, исполнитель задачи), отдельно от ответственного за сделку (`responsible_user_id`). Не ломайте эту семантику при правках.

Вторая ключевая идея — **накопительный снимок**: прогон обрабатывает только новые/изменённые сущности, сохраняет результаты в постоянное хранилище (`data/store/` + `data/registry.json`), а затем рендерит из него **полный** снимок `export/snapshot/` — всю CRM на дату, без склейки архивов.

Это **не пакет и не библиотека**: нет `pyproject.toml`/`setup.py`, нет автотестов, нет CI. Все модули импортируются напрямую из корня репозитория.

## Технологический стек

- Python 3.12 (в репозитории есть локальное venv в `venv/`, на других машинах создаётся вручную).
- Зависимости (`requirements.txt`): `requests` — обязательная; `python-dotenv` — опциональная (подхват `.env`, импорт в `config.py` обёрнут в `try/except`); `faster-whisper` — опциональная (только для локальной транскрибации, нужен `ffmpeg` в PATH).
- Внешние API: Bitrix24 REST (входящий вебхук), AssemblyAI (облачная транскрибация с диаризацией) или локальный Faster Whisper.

## Настройка и запуск

Конфигурация — `config.py` (объём выгрузки, провайдер транскрибации, паузы, пути) плюс секреты в `.env` / переменных окружения:

- `B24_WEBHOOK` — URL входящего вебхука Bitrix24 (права: **crm, im, imopenlines, telephony, user**; для выгрузки задач — ещё **task**). Без него `B24Client` падает с `SystemExit`.
- `ASSEMBLYAI_API_KEY` — ключ AssemblyAI (нужен, если `TRANSCRIPTION_PROVIDER = "assemblyai"`).

Команды:

```bash
pip install -r requirements.txt
python sync.py --limit 3 --no-transcribe   # быстрый тест структуры на 3 сделках
python sync.py --deals 1234 5678           # конкретные сделки
python sync.py --leads 4321 8765           # конкретные лиды
python sync.py --leads --lead-limit 5      # тест на 5 лидах
python sync.py --skip-whisper              # только штатные расшифровки Bitrix (без AssemblyAI/Whisper)
python sync.py                             # полная выгрузка
```

`--limit`/`--deals` и `--lead-limit`/`--leads` перезаписывают `config.DEALS_LIMIT`/`config.DEAL_IDS` и `config.LEADS_LIMIT`/`config.LEAD_IDS` на время запуска. Объём и состав выгрузки настраиваются в `config.py`:

- `DATE_FROM` — нижняя граница по `DATE_CREATE` сделки (или звонка).
- `EXPORT_PERIOD_FROM` / `EXPORT_PERIOD_TO` — явный календарный период (приоритетен при полном проходе).
- `DEALS_LIMIT` / `DEAL_IDS`, `LEADS_LIMIT` / `LEAD_IDS` — ограничения выборки.
- `PROCESS_ACTIVE` / `PROCESS_COMPLETED` / `PROCESS_TECHNICAL` — какие группы сущностей выгружать.
- `PROCESS_LEADS` — выгружать лиды параллельно со сделками. Лиды запускаются по правилу `leads_enabled = PROCESS_LEADS and (not deal_test or lead_test)`: при тестовом запуске по сделкам лиды не трогаются, если явно не задан `--leads`/`--lead-limit`.
- `INCLUDE_COMMENTS`, `INCLUDE_TASKS`, `INCLUDE_EMAILS`, `INCLUDE_MEETINGS` — источники касаний.
- `EXPORT_TYPE` — `"auto"` / `"full"` / `"incremental"` (см. раздел «Инкрементальная выгрузка»).
- `REEXPORT_CLOSED_ON_CHANGE` — переобрабатывать ли закрытые сущности при изменении (по умолчанию `False` — закрытые переиспользуются из store).
- `MAKE_SNAPSHOT_ZIP` — упаковывать `export/snapshot/` в zip с именем по шаблону `ZIP_NAME_TEMPLATE` (`{stamp}` — дата/время снимка в `SNAPSHOT_TZ`).
- `ASSEMBLYAI_DIRECT_URL` — передавать AssemblyAI прямую ссылку Диска без локального скачивания (см. раздел «Важные архитектурные решения»).
- `FILE_BRAND`, `FILE_COUNTRY` — префиксы имён файлов и архивов.

Вспомогательные скрипты (не входят в основной поток):

- `refresh_names.py` — перерендер карточек из `data/store/` с актуальными именами пользователей/контактов/стадий (после увольнений, переименований); `--offline` — только из локальных кэшей, без запросов к API.
- `recover_registry.py` — восстановление `data/registry.json` из содержимого `data/store/` после обрыва прогона или ручных правок store.
- `diag_fields.py` — диагностика метаданных полей (`crm.deal.userfield.list`, `crm.item.fields`; этих вызовов нет в основном коде).
- `diag_filter.py` — диагностика фильтров выборки сделок: что вернёт `crm.deal.list` с текущими настройками.
- `my_test.py` — **неактуален**: склеивает `export/deals/**/*.md`, а этот каталог больше не создаётся. Не запускать.

## Архитектура и модули

Точка входа и оркестратор — `sync.py`. Выгружаются **две сущности**: сделки и лиды. Общий код параметризуется словарём `_ENTITY` (тип CRM-активности, owner_type 2/1, префикс привязки задачи `D_`/`L_`, поле стадии `STAGE_ID`/`STATUS_ID`); обработка каждой сущности — `run_entity` → цикл `process_entity`. Поток данных за один проход:

```
sync.py (main -> run_entity -> process_entity)
  ├─ fetch_deals.py      # crm.deal.list/get (select ["*","UF_*"]) + crm.stagehistory.list;
  │                      # группировка active/completed/technical; инкрементальная выборка;
  │                      # get_deals_by_ids (повтор ошибок, sweep закрытых);
  │                      # stage_at() — стадия сделки на момент ts;
  │                      # метаданные полей (crm.deal.fields) и резолв enum-значений в названия
  ├─ fetch_leads.py      # то же для лидов: crm.lead.list/get/fields, семантика статусов P/S/F
  ├─ fetch_activities.py # звонки как CRM-активности (TYPE_ID=2), комментарии, письма/встречи,
  │                      # штатная AI-расшифровка (crm.activity.call.getTranscript)
  ├─ fetch_openlines.py  # чаты открытых линий + история сообщений; classify_sender() — кто писал
  ├─ fetch_tasks.py      # задачи по сущности (tasks.task.list по UF_CRM_TASK = D_<id> / L_<id>)
  ├─ users.py            # справочники с кэшем в data/: пользователи, контакты (ContactBook),
  │                      # стадии, статусы лидов, источники, воронки
  ├─ transcribe.py       # записи с Диска -> audio/ (в прямом режиме — без скачивания),
  │                      # транскрибация -> transcripts/
  ├─ normalize.py        # всё -> единый формат «касание» (touchpoint)
  ├─ store.py            # data/store/: сохранённые карточки, касания, индексы, сырые данные
  ├─ registry.py         # data/registry.json: реестр сущностей (can_reuse, чекпоинты)
  └─ export_llm.py       # export/touchpoints_{entity}_{group}.jsonl,
                         # export/index_{entity}_{group}.jsonl,
                         # export/snapshot/{Сделки|Лиды}/{Активные|...}/<карточка>.md,
                         # export/snapshot/manifest.json, zip-архив снимка
```

- Сущности разделяются на три группы: **active** (`CLOSED = N`), **completed** (`CLOSED = Y`) и **technical** (дубли/тест/мусор). Группировка реализована в `fetch_deals.deal_group` через `is_technical`. Технические сделки не считаются проигранными продажами. Правила задаются в `config.py`: `TECHNICAL_STAGE_IDS` (точные стадии по воронкам) и `TECHNICAL_STAGE_KEYWORDS` (ключевые слова в названии стадии). Каждая группа пишется в свои файлы.
- Если в `config.py` включена только одна из групп `active`/`completed`, фильтр по `CLOSED` уходит прямо в запрос `crm.deal.list`.
- Выгружаются **все поля сущности** (стандартные + `UF_*`). Значения полей-списков резолвятся в названия через метаданные (кэши `data/deal_fields_meta.json`, `data/lead_fields_meta.json`); в markdown-протоколе поля — секцией «Поля сделки»/«Поля лида», в индексе — ключом `fields`.
- `api_client.py` — единственная точка обращения к Bitrix24: `B24Client.call/result/call_list`. POST JSON на `{webhook}/{method}`, пауза `REQUEST_PAUSE` (облако ~2 запроса/сек), ретраи с экспоненциальной задержкой на 429 / `QUERY_LIMIT_EXCEEDED` / `OPERATION_TIME_LIMIT` / сетевых сбоях, пагинация через `start`/`next` (включая ответы вида `{"items": [...]}` и `{"tasks": [...]}`). Ошибки API — исключение `BitrixError`.
- Логирование — стандартный `logging` с логгером `"b24"` (настраивается в `sync.py`).

### Инкрементальная выгрузка

- `EXPORT_TYPE = "auto"`: если есть предыдущий снимок (`data/state.json`) и запуск не тестовый, работает в инкрементальном режиме; иначе — полный.
- Инкремент: `ALL ACTIVE` + всё изменённое с прошлого снимка (`>=DATE_MODIFY`), с перекрытием `INCREMENTAL_OVERLAP_HOURS` (по умолчанию 24 ч) для надёжности. Дубли убираются по ID.
- Повтор ошибок: сущности из `errors.deals`/`errors.leads` прошлого прогона (`state.json`) перечитываются заново (`get_deals_by_ids`).
- Sweep закрытых: в инкременте закрытые сущности проверяются на изменения; по умолчанию (`REEXPORT_CLOSED_ON_CHANGE = False`) неизменённые переиспользуются из store без повторной обработки.
- Тестовые запуски (`--deals`, `--limit`, `--leads`, `--lead-limit`, `DEAL_IDS`, `DEALS_LIMIT`) всегда полный режим и **не сдвигают** якорь `state.json`.
- `state.json` — якорь следующего инкремента: `last_run`, `export_id`, `export_type`, счётчики, `errors.deals`/`errors.leads`.
- `manifest.json` в `export/snapshot/` описывает текущий снимок: `export_type = "FULL_SNAPSHOT"`, `is_complete`/`status`, версия схемы, страны, покрытие, режим обновления (`update_mode`), фильтр (включая секцию лидов), блоки `deals`/`leads` с разбивкой по группам и числом переиспользованных карточек (`reused`), ошибки и пропуски.

### Накопительный снимок (store + registry)

- `store.py` — постоянное хранилище `data/store/`: карточки (`deals/<id>.md`), касания (`tps/<id>.jsonl`), индексные записи (`index/<id>.json`), полный JSON сущности (`deal/<id>.json`); лиды — в подпапке `lead/{deals,tps,index,deal}/`.
- `registry.py` — реестр `data/registry.json` всех когда-либо выгруженных сущностей: ключи `"<id>"` (сделки) и `"lead:<id>"` (лиды), группа, хэш контента, метки времени. `can_reuse()` решает, можно ли переиспользовать карточку без переобработки; чекпоинт registry пишется каждые 25 сущностей — обрыв прогона не теряет прогресс.
- В конце прогона `export_llm` рендерит `export/snapshot/` из store: переиспользованные карточки берутся как есть (переименовываются под текущий шаблон имени), перерендеренные — из свежих данных. Снимок всегда полный (`FULL_SNAPSHOT`), `update_mode` в manifest лишь указывает способ прохода.
- Имена карточек (`card_filename`): сделка — `<FILE_BRAND>_DEAL_<CTRY>_<PIPELINE>_<ID>_SNAP_<ts>_schema-v<N>.md`, лид — `<FILE_BRAND>_LEAD_<CTRY>_<ID>_SNAP_<ts>_schema-v<N>.md` (без воронки). Страна — `deal_country`/`normalize_country` (РБ→RB, РФ→RF) или `FILE_COUNTRY`. Структура каталогов: `export/snapshot/{Сделки|Лиды}/{Активные|Завершенные|Технические}/`.

### Важные архитектурные решения (не откатывать без причины)

- Звонки берутся как CRM-активности (`crm.activity.list`, `TYPE_ID=2`), а не `voximplant.statistic.get`: работает с любой телефонией и надёжно привязано к сделке; `RESPONSIBLE_ID` активности = сотрудник, который фактически говорил (`actor_user_id` в `normalize_call`).
- Записи разговоров — id файлов Диска в поле `FILES` активности (часть порталов не отдаёт его в списочном методе — добирается точечно через `crm.activity.get`).
- Запись учитывается только для состоявшихся разговоров длиннее `MIN_RECORDING_DURATION_SEC` (15 с); пропущенные звонки записей не дают. Если длительность неизвестна, но файл записи есть — запись сохраняется.
- Приоритет транскрипций: штатная расшифровка Bitrix (если `PREFER_BUILTIN_TRANSCRIPT`) → внешний провайдер (`TRANSCRIPTION_PROVIDER`: `assemblyai` / `whisper`). Статус пишется в `content.transcript_status` (`builtin|assemblyai|whisper|no_recording|no_transcript|pending`).
- AssemblyAI по умолчанию получает запись **прямой ссылкой Диска** (`ASSEMBLYAI_DIRECT_URL = True`): `DOWNLOAD_URL` передаётся как `audio_url`, файл локально не скачивается и не заливается — аудио в `audio/` может отсутствовать, это норма. При ошибке прямого режима — автоматический fallback на скачивание + `POST /v2/upload`.
- История чатов: `imopenlines.session.history.get`, при недоступности — fallback на `im.dialog.messages.get`. Отправитель определяется `classify_sender()`: справочник пользователей → `users_info` из ответа API → клиент/система; системные сообщения коннектора отфильтровываются `is_system_message()`; записи чатов без `CHAT_ID` пропускаются.
- `DOWNLOAD_URL` файла Диска уже содержит авторизацию в пути — скачивается как есть, без добавления `auth`.
- Задачи: актор — `RESPONSIBLE_ID` (исполнитель), постановщик — в `content.created_by`.
- Накопительный снимок (store + registry): закрытые сущности по умолчанию не переобрабатываются — не отключайте `REEXPORT_CLOSED_ON_CHANGE` без причины и не удаляйте `data/store/` / `data/registry.json` (это потеря истории снимков).

## Данные и артефакты (всё это в .gitignore, не коммитить)

| Путь | Содержимое |
|---|---|
| `export/touchpoints_{deal\|lead}_{active\|completed\|technical}.jsonl` | Одна строка = одно касание (машинный формат). Схема — в docstring `normalize.py` и README |
| `export/index_{deal\|lead}_{active\|completed\|technical}.jsonl` | Индекс: сущность → число касаний, звонков, чатов, задач + все поля (`fields`) |
| `export/snapshot/{Сделки\|Лиды}/{Активные\|Завершенные\|Технические}/<карточка>.md` | Хронологический протокол сущности — готовый контекст для LLM (`LEXS_DEAL_RB_..._schema-v<N>.md`) |
| `export/snapshot/manifest.json` | Описание текущего снимка (`FULL_SNAPSHOT`, группы с `reused`, ошибки) |
| `export/*.zip` | Итоговый архив снимка (если `MAKE_SNAPSHOT_ZIP = True`); имя по `ZIP_NAME_TEMPLATE` |
| `raw/deal_<id>.json`, `raw/lead_<id>.json` | Сырой JSON из API — позволяет пересобрать нормализацию без повторных запросов |
| `transcripts/`, `audio/` | Тексты транскрипций и записи звонков (имена: `deal<id>_call<activity_id>.txt/.mp3`); в прямом режиме AssemblyAI аудио не скачивается |
| `data/store/` | Постоянное хранилище: карточки, касания, индексы, сырые данные по каждой сущности (лиды — в `lead/`) |
| `data/registry.json` | Реестр выгруженных сущностей (группа, хэш, метки; чекпоинт каждые 25) |
| `data/` | Кэши справочников (`users_cache.json`, `contacts_cache.json`, `stages.json`, `lead_statuses.json`, `sources.json`, `categories.json`, `deal_fields_meta.json`, `lead_fields_meta.json`) и `state.json` (якорь инкремента + `errors`) |

Идемпотентность: скачанные записи и готовые транскрипты не перекачиваются/не пересоздаются (проверка по существованию файла); кэши контактов дозаполняются; store накапливается между прогонами.

## Соглашения по коду

- Все модули начинаются с `# -*- coding: utf-8 -*-` и docstring-описания модуля на русском.
- Простой стиль: функции + dict-и, без классов там, где не нужно; `int()`-приведение ID из API.
- Конфигурация только через `config.py` — не хардкодить пороги/пути/паузы в логике.
- Все сетевые вызовы к Bitrix — только через `B24Client` (не дёргать `requests` напрямую, кроме скачивания файлов и AssemblyAI в `transcribe.py`).
- Файлы пишутся с `ensure_ascii=False`, UTF-8; JSON-запись атомарная через `tmp + os.replace` (см. `users._save_json`, `transcribe.download_recording` — то же для бинарных через `.part`).
- Любые ошибки API по одной сущности/чату/звонку не должны ронять весь прогон — ловить, логировать, продолжать (см. паттерн в `sync.main`); ошибки сущностей попадают в `errors` state.json и повторяются на следующем прогоне.
- Общий код сделок и лидов параметризуется через `_ENTITY` / аргумент `entity` (`"deal"`/`"lead"`) — не дублируйте ветки кода под каждую сущность.
- Добавляя новый источник касаний, проходите через `normalize.py` и обновляйте схему touchpoint; markdown-рендер нового типа добавляйте в `export_llm.deal_markdown`.

## Тестирование

Автотестов нет. Проверка изменений — прогон на малом объёме:

```bash
python sync.py --limit 3 --no-transcribe
python sync.py --leads --lead-limit 3 --no-transcribe   # если затронуты лиды
```

и ручная сверка `export/touchpoints_*.jsonl`, `export/index_*.jsonl` и `export/snapshot/**/*.md`. Для правок нормализации/экспорта можно пересобирать результат из `raw/deal_*.json` без повторных запросов к API.

Перед запуском с внешней транскрибацией убедитесь, что `ASSEMBLYAI_API_KEY` задан; для Whisper — что установлен `faster-whisper` и `ffmpeg` в PATH.

## Безопасность

- `.env` содержит секреты (вебхук с ключом, `ASSEMBLYAI_API_KEY`) — не читать без необходимости и никогда не коммитить; `.gitignore` уже исключает `.env`, `data/`, `export/`, `raw/`, `audio/`, `transcripts/`, `*.json`.
- Вебхук Bitrix — это полный доступ к порталу: ключ из URL вебхука присутствует в ссылках скачивания файлов Диска, не логировать его. В прямом режиме (`ASSEMBLYAI_DIRECT_URL`) этот URL передаётся в AssemblyAI как `audio_url` — тот же уровень доверия, что и загрузка самого аудио.
- Выгрузка содержит персональные данные и содержимое переписок — директории-артефакты (включая `data/store/` и `data/registry.json`) поэтому исключены из git.
- Деплоя нет: скрипт запускается вручную локально; «развёртывание» = установить зависимости, положить `.env`, запустить `sync.py`.

## База знаний (wiki)

Проект имеет поддерживаемую LLM wiki-базу знаний в `wiki/` (концепция: компилируемое знание поверх сырого кода). Структура, конвенции и процессы ведения (ingest/query/lint) — в `wiki/SCHEMA.md`. При изменениях кода обновляйте затронутые страницы wiki, `wiki/index.md` и `wiki/log.md` согласно SCHEMA.md. Точка входа для чтения — `wiki/overview.md` и `wiki/index.md`.
