import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- доступ
WEBHOOK_URL = os.environ.get("B24_WEBHOOK")

# ---------------------------------------------------------------- объём выгрузки
DEAL_IDS = []# [] = все сделки; иначе список ID, напр. [1234, 5678]
DATE_FROM = "2026-07-01T00:00:00+03:00"       # напр. "2025-01-01T00:00:00+03:00"; None = без ограничения
DEALS_LIMIT = 50        # 0 = без лимита; для первого теста удобно поставить 3-5

# ---------------------------------------------------------------- состав выгрузки
INCLUDE_EMAILS = False     # письма (активности TYPE_ID=3)
INCLUDE_COMMENTS = True    # комментарии таймлайна (важный контекст для ИИ)
INCLUDE_MEETINGS = False   # встречи (TYPE_ID=1)

# ---------------------------------------------------------------- разделение по стадиям
# Активные и завершённые сделки собираются в отдельные выгрузки.
PROCESS_ACTIVE = True       # CLOSED = N
PROCESS_COMPLETED = True    # CLOSED = Y

# Выгружаем ВСЕ поля сделки (стандартные + UF_*), enum-значения
# резолвятся в названия через метаданные полей.
DEAL_ALL_FIELDS = True

# ---------------------------------------------------------------- транскрибация
TRANSCRIBE_ENABLED = True
PREFER_BUILTIN_TRANSCRIPT = False  # сначала пробуем штатную расшифровку Bitrix (AI)

# Провайдер: "assemblyai" (облако, с разделением спикеров) или "whisper" (локально)
TRANSCRIPTION_PROVIDER = "assemblyai"


ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
ASSEMBLYAI_LANGUAGE = "ru"
ASSEMBLYAI_SPEAKERS = 2        # подсказка диаризации: менеджер + клиент
ASSEMBLYAI_POLL_INTERVAL = 3   # секунд между опросами статуса
ASSEMBLYAI_TIMEOUT = 900       # максимум ожидания результата, сек

# Запись звонка учитываем только для состоявшихся разговоров
# длиннее MIN_RECORDING_DURATION_SEC — остальные игнорируются
# (не показываем, не скачиваем, не транскрибируем).
MIN_RECORDING_DURATION_SEC = 15

# Скачивать записи состоявшихся звонков в audio/
DOWNLOAD_RECORDINGS = True

WHISPER_MODEL = "small"    # tiny / base / small / medium / large-v3
WHISPER_DEVICE = "auto"    # auto / cpu / cuda
WHISPER_LANGUAGE = "ru"

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
