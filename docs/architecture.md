# Архитектура

[Все документы](README.md) · [Настройка](setup.md) · [Форматы данных](data.md)

## Поток и модули

`sync.main` отклоняет аргументы запуска; параметры берутся только из `config.py`.
Затем загружает справочники/state, выбирает режим, создаёт `ExportContext`
и очищает snapshot. Для каждой сущности `run_entity` выбирает обработку или
перенос; `process_entity` собирает источники → нормализует → сохраняет raw,
карточку, касания и индекс в store. Затем формируются групповые JSONL, manifest,
ZIP (если включён и результат непустой), прогресс и, вне теста, state/registry.

Различия в `_ENTITY`: owner_type 2/1, задачи `D_`/`L_`, стадия
`STAGE_ID`/`STATUS_ID`. Логирование — Loguru с общей настройкой в
`logging_setup.py`; [состав и уровни логов](operations.md#логирование).

| Модуль | Ответственность / API |
|---|---|
| [api_client.py](../api_client.py) | `B24Client.call/result/call_list`, POST JSON, пагинация `start/next`, массивы и `items`/`tasks` |
| [performance.py](../performance.py) | Профиль одного `sync.main`: монотонные таймеры, счётчики API/аудио, итоговые агрегаты в Loguru; [интерпретация](operations.md#время-выгрузки-и-запросов) |
| [fetch_deals.py](../fetch_deals.py), [fetch_leads.py](../fetch_leads.py) | `crm.deal/lead.list/get/fields`, поля `*` + `UF_*`, группы, инкремент; `crm.stagehistory.list`, `stage_at` |
| [fetch_activities.py](../fetch_activities.py) | `crm.activity.list/get`: звонки TYPE_ID=2, письма 3, встречи 1; `crm.timeline.comment.list` |
| [fetch_openlines.py](../fetch_openlines.py) | `imopenlines.crm.chat.get`, `imopenlines.session.history.get`, fallback `im.dialog.messages.get` |
| [fetch_tasks.py](../fetch_tasks.py) | `tasks.task.list`, `UF_CRM_TASK=D_<id>` / `L_<id>` |
| [users.py](../users.py) | `user.get`, `crm.contact.get`, `crm.status.list`, `crm.category.list`; кэши, `ContactBook`, `LazyUserMap` |
| [transcribe.py](../transcribe.py) | `disk.file.get`, аудио, AssemblyAI, кэш текстов |
| [scripts/diag_download.py](../scripts/diag_download.py) | Ограниченная диагностика чтения аудио через `transcribe.probe_recording`, HTTP Range и curl; отдельный безопасный лог |
| [scripts/diag_fields.py](../scripts/diag_fields.py), [scripts/diag_filter.py](../scripts/diag_filter.py) | Ручная диагностика метаданных и выборки через API |
| [scripts/refresh_names.py](../scripts/refresh_names.py), [scripts/recover_registry.py](../scripts/recover_registry.py) | Ручное обновление имён и восстановление реестра |
| [normalize.py](../normalize.py) | Схема касания, события стадий |
| [store.py](../store.py), [registry.py](../registry.py) | Артефакты и переиспользование |
| [export_llm.py](../export_llm.py) | JSONL, Markdown, manifest, ZIP, имена/время снимка |
| [source_sqlite.py](../source_sqlite.py) | Структурированный SQLite по исходным JSON/JSONL, функция `write_snapshot`; CLI — [scripts/source_sqlite.py](../scripts/source_sqlite.py), без API/config |
| [snapshot_sqlite.py](../snapshot_sqlite.py) | Накопительная БД конечных карточек ZIP; версии без повторов, связи со слепками, проверка восстановления; вызывается из sync, CLI — [scripts/snapshot_sqlite.py](../scripts/snapshot_sqlite.py) |
| [parse_snapshot.py](../parse_snapshot.py) | Разбор полей и событий Markdown schema-v2, сохранение оформления для точного восстановления |
| [import_snapshot.py](../import_snapshot.py) | Библиотека импорта готового ZIP в SQLite, без API/config; CLI — [scripts/import_snapshot.py](../scripts/import_snapshot.py), [схема](data.md#sqlite) |
| [bot.py](../bot.py) | Запуск `sync.py` без аргументов, прогресс и выдача ZIP в Telegram |
| [logging_setup.py](../logging_setup.py) | Два общих файла Loguru либо один через `configure_logging(diagnostic_path=...)`, маскирование секретов, перехват warning/error библиотек, аварийное завершение процесса |

Клиент повторяет сетевые сбои, HTTP 429, `QUERY_LIMIT_EXCEEDED` и
`OPERATION_TIME_LIMIT` с экспоненциальной задержкой; прочие ошибки API и
исчерпание попыток дают `BitrixError`. Параметры сети — в config.

При изменении интеграции используйте [документацию Bitrix24 через MCP](bitrix-api.md).
[Сверка текущих запросов](bitrix-api-audit.md) описывает найденные ограничения
полноты истории, разбора ответов и повторов; исправления в код пока не внесены.

Основные `bot.py` и `sync.py` запускаются из корня. Служебные команды в
`scripts/` добавляют корень в путь импорта при прямом запуске; `scripts`
не требует `__init__.py` или установки пакета. Библиотечные SQLite-модули
в корне самостоятельно не запускаются. Относительные пути аргументов команд
остаются относительно текущего рабочего каталога.

Разбор метаданных сделок и лидов общий:
`fetch_deals.load_fields_meta(client, entity)`. Значение `entity` — `deal` или
`lead`; оно задаёт метод `crm.<entity>.fields` и отдельный кэш. Публичные
`load_deal_fields_meta(client)` / `load_lead_fields_meta(client)` сохранены.
Кэши `deal_fields_meta.json` / `lead_fields_meta.json` обновляются через `.tmp`
и `os.replace`; при ошибке записи прежний кэш сохраняется, временный удаляется.

[Изолированные тесты](testing.md) проверяют основной поток и сравнивают полную
и инкрементальную выгрузки с эталонами; изменения эталонов описаны там же.

## Внутренние интерфейсы после очистки

Неиспользуемые аргументы удалены; актуальные сигнатуры:

| Функция | Сигнатура |
|---|---|
| `export_llm.write_deals_index` | `(deals_meta, path=None)` |
| `normalize.tp_from_ol_message` | `(deal, chat, msg, sender, history, stages_map)` |
| `transcribe.resolve_file_url` | `(file_info)` |
| `sync.resolve_transcript` | `(client, deal_id, call)` |

Объект CLI-аргументов удалён также из `sync.run_entity` и `sync.process_entity`.
Транскрибация выполняется только через AssemblyAI и управляется `TRANSCRIBE_ENABLED`.
Обёртка `logging_setup.run_cli` сохранена для логирования и кодов завершения
бота, выгрузки и служебных скриптов.

Все вызовы в основном потоке и утилитах обслуживания обновлены. Внешние
скрипты, использующие прежние позиционные аргументы, нужно привести к этим
сигнатурам. Функция `users.stage_name`, свойство `B24Client.auth_key` и константы
`DEAL_ALL_FIELDS`, `fetch_leads.OWNER_TYPE`, `export_llm._ID_KEY` удалены.
`resolve_file_url` возвращает готовый `DOWNLOAD_URL` без добавления авторизации.

## Источники и группы

Звонки — CRM-активности вместо статистики телефонии: привязка к сущности не
зависит от провайдера. Записи — ID Диска в `FILES`; при отсутствии в списке
поле добирается через `crm.activity.get`. [Правила транскрибации](setup.md#транскрибация).

`classify_sender` использует сотрудников → `users_info` → клиента/систему.
Пустые и служебные сообщения (`is_system_message`), чаты без `CHAT_ID`
пропускаются. IM-fallback ограничен 20 страницами. [Определение actor](data.md#касание).

Сделки сначала проверяются на техническую стадию, затем делятся по `CLOSED`.
Техническая сделка не обязательно закрыта. При одной включённой группе
active/completed списочный запрос сделок дополнительно фильтруется по `CLOSED`.
У лидов семантика `P` — активный, `S/F` — закрытый; технические — только закрытые
по настроенным ID/словам. Технические записи не считаются проигранными продажами.

## Режимы и переиспользование

- `auto` выбирает инкремент при читаемом `last_run` в state вне теста, иначе
  `full`. Принудительный `incremental` без якоря или при тесте тоже становится `full`.
- Инкремент объединяет активные и изменённые по `DATE_MODIFY` с прошлого
  `last_run` минус перекрытие, убирая дубли по ID. Активная ветка сделок зависит
  от `PROCESS_ACTIVE`; у лидов запрашивается всегда, группы отбираются далее.
  [Фильтры дат](setup.md#выборка-и-параметры).
- ID ошибок прошлого запуска повторяются вне окна изменений. Основной цикл
  регистрирует ошибки сущностей и продолжает; ошибки отдельных источников
  могут перехватываться с warning и не попадать в manifest.
- Активные обрабатываются заново. Закрытые/технические из выборки переносятся
  при прежней и новой закрытой группе, совпадающей схеме и четырёх файлах store.
  `REEXPORT_CLOSED_ON_CHANGE=False` допускает перенос изменённых; `True` —
  только неизменённых по `DATE_MODIFY`.
- Инкремент добирает остальные пригодные закрытые записи из registry/store
  локально, без проверки каждой в API. В `full` добора нет, состав задаётся
  выборкой, но подходящие закрытые всё ещё переиспользуются.

При переносе готовых закрытых сущностей **из текущей выборки** флаги групп
повторно не проверяются. Поэтому отключённая группа может остаться в снимке,
если её записи переиспользованы из store. При новой обработке и доборе старых
записей из registry флаги проверяются; итоговый состав нужно сверять с manifest.

`full` не принуждает повторную транскрибацию. Новая `SCHEMA_VERSION` запрещает
перенос старой схемы; для пересборки всей истории нужен полный проход с нужным
фильтром, поскольку инкремент не запрашивает все старые закрытые сущности.
Store содержит последние версии; история снимков — в отдельных ZIP.

Store пишется по сущности; registry — каждые 25 итераций и в конце обычного
прогона. Реестр содержит тип, группу, стадию, `date_modify`, метки экспорта/снимка,
версию схемы; хэша контента нет. `can_reuse` проверяет схему, файлы — отдельно.
[Восстановление](operations.md#обслуживание).
