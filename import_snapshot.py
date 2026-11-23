"""Импорт готового ZIP-снимка в SQLite без API, config.py и чтения .env."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

APPLICATION_ID = 0x4258534E
DB_VERSION = 1
CARD_NAME = re.compile(r"_(DEAL|LEAD)_.+_(\d+)_SNAP_.+\.md$")
ENTITY_DIRS = {"Сделки": "deal", "Лиды": "lead"}
GROUP_DIRS = {
    "Активные": "active",
    "Завершенные": "completed",
    "Технические": "technical",
}


def read_snapshot(archive: ZipFile) -> tuple[dict, list[ZipInfo], str]:
    """Проверить UTF-8 файлы, manifest и отпечаток содержимого без распаковки."""
    files = sorted(
        (info for info in archive.infolist() if not info.is_dir()),
        key=lambda info: info.filename,
    )
    names = [info.filename for info in files]
    if len(set(names)) != len(names):
        raise ValueError("В ZIP есть повторяющиеся пути файлов")
    if "manifest.json" not in names:
        raise ValueError("В корне ZIP отсутствует manifest.json")

    digest = hashlib.sha256()
    manifest = None
    for info in files:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise ValueError("В ZIP обнаружен некорректный путь")
        data = archive.read(info)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("В ZIP обнаружен файл не в UTF-8") from None
        name = info.filename.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if info.filename == "manifest.json":
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError:
                raise ValueError("manifest.json содержит некорректный JSON") from None

    if not isinstance(manifest, dict):
        # Проверяем содержимое JSON-файла, а не тип аргумента Python.
        raise ValueError("manifest.json должен содержать объект")  # noqa: TRY004
    if not isinstance(manifest.get("export_id"), str) or not manifest["export_id"]:
        raise ValueError("В manifest отсутствует непустой export_id")
    try:
        stamp = datetime.fromisoformat(manifest["snapshot_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("В manifest некорректный snapshot_at") from None
    if stamp.tzinfo is None:
        raise ValueError("snapshot_at должен содержать часовой пояс")
    version = manifest.get("schema_version")
    if type(version) is not int or version < 1:
        raise ValueError("В manifest некорректный schema_version")
    complete = manifest.get("is_complete")
    if type(complete) is not bool or manifest.get("status") != (
        "complete" if complete else "partial"
    ):
        raise ValueError("В manifest несогласованные status и is_complete")
    return manifest, files, digest.hexdigest()


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Создать схему в пустой БД; чужие и неизвестные версии не изменять."""
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    objects = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if application_id == APPLICATION_ID and version == DB_VERSION:
        return
    if application_id or version or objects:
        raise ValueError("БД имеет другую схему или версию; укажите новый файл")
    connection.execute(
        """CREATE TABLE snapshots (
            snapshot_id TEXT PRIMARY KEY NOT NULL,
            snapshot_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            archive_name TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
            is_complete INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
            file_count INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE snapshot_files (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
            path TEXT NOT NULL,
            content TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            entity TEXT,
            entity_id INTEGER,
            entity_group TEXT,
            PRIMARY KEY (snapshot_id, path)
        )"""
    )
    connection.execute(
        "CREATE INDEX snapshot_files_entity "
        "ON snapshot_files(entity, entity_id, snapshot_id)"
    )
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {DB_VERSION}")


def file_identity(path: str) -> tuple[str | None, int | None, str | None]:
    """Извлечь тип/ID карточки из имени, группу — из каталога."""
    parts = PurePosixPath(path).parts
    match = CARD_NAME.search(parts[-1]) if parts else None
    if len(parts) != 3 or not match:
        return None, None, None
    entity = match[1].lower()
    group = GROUP_DIRS.get(parts[1])
    if ENTITY_DIRS.get(parts[0]) != entity or group is None:
        return None, None, None
    return entity, int(match[2]), group


def import_snapshot(archive_path: Path, database_path: Path) -> dict:
    """Добавить снимок одной транзакцией; повтор пропустить, конфликт отклонить."""
    archive_path = Path(archive_path).resolve()
    database_path = Path(database_path).resolve()
    if archive_path == database_path:
        raise ValueError("Пути ZIP и БД должны различаться")
    with ZipFile(archive_path) as archive:
        manifest, files, fingerprint = read_snapshot(archive)
        snapshot_id = manifest["export_id"]
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                ensure_schema(connection)
                existing = connection.execute(
                    "SELECT content_sha256 FROM snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise ValueError(
                            "Снимок с таким export_id уже есть, но содержимое отличается"
                        )
                    return {
                        "snapshot_id": snapshot_id,
                        "files": len(files),
                        "added": False,
                    }
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        manifest["snapshot_at"],
                        datetime.now(UTC).isoformat(timespec="seconds"),
                        archive_path.name,
                        manifest["schema_version"],
                        manifest["status"],
                        int(manifest["is_complete"]),
                        len(files),
                        fingerprint,
                    ),
                )
                for info in files:
                    data = archive.read(info)
                    identity = file_identity(info.filename)
                    connection.execute(
                        "INSERT INTO snapshot_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot_id,
                            info.filename,
                            data.decode("utf-8"),
                            len(data),
                            hashlib.sha256(data).hexdigest(),
                            *identity,
                        ),
                    )
    return {"snapshot_id": snapshot_id, "files": len(files), "added": True}
