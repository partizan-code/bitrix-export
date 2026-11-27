"""Согласованный ZIP SQLite, загрузка на Google Drive и очистка старых файлов."""

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import httplib2
from google.oauth2.service_account import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from logging_setup import log

SCOPES = ["https://www.googleapis.com/auth/drive"]


def make_database_zip(database_path: Path, archive_path: Path) -> Path:
    """Снять копию SQLite с WAL, сохранить ZIP и очистить каталог назначения."""
    database_path, archive_path = Path(database_path), Path(archive_path)
    if not database_path.is_file():
        raise FileNotFoundError("База SQLite для загрузки отсутствует")
    if database_path.resolve() == archive_path.resolve():
        raise ValueError("Пути базы и ZIP должны различаться")
    log.info(
        "SQLite: создаю согласованную копию базы ({:.2f} МиБ)",
        database_path.stat().st_size / 1024**2,
    )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".drive-", dir=archive_path.parent) as directory:
        snapshot = Path(directory) / archive_path.with_suffix(".db").name
        with (
            closing(
                sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            source.backup(target)
        log.info("SQLite: копия готова; сжимаю базу в ZIP")
        temporary_zip = Path(directory) / "database.zip"
        with ZipFile(temporary_zip, "w", compression=ZIP_DEFLATED) as archive:
            archive.write(snapshot, arcname=snapshot.name)
        os.replace(temporary_zip, archive_path)
    log.info("ZIP готов: {:.2f} МиБ", archive_path.stat().st_size / 1024**2)
    # Каталог предназначен только для экспортных копий, включая старые БД.
    for previous in archive_path.parent.iterdir():
        if previous != archive_path and (previous.is_file() or previous.is_symlink()):
            previous.unlink()
    log.info("Каталог ZIP базы очищен; оставлен только новый архив")
    return archive_path


def create_drive_service(credentials_path: Path, timeout: int = 60):
    """Создать клиент с ключом сервисного аккаунта и ограниченным HTTP-ожиданием."""
    log.info("Google Drive: читаю ключ сервисного аккаунта и создаю клиент")
    credentials = Credentials.from_service_account_file(
        str(credentials_path), scopes=SCOPES
    )
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout))
    try:
        service = build("drive", "v3", http=http, cache_discovery=False)
    except Exception:
        http.close()
        raise
    log.info("Google Drive: клиент готов")
    return service


def _query_literal(value: str) -> str:
    """Экранировать литерал запроса Drive, включая апострофы и обратные слеши."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def replace_archive(
    service, archive_path: Path, folder_id: str, retries: int = 3
) -> str:
    """Обновить и переименовать ZIP с сохранением ID, затем удалить старые дубли."""
    archive_path = Path(archive_path)
    files = service.files()
    query = (
        f"'{_query_literal(folder_id)}' in parents and trashed = false "
        f"and (name = '{_query_literal(archive_path.name)}' "
        "or name = 'sqlite3_snapshots.zip' or name contains 'snapshots_') "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )
    matches, tokens, token = [], set(), None
    log.info("Google Drive: ищу старые архивы в целевой папке")
    while True:
        page = files.list(
            q=query,
            spaces="drive",
            fields="nextPageToken,incompleteSearch,files(id,name)",
            pageSize=100,
            pageToken=token,
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=retries)
        matches.extend(
            item
            for item in page.get("files", [])
            if item.get("name") in {archive_path.name, "sqlite3_snapshots.zip"}
            or re.fullmatch(
                r"snapshots_\d{4}_\d{2}_\d{2} \d{2}[:-]\d{2}\.zip", item.get("name", "")
            )
        )
        if page.get("incompleteSearch"):
            raise ValueError("Drive вернул неполный список архивов")
        token = page.get("nextPageToken")
        if not token:
            break
        if token in tokens:
            raise ValueError("Drive вернул повторяющуюся страницу файлов")
        tokens.add(token)
    if not matches:
        raise FileNotFoundError(
            "Архив базы не найден на Drive: загрузите первый ZIP от имени "
            "пользователя и предоставьте сервисному аккаунту доступ"
        )
    current = next(
        (item for item in matches if item["name"] == archive_path.name), matches[0]
    )
    with archive_path.open("rb") as stream:
        media = MediaIoBaseUpload(
            stream,
            mimetype="application/zip",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        request = files.update(
            fileId=current["id"],
            body={"name": archive_path.name},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        log.info(
            "Google Drive: архив найден; загрузка 0% ({:.2f} МиБ)",
            media.size() / 1024**2,
        )
        result = None
        while result is None:
            status, result = request.next_chunk(num_retries=retries)
            if status is not None and result is None:
                percent = min(int(status.progress() * 100), 99)
                log.info("Google Drive: загрузка {}%", percent)
    file_id = result.get("id")
    if file_id != current["id"]:
        raise ValueError("Drive не подтвердил ID обновлённого архива")
    log.info("Google Drive: загрузка 100%; существующий архив обновлён")
    old_ids = {item["id"] for item in matches} - {file_id}
    for old_id in sorted(old_ids):
        files.delete(fileId=old_id, supportsAllDrives=True).execute(num_retries=retries)
    log.info("Google Drive: старые архивы базы удалены ({})", len(old_ids))
    return file_id


def upload_database(
    database_path: Path,
    archive_path: Path,
    *,
    credentials_path: Path,
    folder_id: str,
    timeout: int = 60,
    retries: int = 3,
) -> str:
    """Упаковать БД и заменить архив на Drive; при ошибке оставить локальный ZIP."""
    log.info("Начинаю архивацию SQLite и загрузку на Google Drive")
    try:
        archive_path = make_database_zip(database_path, archive_path)
        with closing(create_drive_service(credentials_path, timeout)) as service:
            return replace_archive(service, archive_path, folder_id, retries)
    except Exception as error:
        # Не выводить ответ Google, содержимое ключа и авторизованные URL.
        status = getattr(getattr(error, "resp", None), "status", None)
        log.error(
            "Архивация/загрузка прервана: {} (HTTP {})",
            type(error).__name__,
            status if isinstance(status, int) else "—",
        )
        raise
