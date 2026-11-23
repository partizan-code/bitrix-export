"""Структурированные слепки из ZIP: версии карточек и событий без повторов текста."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

from import_snapshot import file_identity, read_snapshot
from parse_snapshot import parse_card, render_event, render_layout

APPLICATION_ID = 0x42585333
DB_VERSION = 2
CARD_COLUMNS = (
    "title",
    "crm_url",
    "responsible_name",
    "client_name",
    "stage_name",
    "source_name",
    "country",
    "created_at",
    "amount",
)
EVENT_COLUMNS = (
    "occurred_at",
    "event_type",
    "channel",
    "actor_name",
    "stage_name",
    "direction",
    "duration_seconds",
    "recording_file_ids",
    "audio_filename",
    "failure_code",
    "transcript_status",
    "task_title",
    "task_status",
    "deadline",
    "closed_at",
    "created_by",
    "stage_from",
    "stage_to",
    "headline",
    "line_ending",
    "text",
)


def encoded(value: object) -> str:
    """Детерминированный JSON без экранирования русского текста."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    """Отпечаток содержимого версии, а не предположение об ID события CRM."""
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def quote(name: str) -> str:
    """Экранировать имена SQL-колонок, включая пользовательские подписи."""
    if "\x00" in name:
        raise ValueError("В подписи поля обнаружен NUL")
    return '"' + name.replace('"', '""') + '"'


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Создать только собственную схему в пустой базе, без миграций прототипов."""
    app_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    objects = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if app_id == APPLICATION_ID and version == DB_VERSION:
        return
    if app_id or version or objects:
        raise ValueError("БД имеет другую схему или версию; укажите новый файл")
    statements = [
        """CREATE TABLE snapshots (
            snapshot_id TEXT PRIMARY KEY NOT NULL,
            snapshot_at TEXT NOT NULL,
            snapshot_timezone TEXT,
            status TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
            is_complete INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
            archive_name TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            manifest_text TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )""",
        """CREATE TABLE field_columns (
            section TEXT NOT NULL CHECK (section IN ('header', 'field')),
            label TEXT NOT NULL,
            occurrence INTEGER NOT NULL,
            column_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            PRIMARY KEY (section, label, occurrence)
        )""",
        "CREATE TABLE card_versions ("
        "card_version_id INTEGER PRIMARY KEY, entity TEXT NOT NULL "
        "CHECK (entity IN ('deal', 'lead')), entity_id INTEGER NOT NULL, "
        "content_sha256 TEXT NOT NULL UNIQUE, layout_json TEXT NOT NULL, "
        + ", ".join(f"{quote(name)} TEXT" for name in CARD_COLUMNS)
        + ", UNIQUE (card_version_id, entity, entity_id))",
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, "
        "entity TEXT NOT NULL CHECK (entity IN ('deal', 'lead')), "
        "entity_id INTEGER NOT NULL, content_sha256 TEXT NOT NULL UNIQUE, "
        + ", ".join(
            f"{quote(name)} {'INTEGER' if name == 'duration_seconds' else 'TEXT'}"
            for name in EVENT_COLUMNS
        )
        + ", UNIQUE (event_id, entity, entity_id))",
        """CREATE TABLE timelines (
            timeline_id INTEGER PRIMARY KEY,
            entity TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL UNIQUE,
            UNIQUE (timeline_id, entity, entity_id)
        )""",
        """CREATE TABLE snapshot_cards (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
            entity TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            entity_group TEXT NOT NULL CHECK (entity_group IN ('active', 'completed', 'technical')),
            path TEXT NOT NULL,
            card_sha256 TEXT NOT NULL,
            card_version_id INTEGER NOT NULL,
            timeline_id INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, entity, entity_id),
            UNIQUE (snapshot_id, path),
            FOREIGN KEY (card_version_id, entity, entity_id)
                REFERENCES card_versions(card_version_id, entity, entity_id),
            FOREIGN KEY (timeline_id, entity, entity_id)
                REFERENCES timelines(timeline_id, entity, entity_id)
        )""",
        """CREATE TABLE timeline_events (
            timeline_id INTEGER NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            suffix TEXT NOT NULL,
            PRIMARY KEY (timeline_id, position),
            FOREIGN KEY (timeline_id, entity, entity_id)
                REFERENCES timelines(timeline_id, entity, entity_id),
            FOREIGN KEY (event_id, entity, entity_id)
                REFERENCES events(event_id, entity, entity_id)
        ) WITHOUT ROWID""",
        "CREATE INDEX snapshot_cards_history ON snapshot_cards(entity, entity_id, card_sha256)",
        "CREATE INDEX timeline_events_event ON timeline_events(event_id, timeline_id)",
        "CREATE INDEX events_actor_time ON events(actor_name, occurred_at)",
        "CREATE INDEX events_type_time ON events(event_type, occurred_at)",
        """CREATE VIEW snapshot_events AS
            SELECT sc.snapshot_id, sc.entity, sc.entity_id, te.position, te.event_id, te.suffix
            FROM snapshot_cards sc JOIN timeline_events te
            USING (timeline_id, entity, entity_id)""",
        """CREATE VIEW cards AS
            SELECT sc.snapshot_id, s.snapshot_at, sc.entity_group, sc.path,
                   sc.card_sha256, cv.*
            FROM snapshot_cards sc JOIN snapshots s USING (snapshot_id)
            JOIN card_versions cv USING (card_version_id, entity, entity_id)""",
        """CREATE VIEW event_history AS
            SELECT se.snapshot_id, s.snapshot_at, s.snapshot_timezone,
                   se.position, cv.responsible_name, e.*
            FROM snapshot_events se JOIN snapshots s USING (snapshot_id)
            JOIN events e USING (event_id, entity, entity_id)
            JOIN snapshot_cards sc USING (snapshot_id, entity, entity_id)
            JOIN card_versions cv USING (card_version_id, entity, entity_id)""",
    ]
    for statement in statements:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {DB_VERSION}")


