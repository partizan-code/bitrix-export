# Данные и снимки

[Все документы](README.md) · [Архитектура](architecture.md) · [Проверка результата](operations.md)

## Файлы результата

`entity` — `deal` / `lead`, `group` — `active` / `completed` / `technical`.

| Путь | Содержимое |
|---|---|
| `export/snapshot/{Сделки,Лиды}/{Активные,Завершенные,Технические}/` | Markdown-карточки; все шесть каталогов создаются даже пустыми |
| `export/snapshot/manifest.json` | Охват и результат сборки |
| `export/*.zip` | Содержимое snapshot, manifest в корне; пустые каталоги включены, `.DS_Store` / `._*` исключены |
| `export/touchpoints_<entity>_<group>.jsonl` | Строка — касание; вне ZIP |
| `export/index_<entity>_<group>.jsonl` | Строка — индекс сущности; вне ZIP |
| `raw/<entity>_<id>.json` | Сущность и собранные источники для повторной нормализации |
| `audio/`, `transcripts/` | Кэши записей/текстов; `deal<id>_call<activity_id>.*` используется и для лидов |
| `data/store/` | Последние артефакты, структура ниже |
| `data/registry.json` | Реестр: ключ `<id>` сделки, `lead:<id>` лида |
| `data/state.json` | `last_run`, `export_id`, `export_type`, счётчики, `errors.deals/leads` |
| `data/progress.json` | Этап, счётчики, ошибки; в конце итог и имя ZIP |
| `data/deal_fields_meta.json`, `data/lead_fields_meta.json` | Раздельные кэши метаданных, обновляемые общим загрузчиком атомарно |
| `data/*.json` | Кэши пользователей/контактов, стадий/статусов, источников, воронок, метаданных полей |

