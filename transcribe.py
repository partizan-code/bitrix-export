"""
Записи звонков -> текст.

Транскрибация через AssemblyAI с диаризацией (разделение спикеров).
Ключ: ASSEMBLYAI_API_KEY из .env, загружаемый через config.py.

Скачивание записей с Диска Bitrix24 — функция download_call_recording,
работает независимо от транскрибации.
"""

import os
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

import config
from logging_setup import log
from performance import count, measure, timed

AA_BASE = "https://api.assemblyai.com"


# ================================================================== скачивание
def get_disk_file(client, file_id):
    """Информация о файле Диска (NAME, DOWNLOAD_URL, SIZE)."""
    return client.result("disk.file.get", {"id": int(file_id)}) or {}


def resolve_file_url(file_info):
    """
    Ссылка на скачивание файла Диска (запись разговора из FILES активности).

    DOWNLOAD_URL, который disk.file.get отдаёт вебхуку, уже содержит
    авторизацию в пути (/rest/<user>/<key>/download/?token=...) —
    скачиваем его как есть, ничего не добавляя.
    """
    return file_info.get("DOWNLOAD_URL") or None


@timed("audio.download")
def download_recording(url, dest_path):
    """Скачивание с потоковой записью. Повторно не качает."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        count("audio.download_cache_hit")
        return dest_path
    count("audio.download_cache_miss")
    tmp = dest_path + ".part"
    try:
        with requests.get(url, timeout=config.REQUEST_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                f.writelines(r.iter_content(chunk_size=1 << 16))
        os.replace(tmp, dest_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest_path


def probe_recording(
    url: str,
    *,
    byte_range: tuple[int, int] | None = None,
    close_connection: bool = False,
    byte_limit: int = 65536,
    read_timeout: float = 10,
    total_timeout: float = 30,
) -> dict:
    """Проверить передачу аудио в память; вернуть только безопасные метаданные.

    Время проверяется между чтениями; блокирующее чтение ограничено read_timeout.
    Ссылки, произвольные заголовки, тексты ошибок и байты аудио не возвращаются.
    """
    if not 1 <= byte_limit <= 65536 or read_timeout <= 0 or total_timeout <= 0:
        raise ValueError("Некорректный лимит диагностического чтения")
    if byte_range is not None and (byte_range[0] < 0 or byte_range[1] < byte_range[0]):
        raise ValueError("Некорректный диапазон байтов")
    started = time.monotonic()
    result = {"bytes_read": 0, "redirects": 0, "outcome": "error"}
    headers = {}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        byte_limit = min(byte_limit, byte_range[1] - byte_range[0] + 1)
    if close_connection:
        headers.update({"Connection": "close", "Accept-Encoding": "identity"})
    first = b""
    try:
        with requests.Session() as session:
            for hop in range(4):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                    result["outcome"] = "unsafe_url"
                    break
                if time.monotonic() - started >= total_timeout:
                    result["outcome"] = "time_budget"
                    break
                with session.get(
                    url,
                    headers=headers,
                    timeout=(8, read_timeout),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    result.update(
                        http_status=response.status_code,
                        headers_seconds=round(time.monotonic() - started, 3),
                    )
                    if response.is_redirect:
                        if hop == 3:
                            result["outcome"] = "redirect_limit"
                            break
                        url = urljoin(url, response.headers.get("Location", ""))
                        result["redirects"] += 1
                        continue
                    content_type = response.headers.get("Content-Type", "")
                    media = re.match(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", content_type)
                    length = response.headers.get("Content-Length", "")
                    expected = (
                        int(length) if re.fullmatch(r"[0-9]{1,18}", length) else None
                    )
                    range_match = re.fullmatch(
                        r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                        response.headers.get("Content-Range", ""),
                    )
                    received_range = (
                        tuple(map(int, range_match.groups())) if range_match else None
                    )
                    result.update(
                        content_type=media.group().lower() if media else "other",
                        content_length=expected,
                        content_range=received_range,
                        range_honored=(
                            response.status_code == 206
                            and received_range is not None
                            and received_range[:2] == byte_range
                        )
                        if byte_range is not None
                        else None,
                    )
                    if response.status_code not in (200, 206):
                        result["outcome"] = "http_error"
                        break
                    reader = getattr(response.raw, "read1", response.raw.read)
                    while result["bytes_read"] < byte_limit:
                        if expected is not None and result["bytes_read"] >= expected:
                            break
                        if time.monotonic() - started >= total_timeout:
                            result["outcome"] = "time_budget"
                            break
                        chunk = reader(min(4096, byte_limit - result["bytes_read"]))
                        if not chunk:
                            break
                        if result["bytes_read"] == 0:
                            result["first_data_seconds"] = round(
                                time.monotonic() - started, 3
                            )
                        first = (first + chunk)[:32]
                        result["bytes_read"] += len(chunk)
                    else:
                        result["outcome"] = "sample_complete"
                    if result["outcome"] == "error":
                        result["outcome"] = "eof"
                    if expected is not None:
                        if result["bytes_read"] == expected:
                            result["outcome"] = "file_complete"
                        elif result["outcome"] == "eof":
                            result["outcome"] = "incomplete"
                    break
    except Exception as error:  # noqa: BLE001 — продолжить остальные пробы
        result["error_type"] = type(error).__name__
        result["outcome"] = "error"
    result.update(
        elapsed_seconds=round(time.monotonic() - started, 3),
        looks_html=first.lstrip().lower().startswith((b"<!doctype html", b"<html")),
        audio_signature=(
            first[:3] == b"ID3"
            or first[:4] in (b"RIFF", b"OggS", b"fLaC")
            or (len(first) > 1 and first[0] == 255 and first[1] & 224 == 224)
        ),
    )
    if not result["bytes_read"] and result["outcome"] in {"file_complete", "eof"}:
        result["outcome"] = "empty"
    if result["looks_html"] and result["outcome"] in {
        "file_complete",
        "sample_complete",
        "eof",
    }:
        result["outcome"] = "unexpected_content"
    return result


def download_call_recording(client, deal_id, call):
    """
    Скачивает запись состоявшегося звонка в audio/.
    Возвращает путь к файлу или None.
    """
    file_ids = call.get("file_ids") or []
    if not file_ids:
        return None
    act_id = call.get("activity_id") or "x"

    try:
        info = get_disk_file(client, file_ids[0])
        url = resolve_file_url(info)
        if not url:
            log.error(
                "Сущность {} звонок {}: у файла {} нет DOWNLOAD_URL",
                deal_id,
                act_id,
                file_ids[0],
            )
            return None
        ext = os.path.splitext(info.get("NAME") or "")[1] or ".mp3"
        dest = os.path.join(config.AUDIO_DIR, f"deal{deal_id}_call{act_id}{ext}")
        return download_recording(url, dest)
    except Exception:  # noqa: BLE001 — ошибка записи не останавливает остальные звонки
        log.exception("Сущность {} звонок {}: запись не скачана", deal_id, act_id)
        return None


# ================================================================== AssemblyAI
def _aa_headers():
    key = config.ASSEMBLYAI_API_KEY
    if not key:
        raise RuntimeError(
            "Не задан ключ AssemblyAI. Укажите ASSEMBLYAI_API_KEY "
            "в файле .env рядом с config.py."
        )
    return {"authorization": key}


@timed("audio.upload")
def _aa_upload(path, headers):
    """Загрузка файла чанками (безопасно для больших записей)."""

    def _chunks(f, size=5 * 1024 * 1024):
        while True:
            chunk = f.read(size)
            if not chunk:
                break
            yield chunk

    with open(path, "rb") as f:
        r = requests.post(
            f"{AA_BASE}/v2/upload", headers=headers, data=_chunks(f), timeout=600
        )
    r.raise_for_status()
    return r.json()["upload_url"]


@timed("audio.recognition")
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
        "speaker_labels": True,  # диаризация
        "speakers_expected": config.ASSEMBLYAI_SPEAKERS,
        "punctuate": True,
    }
    r = requests.post(
        f"{AA_BASE}/v2/transcript", json=payload, headers=headers, timeout=60
    )
    r.raise_for_status()
    transcript_id = r.json()["id"]
    count("audio.jobs_submitted")
    log.debug("AssemblyAI: transcript_id={}, ожидание результата", transcript_id)

    poll_url = f"{AA_BASE}/v2/transcript/{transcript_id}"
    deadline = time.time() + config.ASSEMBLYAI_TIMEOUT
    with measure("audio.polling"):
        while True:
            count("audio.poll_requests")
            res = requests.get(poll_url, headers=headers, timeout=60).json()
            status = res.get("status")
            if status == "completed":
                count("audio.jobs_completed")
                log.debug(
                    "AssemblyAI: transcript_id={}, распознавание завершено",
                    transcript_id,
                )
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


# ================================================================== общий вход
def transcribe_file(path):
    """Транскрибация одного файла через AssemblyAI. Текст или None."""
    return _transcribe_assemblyai(path)


def transcript_path(deal_id, call):
    """Путь к файлу транскрипции звонка в кэше transcripts/."""
    act_id = call.get("activity_id") or "x"
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
    url = resolve_file_url(info)
    if not url:
        raise RuntimeError(f"у файла {call['file_ids'][0]} нет DOWNLOAD_URL")
    return _transcribe_assemblyai_url(url)


def use_assemblyai_direct():
    """Прямая транскрибация по ссылке включена?"""
    return bool(getattr(config, "ASSEMBLYAI_DIRECT_URL", False))


@timed("audio.transcript")
def transcribe_call(client, deal_id, call):
    """
    Полный цикл для одного звонка: взять запись -> транскрибировать ->
    сохранить текст в transcripts/. Возвращает путь к .txt или None.
    При ASSEMBLYAI_DIRECT_URL запись на диск вообще не скачивается —
    AssemblyAI получает прямую ссылку (fallback: скачивание + upload).
    """
    act_id = call.get("activity_id") or "x"
    txt_path = transcript_path(deal_id, call)
    if os.path.exists(txt_path):
        count("audio.transcript_cache_hit")
        return txt_path
    count("audio.transcript_cache_miss")

    if use_assemblyai_direct() and call.get("file_ids"):
        log.debug(
            "Сущность {} звонок {}: транскрибация по прямой ссылке", deal_id, act_id
        )
        try:
            text = _transcribe_assemblyai_direct(client, call)
        except Exception as e:  # noqa: BLE001 — резервная загрузка вместо прямой ссылки
            log.warning(
                "Сущность {} звонок {}: прямая ссылка не сработала ({}) "
                "— скачиваю и загружаю файлом",
                deal_id,
                act_id,
                e,
            )
        else:
            if not text:
                return None
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            log.debug("Сущность {} звонок {}: транскрипция сохранена", deal_id, act_id)
            return txt_path

    audio_path = call.get("local_audio")
    if not audio_path and call.get("file_ids"):
        audio_path = download_call_recording(client, deal_id, call)
    if not audio_path:
        return None

    try:
        log.debug(
            "Сущность {} звонок {}: транскрибация локального аудио", deal_id, act_id
        )
        text = transcribe_file(audio_path)
    except Exception:  # noqa: BLE001 — ошибка транскрибации не останавливает выгрузку
        log.exception(
            "Сущность {} звонок {}: транскрибация не удалась", deal_id, act_id
        )
        return None
    if not text:
        return None
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    log.debug("Сущность {} звонок {}: транскрипция сохранена", deal_id, act_id)
    return txt_path
