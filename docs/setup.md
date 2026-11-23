# Настройка и запуск

[Все документы](README.md) · [Архитектура](architecture.md) · [Эксплуатация](operations.md)

## Окружение и доступ

Python 3.12, плоские модули в корне, `venv/` и
[requirements.txt](../requirements.txt); пакетной сборки и CI нет.
Проверку кода без вебхука, `.env` и рабочих данных выполняйте по
[инструкции изолированных тестов](testing.md).
Команды выполняются из корня проекта. Если окружения ещё нет:

```text
python -m venv venv
```

| Оболочка | Активация |
|---|---|
| macOS/Linux (sh/zsh/bash) | `source venv/bin/activate` |
| Windows PowerShell | `.\venv\Scripts\Activate.ps1` |
| Windows cmd | `venv\Scripts\activate.bat` |

После активации:

```text
python -m pip install -r requirements.txt
```

`requests` нужен для HTTP-запросов; `python-dotenv` — для загрузки секретов из `.env`;
`loguru` — для [файлового логирования](operations.md#логирование).
Бот имеет [отдельную зависимость](operations.md#telegram-бот).

Создайте входящий вебхук Bitrix24 с правами `crm`, `im`, `imopenlines`,
`telephony`, `user`; для задач добавьте `task`. Секреты храните в файле `.env`
рядом с `config.py`. При импорте `config.py` вызов `load_dotenv()` загружает
значения из этого файла:

```dotenv
B24_WEBHOOK=https://<portal>/rest/<user_id>/<secret>/
ASSEMBLYAI_API_KEY=<assemblyai_key>
```

Первый параметр обязателен для Bitrix, второй — для AssemblyAI. Вебхук даёт
доступ в пределах прав пользователя и scopes. Реальные значения не публикуйте.
`PORTAL_URL` для ссылок карточек выводится из вебхука; в коде есть резервный адрес.

## Команды и побочные эффекты

Ниже приведены команды **реальной выгрузки**. Они не являются изолированными
автотестами, в том числе при `--limit` или `--no-transcribe`.

```text
python sync.py
python sync.py --limit 3 --no-transcribe
python sync.py --deals 1234 5678
python sync.py --leads 4321 8765
python sync.py --limit 3 --lead-limit 3 --no-transcribe
python sync.py --skip-transcribe
```

| Аргумент | Действие |
|---|---|
| Без аргументов | Проход по config; при `EXPORT_TYPE="auto"` возможен инкремент |
| `--limit N` / `--deals ID ...` | Лимит / ID сделок на этот запуск |
| `--lead-limit N` / `--leads ID ...` | Лимит / ID лидов на этот запуск |
| `--no-transcribe` | Отключить все транскрипции, включая штатные |
| `--skip-transcribe` | Отключить AssemblyAI; штатные возможны при `PREFER_BUILTIN_TRANSCRIPT=True` |

Непустые ID или ненулевые лимиты включают тестовый режим: `full` без сохранения
state/registry. Он **вызывает API и пишет данные**: кэши, raw, store, новый
snapshot и возможный ZIP. `--no-transcribe` не запрещает скачивание записей.
Для изоляции нужны отдельные данные/пути и подмена сетевых вызовов.
Сам импорт `config.py` загружает `.env` при доступном `python-dotenv`
и создаёт каталоги данных; изолированные тесты подменяют config до импорта.

Сделки запускаются всегда: лимит лидов их не ограничивает. Для малого прогона
обоих типов задавайте оба лимита. При тесте сделок лиды пропускаются без
собственных ID/лимита; для них также нужен `PROCESS_LEADS=True`.
Один `--leads` без ID тест не включает. `0` и пустые CLI-списки не сбрасывают config.

## Выборка и параметры

| Настройки `config.py` | Назначение / текущее значение |
|---|---|
| `DEAL_IDS`, `LEAD_IDS`; `DEALS_LIMIT`, `LEADS_LIMIT` | ID и лимиты: `[]` / `0` — без ограничений |
| `DATE_FROM` | Нижняя граница создания сущностей и выборки звонков; сейчас `2025-10-29T00:00:00+03:00` |
| `EXPORT_PERIOD_FROM`, `EXPORT_PERIOD_TO` | Явный период создания; обе границы `None` |
| `PROCESS_LEADS`; `PROCESS_ACTIVE/COMPLETED/TECHNICAL` | Лиды и все группы включены |
| `INCLUDE_COMMENTS/TASKS`; `INCLUDE_EMAILS/MEETINGS` | Комментарии/задачи включены, письма/встречи выключены |
| `TECHNICAL_STAGE_IDS/KEYWORDS`; `LEAD_TECHNICAL_STATUS_IDS/KEYWORDS` | Правила технических стадий/статусов |
| `EXPORT_TYPE`, `INCREMENTAL_OVERLAP_HOURS` | `auto` / `full` / `incremental`; перекрытие 24 ч |
| `REEXPORT_CLOSED_ON_CHANGE` | `False`: закрытые переиспользуются даже после изменений |
| `SCHEMA_VERSION`, `SNAPSHOT_TZ`, `MAKE_SNAPSHOT_ZIP` | Схема 2, `Europe/Minsk`, ZIP включён |
| `FILE_BRAND`, `DEAL_COUNTRY_BY_CATEGORY`, `ZIP_NAME_TEMPLATE` | Бренд, соответствие ID воронки и страны сделки, имя архива; [правила страны](data.md#файлы-результата) |
| `REQUEST_PAUSE`, `MAX_RETRIES`, `REQUEST_TIMEOUT` | Пауза 0,55 с, 5 попыток, тайм-аут 60 с |
| `DATA_DIR`, `RAW_DIR`, `AUDIO_DIR`, `TRANSCRIPTS_DIR`, `EXPORT_DIR` | Пути через `os.path` относительно каталога проекта |
| `DEBUG_LOG_PATH`, `ERROR_LOG_PATH` | `debug.log` и `error.log` в корне проекта, независимо от текущего каталога запуска |

Полный режим применяет период только при **обеих** границах: FROM `00:00:00`,
TO `23:59:59`. `DATE_FROM` заменяет начало периода, сохраняя верхнюю границу TO.
Одиночная граница игнорируется. При `None` предыдущая неделя автоматически не
выбирается, несмотря на комментарий config. Инкремент игнорирует FROM/TO,
но учитывает `DATE_FROM` в обеих выборках. Для всей истории — `DATE_FROM=None`.
Один явный ID запрашивается точечным `get` без фильтра дат.

Лимиты применяются к уже полученному списку **после пагинации**, поэтому
`--limit 3` не означает получение только трёх записей по API. Списочные запросы
сделок и лидов всегда выбирают стандартные и пользовательские поля (`*`, `UF_*`);
отдельного переключателя `DEAL_ALL_FIELDS` больше нет.

## Транскрибация

Порядок в `sync.resolve_transcript`: `--no-transcribe` → штатная расшифровка
при `PREFER_BUILTIN_TRANSCRIPT=True` → AssemblyAI при наличии записи,
`TRANSCRIBE_ENABLED=True` и отсутствии `--skip-transcribe`. Сейчас штатный приоритет
выключен, AssemblyAI включён.

- AssemblyAI: ключ `ASSEMBLYAI_API_KEY` из `.env` через `config.py`, язык `ru`,
  подсказка 2 спикера, опрос каждые 3 с, ожидание до 900 с. Настройки `ASSEMBLYAI_*`.

`ASSEMBLYAI_DIRECT_URL=True` передаёт провайдеру авторизованный `DOWNLOAD_URL`
Диска; локального аудио может не быть. При ошибке — скачивание и upload.
`auth` к URL не добавляется. Оба режима передают запись внешнему сервису.

У пропущенных звонков и длительности ≤ `MIN_RECORDING_DURATION_SEC` (15 с)
файлы исключаются, само касание сохраняется. Неизвестная длительность при
наличии файла допускает запись. `DOWNLOAD_RECORDINGS=True` включает обычное
скачивание; этот флаг не запрещает загрузки во всех путях транскрибации.
Существующие аудио и тексты переиспользуются; статусы — в [data.md](data.md).
