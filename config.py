#!/usr/bin/env python3
"""Настройки выгрузки Bitrix24; команды запуска — в docs/setup.md.

Секреты задаются в окружении или .env рядом с этим файлом. При BITRIX_ENV=dev
сначала загружается .env.dev с реквизитами тестового Telegram-бота.
Права вебхука: crm, im, imopenlines, telephony, user;
для INCLUDE_TASKS также нужен task.

При импорте загружаются настройки окружения и создаются рабочие каталоги.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENVIRONMENT = os.environ.get("BITRIX_ENV", "production")
if ENVIRONMENT not in {"production", "dev"}:
    raise ValueError("BITRIX_ENV должен быть production или dev")

# Приоритет: окружение процесса > .env.dev (только dev) > .env.
try:
    from dotenv import load_dotenv

    if ENVIRONMENT == "dev":
        _dev_env = os.path.join(BASE_DIR, ".env.dev")
        if not os.path.isfile(_dev_env):
            raise FileNotFoundError(
                "Для BITRIX_ENV=dev нужен .env.dev рядом с config.py"
            )
        load_dotenv(_dev_env)
        if not all(
            os.environ.get(key, "").strip() for key in ("TG_BOT_TOKEN", "TG_CHAT_ID")
        ):
            raise ValueError(
                "Задайте TG_BOT_TOKEN и TG_CHAT_ID в .env.dev или окружении"
            )
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    if ENVIRONMENT == "dev":
        raise RuntimeError(
            "Для .env.dev установите зависимости: mise run setup"
        ) from None

# Доступ и ссылки на портал
WEBHOOK_URL = os.environ.get("B24_WEBHOOK")

# Адрес портала для ссылок в карточках: из вебхука или резервный адрес.
_wh = (WEBHOOK_URL or "").strip()
if _wh.startswith("http") and "://" in _wh:
    _scheme, _hostpath = _wh.split("://", 1)
    PORTAL_URL = f"{_scheme}://{_hostpath.split('/', 1)[0]}"
else:
    PORTAL_URL = "https://lexs.bitrix24.by"

# Выборка сделок и лидов
# Непустые ID или ненулевые лимиты включают full без сохранения state/registry.
# Лимиты обрезают результат после пагинации, а не число запросов к API.
DEAL_IDS = []  # [] = без фильтра ID; пример: [1234, 5678]
DEALS_LIMIT = 0  # 0 = без лимита
PROCESS_LEADS = True  # включить выгрузку лидов
LEAD_IDS = []  # [] = без фильтра ID
LEADS_LIMIT = 0  # 0 = без лимита

# Нижняя граница создания сделок/лидов и даты звонков; None = без этой границы.
DATE_FROM = "2025-10-29T00:00:00+03:00"
# Период создания для full: нужны обе даты (ГГГГ-ММ-ДД); None = без периода.
# DATE_FROM заменяет начало периода. Инкремент игнорирует период.
EXPORT_PERIOD_FROM = None  # пример: "2026-07-27"
EXPORT_PERIOD_TO = None  # пример: "2026-08-02"

# Состав касаний (звонки, чаты и история стадий собираются всегда)
INCLUDE_EMAILS = False  # письма (TYPE_ID=3)
INCLUDE_COMMENTS = True  # комментарии таймлайна
INCLUDE_MEETINGS = False  # встречи (TYPE_ID=1)
INCLUDE_TASKS = True  # задачи; без права task пропускаются с предупреждением

# Группы выгрузки (общие для сделок и лидов)
PROCESS_ACTIVE = True  # активные
PROCESS_COMPLETED = True  # завершённые, кроме технических
PROCESS_TECHNICAL = True  # дубли, тесты, мусор — отдельно от проигранных продаж

# Технические сделки: совпадение ID стадии или ключевого слова в её названии.
# Ключи — строки с ID воронок; "*" — резервное правило для всех воронок.
# Пример: {"2": ["C2:LOSE"]}. Ключевые слова проверяются без учёта регистра.
TECHNICAL_STAGE_IDS = {}
TECHNICAL_STAGE_KEYWORDS = [
    "дубль",
    "тест",
    "битое",
    "битый",
    "мусор",
    "ошибка",
    "junk",
    "spam",
    "несуществующ",
]

# Технические лиды: только закрытые, по ID статуса или слову в его названии
# без учёта регистра. Остальные закрытые, включая конверсию, — завершённые.
LEAD_TECHNICAL_STATUS_IDS = []
LEAD_TECHNICAL_STATUS_KEYWORDS = ["дубль", "тест", "битое", "битый", "мусор", "корзина"]

# Режим обновления накопительного снимка
# auto: инкремент при наличии data/state.json, иначе full.
# full: полная выборка по фильтрам дат.
# incremental: активные + изменённые с прошлого снимка; без state — full.
EXPORT_TYPE = "auto"
INCREMENTAL_OVERLAP_HOURS = 24  # перекрытие окна изменений, ч

# Закрытые/технические сделки и лиды учитываются в data/registry.json.
# False: переносить из store без переобработки, даже после изменений.
# True: переобрабатывать изменившиеся. Без store или при старой схеме — заново.
REEXPORT_CLOSED_ON_CHANGE = False

# Формат снимка и имена файлов
SCHEMA_VERSION = 2  # версия схемы в именах файлов и manifest
SNAPSHOT_TZ = "Europe/Minsk"  # единое время для manifest, карточек и ZIP
MAKE_SNAPSHOT_ZIP = True  # архивировать export/snapshot после выгрузки
SNAPSHOT_SQLITE_ENABLED = True  # сохранять конечные карточки ZIP в накопительную БД
ZIP_NAME_TEMPLATE = "LEXS выгрузка из Битрикс24 на дату {stamp}"  # без расширения
# {stamp} — дата снимка в SNAPSHOT_TZ, формат «ГГГГ-ММ-ДД ЧЧ-ММ».
FILE_BRAND = "LEXS"  # префикс имён: LEXS_DEAL_... / LEXS_SALES_...

# Служебная страна сделки — по ID воронки; лида — по префиксу _source.
# Неизвестная страна — UNKNOWN; исходное поле «Страна» не изменяется.
DEAL_COUNTRY_BY_CATEGORY = {
    0: "RB",  # РБ OFF
    34: "RB",  # РБ ON
    36: "RB",  # РБ Отложенный спрос
    6: "RB",  # Кухни ON+OFF
    28: "RF",  # Россия OFF
    32: "RF",  # Россия ON
    38: "RF",  # РФ Отложенный спрос
}

# Записи звонков и транскрибация
# У пропущенных и коротких звонков запись исключается, само касание сохраняется.
# При неизвестной длительности запись допускается, если есть файл.
MIN_RECORDING_DURATION_SEC = 15  # запись нужна при длительности > порога, с
# Транскрибация может скачивать записи независимо от DOWNLOAD_RECORDINGS.
DOWNLOAD_RECORDINGS = True  # скачивать записи в audio/
TRANSCRIBE_ENABLED = True  # транскрибация через AssemblyAI
PREFER_BUILTIN_TRANSCRIPT = False  # сначала Bitrix AI, независимо от TRANSCRIBE_ENABLED

# AssemblyAI: ключ из окружения или .env.
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
ASSEMBLYAI_LANGUAGE = "ru"
ASSEMBLYAI_SPEAKERS = 2  # ожидаемые спикеры: менеджер и клиент
ASSEMBLYAI_POLL_INTERVAL = 3  # интервал опроса статуса, с
ASSEMBLYAI_TIMEOUT = 900  # ожидание результата, с
# True: передавать AssemblyAI авторизованную ссылку Диска; при ошибке — файл.
# False: скачивать запись в audio/ и загружать файлом.
ASSEMBLYAI_DIRECT_URL = False

# Сеть
REQUEST_PAUSE = 0.55  # пауза между запросами к Bitrix24, с
MAX_RETRIES = 5  # максимум попыток запроса к Bitrix24
REQUEST_TIMEOUT = 60  # тайм-аут HTTP-запроса, с

# Пути относительно каталога проекта
DEBUG_LOG_PATH = os.path.join(BASE_DIR, "debug.log")  # DEBUG и выше, включая ошибки
ERROR_LOG_PATH = os.path.join(BASE_DIR, "error.log")  # только ERROR и CRITICAL
DATA_DIR = os.path.join(BASE_DIR, "data")  # кэши, состояние синхронизации
RAW_DIR = os.path.join(BASE_DIR, "raw")  # сырой JSON из API
AUDIO_DIR = os.path.join(BASE_DIR, "audio")  # записи звонков
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")  # тексты транскрипций
EXPORT_DIR = os.path.join(BASE_DIR, "export")  # итог: jsonl + md для ИИ
SNAPSHOT_SQLITE_PATH = os.path.join(DATA_DIR, "sqlite3_snapshots.db")

for _d in (DATA_DIR, RAW_DIR, AUDIO_DIR, TRANSCRIPTS_DIR, EXPORT_DIR):
    os.makedirs(_d, exist_ok=True)
