# Выгрузка касаний Bitrix24 для анализа ИИ

Собирает по каждой сделке **все каналы общения в одну хронологическую ленту**:
звонки (с транскрипциями), переписки открытых линий (Viber / Telegram / WhatsApp и др.),
комментарии таймлайна, смены стадий. В каждом касании фиксируется **тот, кто фактически
вёл диалог** (оператор звонка, автор сообщения) — отдельно от ответственного за сделку.

## Установка

```bat
pip install -r requirements.txt
```

`faster-whisper` нужен только для локальной транскрибации записей. Если на портале
уже включена AI-расшифровка звонков Bitrix — скрипт сначала берёт её, Whisper — запасной вариант.

## Настройка

1. В Bitrix24: **Настройки → Разработчикам → Другое → Входящий вебхук**.
2. Права вебхука: **crm, im, imopenlines, telephony, user**.
3. Подставьте URL вебхука в `config.py` (`WEBHOOK_URL`) или задайте переменную окружения `B24_WEBHOOK`.

## Запуск

```bat
python sync.py --limit 3 --no-transcribe   :: быстрый тест структуры на 3 сделках
python sync.py --deals 1234 5678           :: конкретные сделки
python sync.py                             :: полная выгрузка
python sync.py --skip-whisper              :: только штатные расшифровки Bitrix
```

Объём и состав выгрузки настраиваются в `config.py` (`DATE_FROM`, `INCLUDE_EMAILS` и т.д.).

## Результат

| Путь | Содержимое |
|---|---|
| `export/touchpoints.jsonl` | Одна строка = одно касание (машинный формат для БД/RAG) |
| `export/deals/deal_<id>.md` | Хронологический протокол сделки — готовый контекст для LLM |
| `export/deals_index.jsonl` | Индекс: сделка → число касаний, звонков, чатов |
| `raw/deal_<id>.json` | Сырой JSON из API (пересборка без повторных запросов) |
| `transcripts/`, `audio/` | Тексты транскрипций и записи звонков |

## Схема касания (touchpoints.jsonl)

```json
{
  "deal_id": 1234,
  "touchpoint_id": "call_991",
  "timestamp": "2026-07-15T14:32:10+03:00",
  "channel": "call | viber | telegram | whatsapp | email | comment | meeting",
  "event_type": "call | message | comment | email | meeting | stage_change",
  "direction": "in | out | internal",
  "actor": {"type": "manager | client | bot | system", "user_id": 17, "name": "Иванов И.И."},
  "responsible_user_id": 42,
  "client": {"contact_id": 555, "name": "Петров П."},
  "deal_stage_at_time": {"stage_id": "C1:PREPARATION", "name": "Подготовка"},
  "content": {"type": "transcript", "text": "...", "duration_sec": 143,
              "recording_url": "...", "transcript_status": "builtin | whisper | ..."},
  "source": {"activity_id": 7739, "stat_id": 991}
}
```

## Как решается «кто вёл диалог»

- **Звонок** → `PORTAL_USER_ID` из статистики телефонии (оператор звонка);
- **Сообщение ОЛ** → `senderid` каждого сообщения, резолв через справочник пользователей;
- **Комментарий/письмо** → `AUTHOR_ID`.
Поле `actor` — фактический участник; `responsible_user_id` — ответственный, для сравнения.

## Источники данных (REST API)

`crm.deal.list` · `crm.stagehistory.list` (стадии) · `voximplant.statistic.get` (звонки + записи) ·
`crm.activity.call.getTranscript` (штатная расшифровка) · `imopenlines.crm.chat.get` +
`imopenlines.session.history.get` (чаты) · `crm.timeline.comment.list` · `user.get` (справочник)