def field_column(connection: sqlite3.Connection, key: tuple, columns: dict) -> str:
    """Сопоставить подпись карточки отдельной колонке; сохранить повторы подписей."""
    if key in columns:
        return columns[key]
    section, label, occurrence = key
    name = f"{section}_{label}" + (f"__{occurrence}" if occurrence > 1 else "")
    if name.casefold() in {value.casefold() for value in columns.values()}:
        name += "__" + digest(key)[:16]
    connection.execute(f"ALTER TABLE card_versions ADD COLUMN {quote(name)} TEXT")
    connection.execute("INSERT INTO field_columns VALUES (?, ?, ?, ?)", (*key, name))
    columns[key] = name
    return name


def restore_card(
    connection: sqlite3.Connection, snapshot_id: str, entity: str, entity_id: int
) -> str:
    """Восстановить точный Markdown выбранного слепка только из базы."""
    cursor = connection.execute(
        "SELECT cv.* FROM snapshot_cards sc JOIN card_versions cv "
        "USING (card_version_id, entity, entity_id) "
        "WHERE sc.snapshot_id=? AND sc.entity=? AND sc.entity_id=?",
        (snapshot_id, entity, entity_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Карточка отсутствует в выбранном слепке")
    values = dict(zip((item[0] for item in cursor.description), row, strict=True))
    cursor = connection.execute(
        "SELECT e.occurred_at, e.headline, e.line_ending, e.text, se.suffix "
        "FROM snapshot_events se JOIN events e USING (event_id, entity, entity_id) "
        "WHERE se.snapshot_id=? AND se.entity=? AND se.entity_id=? ORDER BY se.position",
        (snapshot_id, entity, entity_id),
    )
    events = "".join(
        render_event(
            dict(
                zip(
                    ("occurred_at", "headline", "line_ending", "text"),
                    row[:4],
                    strict=True,
                )
            )
        )
        + row[4]
        for row in cursor
    )
    return render_layout(json.loads(values["layout_json"]), values, events)


def validate_manifest(manifest: dict, identities: list[tuple]) -> None:
    """Проверить состав карточек по каждому типу и группе, включая частичные ZIP."""
    groups = defaultdict(set)
    for entity, entity_id, group in identities:
        if entity_id in groups[entity, "all"]:
            raise ValueError("Одна карточка присутствует в ZIP несколько раз")
        groups[entity, "all"].add(entity_id)
        groups[entity, group].add(entity_id)
    for entity, block_name in (("deal", "deals"), ("lead", "leads")):
        block = manifest.get(block_name)
        if not isinstance(block, dict) or block.get("total") != len(
            groups[entity, "all"]
        ):
            raise ValueError("Количество карточек не совпадает с manifest")
        id_key = f"{entity}_ids"
        ids = block.get(id_key, [] if not groups[entity, "all"] else None)
        if (
            not isinstance(ids, list)
            or len(ids) != len(set(ids))
            or set(ids) != groups[entity, "all"]
        ):
            raise ValueError("ID карточек не совпадают с manifest")
        for group in ("active", "completed", "technical"):
            data = block.get("groups", {}).get(group, {})
            ids = data.get("ids")
            if (
                not isinstance(ids, list)
                or len(ids) != len(set(ids))
                or set(ids) != groups[entity, group]
                or data.get("count") != len(ids)
            ):
                raise ValueError("Группы карточек не совпадают с manifest")


def insert_version(connection: sqlite3.Connection, table: str, values: dict) -> int:
    """Переиспользовать точное содержимое; изменения хранить новой версией."""
    id_column = {
        "events": "event_id",
        "card_versions": "card_version_id",
        "timelines": "timeline_id",
    }[table]
    existing = connection.execute(
        f"SELECT {id_column} FROM {table} WHERE content_sha256=?",
        (values["content_sha256"],),
    ).fetchone()
    if existing:
        return existing[0]
    names = list(values)
    cursor = connection.execute(
        f"INSERT INTO {table} ({', '.join(quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        list(values.values()),
    )
    return cursor.lastrowid


def save_timeline(
    connection: sqlite3.Connection, entity: str, entity_id: int, events: list
) -> int:
    """Переиспользовать всю неизменную ленту, включая порядок и повторы событий."""
    records = []
    for event, suffix in events:
        record = {"entity": entity, "entity_id": entity_id, **event}
        record["content_sha256"] = digest(record)
        records.append((record, suffix))
    fingerprint = digest(
        [entity, entity_id, [(r["content_sha256"], s) for r, s in records]]
    )
    existing = connection.execute(
        "SELECT timeline_id FROM timelines WHERE content_sha256=?", (fingerprint,)
    ).fetchone()
    if existing:
        return existing[0]
    timeline_id = insert_version(
        connection,
        "timelines",
        {
            "entity": entity,
            "entity_id": entity_id,
            "content_sha256": fingerprint,
        },
    )
    for position, (record, suffix) in enumerate(records):
        event_id = insert_version(connection, "events", record)
        connection.execute(
            "INSERT INTO timeline_events VALUES (?, ?, ?, ?, ?, ?)",
            (timeline_id, entity, entity_id, position, event_id, suffix),
        )
    return timeline_id


def save_card_versions(
    connection: sqlite3.Connection,
    original: bytes,
    entity: str,
    entity_id: int,
    columns: dict,
) -> tuple[int, int, str]:
    """Сохранить версии полей и ленты; целиком одинаковую карточку разобрать один раз."""
    card_hash = hashlib.sha256(original).hexdigest()
    existing = connection.execute(
        "SELECT card_version_id, timeline_id FROM snapshot_cards "
        "WHERE entity=? AND entity_id=? AND card_sha256=? LIMIT 1",
        (entity, entity_id, card_hash),
    ).fetchone()
    if existing:
        return existing[0], existing[1], card_hash
    try:
        parsed = parse_card(original.decode("utf-8"), entity, entity_id)
    except ValueError as error:
        raise ValueError(f"Карточка {entity} #{entity_id}: {error}") from None
    mapping = {
        key: key if isinstance(key, str) else field_column(connection, key, columns)
        for key in parsed["values"]
    }
    values = {mapping[key]: value for key, value in parsed["values"].items()}
    parts = [
        [kind, mapping[value] if kind == "column" else value]
        for kind, value in parsed["layout"]
    ]
    version = {
        "entity": entity,
        "entity_id": entity_id,
        **values,
        "layout_json": encoded(parts),
    }
    version["content_sha256"] = digest(version)
    version_id = insert_version(connection, "card_versions", version)
    timeline_id = save_timeline(connection, entity, entity_id, parsed["events"])
    return version_id, timeline_id, card_hash


def import_snapshot(archive_path: Path, database_path: Path) -> dict:
    """Импортировать один ZIP транзакционно, сверяя восстановленные карточки."""
    archive_path, database_path = (
        Path(archive_path).resolve(),
        Path(database_path).resolve(),
    )
    if archive_path == database_path:
        raise ValueError("Пути ZIP и БД должны различаться")
    with ZipFile(archive_path) as archive:
        manifest, files, fingerprint = read_snapshot(archive)
        if manifest["schema_version"] != 2:
            raise ValueError("Поддерживаются только конечные карточки schema-v2")
        cards = []
        for info in files:
            if info.filename == "manifest.json":
                continue
            identity = file_identity(info.filename)
            if identity[0] is None:
                raise ValueError("В ZIP есть файл вне структуры конечных карточек")
            cards.append((info, identity))
        validate_manifest(manifest, [identity for _, identity in cards])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path, timeout=10)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                ensure_schema(connection)
                snapshot_id = manifest["export_id"]
                existing = connection.execute(
                    "SELECT content_sha256 FROM snapshots WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise ValueError(
                            "Снимок с таким export_id уже есть, но содержимое отличается"
                        )
                    return {
                        "snapshot_id": snapshot_id,
                        "cards": len(cards),
                        "added": False,
                    }
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        manifest["snapshot_at"],
                        manifest.get("snapshot_timezone"),
                        manifest["status"],
                        int(manifest["is_complete"]),
                        archive_path.name,
                        fingerprint,
                        archive.read("manifest.json").decode("utf-8"),
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
                columns = {
                    tuple(row[:3]): row[3]
                    for row in connection.execute("SELECT * FROM field_columns")
                }
                for info, (entity, entity_id, group) in cards:
                    original = archive.read(info)
                    version_id, timeline_id, card_hash = save_card_versions(
                        connection, original, entity, entity_id, columns
                    )
                    connection.execute(
                        "INSERT INTO snapshot_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot_id,
                            entity,
                            entity_id,
                            group,
                            info.filename,
                            card_hash,
                            version_id,
                            timeline_id,
                        ),
                    )
                    restored = restore_card(connection, snapshot_id, entity, entity_id)
                    if restored.encode("utf-8") != original:
                        raise ValueError(
                            f"Карточка {entity} #{entity_id}: восстановленный текст отличается"
                        )
                if read_snapshot(archive)[2] != fingerprint:
                    raise ValueError("ZIP изменился во время импорта")
    return {"snapshot_id": snapshot_id, "cards": len(cards), "added": True}