В store сделки лежат в `deals/<id>.md`, `tps/<id>.jsonl`, `index/<id>.json`,
`deal/<id>.json`; лиды — в тех же подкаталогах внутри `data/store/lead/`.
`store.has_deal` требует все четыре файла. Raw включает историю стадий,
нормализованные звонки, чаты и включённые источники, а не все ответы API.
Общей CLI-пересборки из raw нет; для имён — [refresh_names.py](operations.md#обслуживание).

Атомарность кэшей метаданных не означает атомарность всей выгрузки: четыре
файла store записываются последовательно, а state и registry — напрямую.
`has_deal` проверяет наличие файлов, не их читаемость или согласованность.
Порядок резервирования и проверки после обрыва — в
[инструкции эксплуатации](operations.md#хранение-и-безопасность).

Имена карточек:

```text
<BRAND>_DEAL_<CTRY>_<PIPELINE>_<ID>_SNAP_<YYYYMMDD>T<HHMMSS><TZ>_schema-v<N>.md
<BRAND>_LEAD_<CTRY>_<ID>_SNAP_<YYYYMMDD>T<HHMMSS><TZ>_schema-v<N>.md
```

Бренд — `FILE_BRAND`; страна — `COUNTRY_FIELD` или поле «Страна»
(РБ → `RB`, РФ → `RF`), запасная — `FILE_COUNTRY`. У лидов нет воронки.
Один `ExportContext` задаёт время карточек, manifest и ZIP в `SNAPSHOT_TZ`.
`ZIP_NAME_TEMPLATE` использует `{stamp}` = `ГГГГ-ММ-ДД ЧЧ-ММ`: повторная сборка
в ту же минуту может перезаписать архив.

## Касание

Схема — в [normalize.py](../normalize.py). Пример звонка:

```json
{
  "deal_id": 1234,
  "touchpoint_id": "call_991",
  "timestamp": "2026-07-15T14:32:10+03:00",
  "channel": "call",
  "event_type": "call",
  "direction": "in",
  "actor": {"type": "manager", "user_id": 17, "name": "Иванов И.И."},
  "responsible_user_id": 42,
  "client": {"contact_id": 555, "name": "Петров П."},
  "deal_stage_at_time": {"stage_id": "C1:PREPARATION", "name": "Подготовка"},
  "content": {"type": "transcript", "text": "Текст разговора",
              "duration_sec": 143, "recording_url": null,
              "transcript_status": "assemblyai"},
  "source": {"activity_id": 991, "file_ids": [512]}
}
```

`deal_id` содержит ID **и у лидов**: тип определяйте по файлу/контексту.
`deal_stage_at_time` вычисляется по истории на момент события или равен `null`.
`responsible_user_id` — ответственный за сущность, а `actor` определяется так:

| Событие | `touchpoint_id` | Источник actor |
|---|---|---|
| Звонок | `call_<activity_id>` (или `stat_id`) | `RESPONSIBLE_ID` активности, при отсутствии — `AUTHOR_ID` |
| Сообщение ОЛ | `ol_<chat_id>_<msg_id>` | Отправитель через `classify_sender` |
| Комментарий | `comment_<id>` | `AUTHOR_ID` |
| Письмо / встреча | `act_<id>` | `AUTHOR_ID` |
| Задача | `task_<id>` | Исполнитель `RESPONSIBLE_ID`; постановщик — `content.created_by` |
| Смена стадии | `stage_<i>_<stage_id>` | `system` |

- `channel`: `call`, код ОЛ (`viber`, `telegram`, `whatsapp`, `site_chat`, `vk`,
  `instagram`, `facebook`, `avito`, `olx` и др.), `comment`, `email`, `meeting`, `task`, `crm`.
- `event_type`: `call`, `message`, `comment`, `email`, `meeting`, `task`, `stage_change`.
- `direction`: `in`, `out`, `internal`, `unknown`, `null`; `actor.type`:
  `manager`, `client`, `bot`, `system`, `unknown`; `content.type`: `text`, `transcript`, `meta`.
- `source`: `activity_id`, `chat_id` + `message_id`, `comment_id` или `task_id`;
  у звонков также `file_ids` и возможный `stat_id`.
- Content звонка: телефон, длительность, пропуск, путь аудио, комментарий;
  `recording_url` текущего CRM-потока — `null`. Задачи: описание, статус, дедлайн,
  дата закрытия. Стадии: `stage_from` / `stage_to`.

`transcript_status`: `builtin` / `assemblyai` / `whisper` — текст получен;
`no_recording` — нет подходящей записи; `no_transcript` — внешний путь не дал
текста; `pending` — получение отключено/не запрашивалось, без обещания фонового задания.

Индекс: `deal_id` **или** `lead_id`, `entity`, заголовок, даты, ответственный,
контакт, источник, страна, стадия, `closed`, группа, количества `touchpoints`,
`calls`, `ol_chats`, `tasks`, `fields`, при переносе — `reused`.
Все стандартные/пользовательские поля выгружаются, значения списков и справочников
разрешаются через метаданные. В карточке — «Поля сделки» / «Поля лида».

## Manifest и границы полноты

| Поля | Смысл |
|---|---|
| `export_id`, `schema_version`, `snapshot_at`, `snapshot_timezone` | Идентификация/время |
| `export_type` | Всегда `FULL_SNAPSHOT` — накопленный снимок |
| `update_mode`, `previous_export_id`, `previous_snapshot_at`, `changed_since`, `overlap_from` | Способ получения (`FULL` / `INCREMENTAL`), прошлый якорь и окно |
| `countries`, `pipelines`, `coverage` | Страны, воронки; от старейшего объекта результата до снимка |
| `filter` | Даты, группы, источники, правила технических; `leads.enabled`; `per_entity.deals/leads`: `ids`, `limit`, `limit_reached` |
| `deals`, `leads` | Total, счётчики групп/файлов, ID, `groups.<group>.count/reused/ids`, ошибки; все группы, даже пустые |
| `errors`, `errors_count`, `skipped` | Ошибки и пропуски |
| `is_complete`, `status` | Нет зарегистрированных ошибок — `true` / `complete`, иначе `false` / `partial` |

**`FULL_SNAPSHOT` и `is_complete=true` не гарантируют охват всей CRM.** Лимиты,
отключённые группы/источники, пропуски и перехваченные ошибки источников сами
по себе не снимают флаг полноты. Проверяйте filter, coverage, skipped и логи.
Период manifest описывает настройки; фактическая выборка зависит от режима и
приоритета `DATE_FROM`. Оба типа сущностей проверяйте отдельно.
Также сопоставляйте флаги групп с фактическими `groups.<group>.ids`:
[перенос из текущей выборки](architecture.md#режимы-и-переиспользование)
может сохранить записи отключённой группы.

При пустом блоке `leads` текущий рендер может записать `deal_ids: []` вместо
`lead_ids: []`: тип определяется по записям индекса. Для пустого блока
ориентируйтесь на имя блока, `total` и `groups`, а не только на ключ списка ID.

При `REEXPORT_CLOSED_ON_CHANGE=False` перенесённые карточки могут не отражать
последующие изменения. Full ограничен выборкой, инкремент добавляет пригодные
старые закрытые из store. [Подробности](architecture.md#режимы-и-переиспользование).
