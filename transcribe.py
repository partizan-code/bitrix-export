"""
Записи звонков -> текст.

Провайдеры транскрибации (config.TRANSCRIPTION_PROVIDER):
  - "assemblyai" — AssemblyAI, с диаризацией (разделение спикеров).
                   Ключ: переменная окружения ASSEMBLYAI_API_KEY или config.
  - "whisper"    — локальный Faster Whisper (опциональная зависимость).

Скачивание записей с Диска Bitrix24 — функция download_call_recording,
работает независимо от провайдера транскрибации.
"""

import logging
import os
import time

import requests

import config

log = logging.getLogger("b24")

AA_BASE = "https://api.assemblyai.com"


# ================================================================== скачивание
def get_disk_file(client, file_id):
    """Информация о файле Диска (NAME, DOWNLOAD_URL, SIZE)."""
    return client.result("disk.file.get", {"id": int(file_id)}) or {}


def resolve_file_url(client, file_info):
    """
    Ссылка на скачивание файла Диска (запись разговора из FILES активности).

    DOWNLOAD_URL, который disk.file.get отдаёт вебхуку, уже содержит
    авторизацию в пути (/rest/<user>/<key>/download/?token=...) —
    скачиваем его как есть, ничего не добавляя.
    """
    return file_info.get("DOWNLOAD_URL") or None


