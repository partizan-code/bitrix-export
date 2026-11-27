"""Согласованный ZIP SQLite и замена существующего архива на Google Drive."""

import os
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
    """Снять копию SQLite вместе с WAL и атомарно заменить локальный ZIP."""
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
        snapshot = Path(directory) / database_path.name
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
            archive.write(snapshot, arcname=database_path.name)
        os.replace(temporary_zip, archive_path)
    log.info("ZIP готов: {:.2f} МиБ", archive_path.stat().st_size / 1024**2)
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
    """Обновить ровно один одноимённый файл в папке, сохранив его ID и права."""
    archive_path = Path(archive_path)
    files = service.files()
    query = (
        f"'{_query_literal(folder_id)}' in parents and trashed = false "
        f"and name = '{_query_literal(archive_path.name)}' "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )
    matches, tokens, token = [], set(), None
    log.info("Google Drive: ищу существующий архив в целевой папке")
    while True:
        page = files.list(
            q=query,
            spaces="drive",
            fields="nextPageToken,incompleteSearch,files(id)",
            pageSize=100,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=retries)
        matches.extend(page.get("files", []))
        if page.get("incompleteSearch") or len(matches) > 1:
            raise ValueError("Не удалось однозначно определить архив в папке Drive")
        token = page.get("nextPageToken")
        if not token:
            break
        if token in tokens:
            raise ValueError("Drive вернул повторяющуюся страницу файлов")
        tokens.add(token)
    if not matches:
        raise FileNotFoundError(
            "Архив не найден на Drive: проверьте имя и доступ сервисного аккаунта"
        )

    with archive_path.open("rb") as stream:
        media = MediaIoBaseUpload(
            stream,
            mimetype="application/zip",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        request = files.update(
            fileId=matches[0]["id"],
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
    log.info("Google Drive: загрузка 100%; существующий архив обновлён")
    return result["id"]


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
