# -*- coding: utf-8 -*-
"""
Конфигурация выгрузки Bitrix24.

Единственное обязательное действие: подставить свой WEBHOOK_URL.
Вебхук создаётся: Настройки -> Разработчикам -> Другое -> Входящий вебхук.
Необходимые права (scopes): crm, im, imopenlines, telephony, user
"""

import os

# подхватываем .env рядом с config.py, если установлен python-dotenv
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# ---------------------------------------------------------------- доступ
WEBHOOK_URL = os.environ.get("B24_WEBHOOK")  # переменная окружения или .env

# ---------------------------------------------------------------- объём выгрузки
DEAL_IDS = []          # [] = все сделки; иначе список ID, напр. [1234, 5678]
DATE_FROM = "2026-07-01T00:00:00+03:00"  # None = без ограничения
DEALS_LIMIT = 20       # 0 = без лимита; 20 — для тестовой выборки

# ---------------------------------------------------------------- состав выгрузки
INCLUDE_EMAILS = False     # письма (активности TYPE_ID=3)
INCLUDE_COMMENTS = True    # комментарии таймлайна (важный контекст для ИИ)
INCLUDE_MEETINGS = False   # встречи (TYPE_ID=1)
INCLUDE_TASKS = True       # задачи по сделке (требует scope task в вебхуке)

# Источник сделки (штатное поле SOURCE_ID) выводится в заголовок протокола.
# Название резолвится из справочника crm.status.list (ENTITY_ID=SOURCE).

# ---------------------------------------------------------------- разделение по стадиям
# Активные, завершённые и технические сделки собираются в отдельные выгрузки.
PROCESS_ACTIVE = True       # CLOSED = N
PROCESS_COMPLETED = True    # CLOSED = Y
PROCESS_TECHNICAL = True    # технические финальные стадии (дубли/тесты/мусор)

# Классификация технических сделок — важно для конверсии:
# технический дубль НЕ должен считаться проигранной продажей.
# 1) Явное правило по воронкам: {ID воронки: [ID технических стадий, ...]}
#    напр. TECHNICAL_STAGE_IDS = {"0": ["C1:JUNK"], "2": ["C2:LOSE"]}
TECHNICAL_STAGE_IDS = {}
# 2) Подстраховка: ключевые слова в НАЗВАНИИ стадии (регистронезависимо).
#    Срабатывает, если название финальной стадии содержит любое из слов.
TECHNICAL_STAGE_KEYWORDS = ["дубль", "тест", "битое", "битый", "мусор",
                            "ошибка", "junk", "spam", "несуществующ"]

# Выгружаем ВСЕ поля сделки (стандартные + UF_*), enum-значения
# резолвятся в названия через метаданные полей.
DEAL_ALL_FIELDS = True

# ---------------------------------------------------------------- снимок / поставка
SCHEMA_VERSION = 2          # версия схемы выгрузки (идёт в имена файлов и manifest)
# Период выгрузки. None = предыдущая календарная неделя (пн–вс) от даты снимка.
EXPORT_PERIOD_FROM = None   # напр. "2026-07-27"
EXPORT_PERIOD_TO = None     # напр. "2026-08-02"
# Запаковывать export/deals в итоговый zip после прохода
MAKE_SNAPSHOT_ZIP = True

# Инкрементальная (недельная) выгрузка.
# "auto" — если есть предыдущий снимок (data/state.json), работает инкрементально:
#   ALL ACTIVE + всё изменённое с прошлого снимка (новые/закрытые/технические);
# "full" — всегда полный проход по фильтру периода;
# "incremental" — принудительно инкрементально (без прошлого снимка = full).
EXPORT_TYPE = "auto"
# Страховочное перекрытие окна изменений (часы): запрашиваем изменения
# с (прошлый снимок - overlap), дубли убираются по ID.
INCREMENTAL_OVERLAP_HOURS = 24

# Префиксы в именах файлов (для файловой индексации в LEX'S).
FILE_BRAND = "LEXS"       # LEXS_DEAL_... / LEXS_SALES_...
FILE_COUNTRY = "RB"       # код страны в имени файла и manifest

# ---------------------------------------------------------------- транскрибация
TRANSCRIBE_ENABLED = True
PREFER_BUILTIN_TRANSCRIPT = False  # True — сначала брать штатную AI-расшифровку Bitrix

# Провайдер: "assemblyai" (облако, с разделением спикеров) или "whisper" (локально)
TRANSCRIPTION_PROVIDER = "assemblyai"

# AssemblyAI. Ключ безопаснее задать переменной окружения ASSEMBLYAI_API_KEY
# (или в файле .env рядом с config.py)
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
ASSEMBLYAI_LANGUAGE = "ru"
ASSEMBLYAI_SPEAKERS = 2        # подсказка диаризации: менеджер + клиент
ASSEMBLYAI_POLL_INTERVAL = 3   # секунд между опросами статуса
ASSEMBLYAI_TIMEOUT = 900       # максимум ожидания результата, сек

# Faster Whisper (если TRANSCRIPTION_PROVIDER = "whisper")
WHISPER_MODEL = "small"    # tiny / base / small / medium / large-v3
WHISPER_DEVICE = "auto"    # auto / cpu / cuda
WHISPER_LANGUAGE = "ru"

# Запись звонка учитываем только для состоявшихся разговоров
# длиннее MIN_RECORDING_DURATION_SEC — остальные игнорируются
# (не показываем, не скачиваем, не транскрибируем).
MIN_RECORDING_DURATION_SEC = 15

# Скачивать записи состоявшихся звонков в audio/
DOWNLOAD_RECORDINGS = True

# ---------------------------------------------------------------- сеть
REQUEST_PAUSE = 0.55       # пауза между запросами (облако: ~2 запроса/сек)
MAX_RETRIES = 5
REQUEST_TIMEOUT = 60

# ---------------------------------------------------------------- пути
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")           # кэши, состояние синхронизации
RAW_DIR = os.path.join(BASE_DIR, "raw")             # сырой JSON из API
AUDIO_DIR = os.path.join(BASE_DIR, "audio")         # записи звонков
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")  # тексты транскрипций
EXPORT_DIR = os.path.join(BASE_DIR, "export")       # итог: jsonl + md для ИИ

for _d in (DATA_DIR, RAW_DIR, AUDIO_DIR, TRANSCRIPTS_DIR, EXPORT_DIR):
    os.makedirs(_d, exist_ok=True)