def download_recording(url, dest_path):
    """Скачивание с потоковой записью. Повторно не качает."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    r = requests.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)
    r.raise_for_status()
    tmp = dest_path + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 16):
            f.write(chunk)
    os.replace(tmp, dest_path)
    return dest_path


def download_call_recording(client, deal_id, call):
    """
    Скачивает запись состоявшегося звонка в audio/.
    Возвращает путь к файлу или None.
    """
    file_ids = call.get("file_ids") or []
    if not file_ids:
        return None
    act_id = call.get("activity_id") or call.get("stat_id") or "x"

    try:
        info = get_disk_file(client, file_ids[0])
        url = resolve_file_url(client, info)
        if not url:
            log.warning("Сделка %s: у файла %s нет DOWNLOAD_URL", deal_id, file_ids[0])
            return None
        ext = os.path.splitext(info.get("NAME") or "")[1] or ".mp3"
        dest = os.path.join(config.AUDIO_DIR, f"deal{deal_id}_call{act_id}{ext}")
        return download_recording(url, dest)
    except Exception as e:
        log.warning("Сделка %s звонок %s: запись не скачана: %s", deal_id, act_id, e)
        return None


# ================================================================== AssemblyAI
def _aa_headers():
    key = config.ASSEMBLYAI_API_KEY
    if not key:
        raise RuntimeError(
            "Не задан ключ AssemblyAI: переменная окружения ASSEMBLYAI_API_KEY "
            "или config.ASSEMBLYAI_API_KEY"
        )
    return {"authorization": key}


def _aa_upload(path, headers):
    """Загрузка файла чанками (безопасно для больших записей)."""
    def _chunks(f, size=5 * 1024 * 1024):
        while True:
            chunk = f.read(size)
            if not chunk:
                break
            yield chunk

    with open(path, "rb") as f:
        r = requests.post(f"{AA_BASE}/v2/upload", headers=headers,
                          data=_chunks(f), timeout=600)
    r.raise_for_status()
    return r.json()["upload_url"]


def _transcribe_assemblyai_url(audio_url):
    """
    Транскрибация через AssemblyAI с диаризацией по готовой ссылке
    (upload_url после _aa_upload ИЛИ подписанный DOWNLOAD_URL Битрикса —
    AssemblyAI скачивает аудио сам, без локальной копии).
    Возвращает текст вида:
        Спикер A: Здравствуйте, ...
        Спикер B: Да, слушаю вас.
    """
    headers = _aa_headers()
    payload = {
        "audio_url": audio_url,
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "language_code": config.ASSEMBLYAI_LANGUAGE,
        "speaker_labels": True,                       # диаризация
        "speakers_expected": config.ASSEMBLYAI_SPEAKERS,
        "punctuate": True,
    }
    r = requests.post(f"{AA_BASE}/v2/transcript", json=payload,
                      headers=headers, timeout=60)
    r.raise_for_status()
    transcript_id = r.json()["id"]
    log.info("    AssemblyAI: transcript %s создан, жду результат...", transcript_id)

    poll_url = f"{AA_BASE}/v2/transcript/{transcript_id}"
    deadline = time.time() + config.ASSEMBLYAI_TIMEOUT
    while True:
        res = requests.get(poll_url, headers=headers, timeout=60).json()
        status = res.get("status")
        if status == "completed":
            break
        if status == "error":
            raise RuntimeError(f"AssemblyAI: {res.get('error')}")
        if time.time() > deadline:
            raise TimeoutError(
                f"AssemblyAI: транскрипт {transcript_id} не готов за "
                f"{config.ASSEMBLYAI_TIMEOUT}с — повторите позже"
            )
        time.sleep(config.ASSEMBLYAI_POLL_INTERVAL)

    utterances = res.get("utterances") or []
    if utterances:
        return "\n".join(
            f"Спикер {u.get('speaker', '?')}: {(u.get('text') or '').strip()}"
            for u in utterances
        )
    return (res.get("text") or "").strip()


def _transcribe_assemblyai(path):
    """Классический путь: загрузить локальный файл в AssemblyAI, транскрибировать."""
    return _transcribe_assemblyai_url(_aa_upload(path, _aa_headers()))


# ================================================================== Faster Whisper
_model = None
_whisper_available = None


def _get_whisper_model():
    global _model, _whisper_available
    if _whisper_available is False:
        return None
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper не установлен — провайдер whisper недоступен")
        _whisper_available = False
        return None
    device = config.WHISPER_DEVICE
    if device == "auto":
        try:
            import torch  # noqa
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    log.info("Загружаю Whisper '%s' (%s, %s)...", config.WHISPER_MODEL, device, compute)
    _model = WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute)
    _whisper_available = True
    return _model


def _transcribe_whisper(path):
    model = _get_whisper_model()
    if model is None:
        return None
    segments, _ = model.transcribe(
        path, language=config.WHISPER_LANGUAGE, vad_filter=True,
    )
    lines = []
    for seg in segments:
        m, s = divmod(int(seg.start), 60)
        lines.append(f"[{m:02d}:{s:02d}] {seg.text.strip()}")
    return "\n".join(lines)


# ================================================================== общий вход
def transcribe_file(path):
    """Транскрибация одного файла выбранным провайдером. Текст или None."""
    provider = (config.TRANSCRIPTION_PROVIDER or "").lower()
    if provider == "assemblyai":
        return _transcribe_assemblyai(path)
    if provider == "whisper":
        return _transcribe_whisper(path)
    raise ValueError(f"Неизвестный TRANSCRIPTION_PROVIDER: {provider!r}")


def transcript_path(deal_id, call):
    """Путь к файлу транскрипции звонка в кэше transcripts/."""
    act_id = call.get("activity_id") or call.get("stat_id") or "x"
    return os.path.join(config.TRANSCRIPTS_DIR, f"deal{deal_id}_call{act_id}.txt")


def has_transcript(deal_id, call):
    """Транскрипция звонка уже есть в кэше?"""
    return os.path.exists(transcript_path(deal_id, call))


def _transcribe_assemblyai_direct(client, call):
    """
    AssemblyAI забирает запись САМ по подписанной ссылке Диска Битрикса
    (disk.file.get -> DOWNLOAD_URL): ничего не скачиваем на локальный диск.
    """
    info = get_disk_file(client, call["file_ids"][0])
    url = resolve_file_url(client, info)
    if not url:
        raise RuntimeError(f"у файла {call['file_ids'][0]} нет DOWNLOAD_URL")
    return _transcribe_assemblyai_url(url)


def use_assemblyai_direct():
    """Прямая транскрибация по ссылке включена и применима?"""
    return ((config.TRANSCRIPTION_PROVIDER or "").lower() == "assemblyai"
            and bool(getattr(config, "ASSEMBLYAI_DIRECT_URL", False)))


def transcribe_call(client, deal_id, call):
    """
    Полный цикл для одного звонка: взять запись -> транскрибировать ->
    сохранить текст в transcripts/. Возвращает путь к .txt или None.
    При ASSEMBLYAI_DIRECT_URL запись на диск вообще не скачивается —
    AssemblyAI получает прямую ссылку (fallback: скачивание + upload).
    """
    act_id = call.get("activity_id") or call.get("stat_id") or "x"
    txt_path = transcript_path(deal_id, call)
    if os.path.exists(txt_path):
        return txt_path

    if use_assemblyai_direct() and call.get("file_ids"):
        try:
            text = _transcribe_assemblyai_direct(client, call)
        except Exception as e:
            log.warning("Сделка %s звонок %s: прямая ссылка не сработала (%s) "
                        "— скачиваю и загружаю файлом", deal_id, act_id, e)
        else:
            if not text:
                return None
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            return txt_path

    audio_path = call.get("local_audio")
    if not audio_path:
        if call.get("file_ids"):
            audio_path = download_call_recording(client, deal_id, call)
        elif call.get("recording_url"):
            audio_path = os.path.join(config.AUDIO_DIR, f"deal{deal_id}_call{act_id}.mp3")
            try:
                download_recording(call["recording_url"], audio_path)
            except Exception as e:
                log.warning("Сделка %s звонок %s: запись не скачана: %s", deal_id, act_id, e)
                return None
    if not audio_path:
        return None

    try:
        text = transcribe_file(audio_path)
    except Exception as e:
        log.warning("Сделка %s звонок %s: транскрибация не удалась: %s", deal_id, act_id, e)
        return None
    if not text:
        return None
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    return txt_path
