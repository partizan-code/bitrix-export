"""Структурированный снимок SQLite из согласованных JSON/JSONL одного прогона."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

APPLICATION_ID = 0x42585332
DB_VERSION = 1
GROUPS = ("active", "completed", "technical")
EVENT_FIELDS = (
    "touchpoint_id",
    "timestamp",
    "channel",
    "event_type",
    "direction",
    "responsible_user_id",
    "actor__type",
    "actor__user_id",
    "actor__name",
    "client__contact_id",
    "client__name",
    "deal_stage_at_time__stage_id",
    "deal_stage_at_time__name",
    "content__type",
    "content__text",
    "content__phone",
    "content__duration_sec",
    "content__failed_code",
    "content__transcript_status",
    "content__recording_url",
    "content__audio_path",
    "content__comment",
    "content__description",
    "content__status",
    "content__deadline",
    "content__closed_date",
    "content__created_by",
    "content__stage_from__stage_id",
    "content__stage_from__name",
    "content__stage_to__stage_id",
    "content__stage_to__name",
    "source__activity_id",
    "source__stat_id",
    "source__chat_id",
    "source__message_id",
    "source__comment_id",
    "source__task_id",
    "source__file_ids",
)


class SourceError(ValueError):
    """Ошибка согласованности источников без вывода их содержимого."""


def dumps(value: object) -> str:
    """Стабильное представление JSON для отпечатка и сложных значений поля."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def quote(name: str) -> str:
    """Безопасно заключить произвольное имя колонки в кавычки SQLite."""
    if not name or "\x00" in name:
        raise SourceError("Некорректное имя поля источника")
    return '"' + name.replace('"', '""') + '"'


def scalar(value: object, name: str = "") -> object:
    """Сохранить значение поля; числовые ID приводить к int, суммы не округлять."""
    if isinstance(value, (dict, list)):
        return dumps(value)
    if (
        isinstance(value, str)
        and (name.rsplit("__", 1)[-1].lower() == "id" or name.lower().endswith("_id"))
        and re.fullmatch(r"-?\d+", value)
    ):
        return int(value)
    return value


def flatten(value: dict, prefix: str = "") -> dict:
    """Вложенные объекты развернуть в колонки через __, массивы оставить полями."""
    result = {}
    for key, item in value.items():
        name = f"{prefix}__{key}" if prefix else key
        if isinstance(item, dict) and item:
            nested = flatten(item, name)
            if result.keys() & nested.keys():
                raise SourceError("Неоднозначные пути вложенных полей")
            result.update(nested)
        else:
            if name in result:
                raise SourceError("Неоднозначные пути вложенных полей")
            result[name] = item
    return result


def components(value: object, path: str = "") -> Iterable[tuple[str, str, object]]:
    """Разложить массивы/объекты на адресуемые значения для SQL-выборок."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from components(item, path + "/" + escaped)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from components(item, path + "/" + str(index))
    else:
        kind = (
            "null"
            if value is None
            else "boolean"
            if isinstance(value, bool)
            else "integer"
            if isinstance(value, int)
            else "number"
            if isinstance(value, float)
            else "object"
            if isinstance(value, dict)
            else "array"
            if isinstance(value, list)
            else "string"
        )
        yield path, kind, scalar(value)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Создать отдельную схему; существующие БД другого назначения не менять."""
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (application_id, version) == (APPLICATION_ID, DB_VERSION):
        return
    objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
    if application_id or version or objects:
        raise SourceError(
            "БД имеет другую схему; укажите новый файл для первоисточников"
        )
    statements = [
        """CREATE TABLE snapshots (
            snapshot_id TEXT PRIMARY KEY NOT NULL, snapshot_at TEXT NOT NULL,
            status TEXT NOT NULL, is_complete INTEGER NOT NULL,
            schema_version INTEGER NOT NULL, imported_at TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL
        )""",
        """CREATE TABLE entities (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
            entity TEXT NOT NULL CHECK (entity IN ('deal', 'lead')),
            entity_id INTEGER NOT NULL, entity_group TEXT NOT NULL,
            title TEXT, responsible_user_id INTEGER, responsible_name TEXT,
            client_name TEXT, stage_name TEXT, country TEXT, source_name TEXT,
            PRIMARY KEY (snapshot_id, entity, entity_id)
        )""",
        """CREATE TABLE field_definitions (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
            entity TEXT NOT NULL, field_code TEXT NOT NULL, title TEXT,
            field_type TEXT, raw_column TEXT NOT NULL, display_column TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, entity, field_code)
        )""",
        """CREATE TABLE touchpoints (
            snapshot_id TEXT NOT NULL, entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, entity, entity_id, position),
            FOREIGN KEY (snapshot_id, entity, entity_id)
                REFERENCES entities(snapshot_id, entity, entity_id)
        )""",
        """CREATE TABLE field_items (
            snapshot_id TEXT NOT NULL, entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
            record_kind TEXT NOT NULL, position INTEGER NOT NULL, field_column TEXT NOT NULL,
            value_path TEXT NOT NULL, value_type TEXT NOT NULL, value,
            PRIMARY KEY (snapshot_id, entity, entity_id, record_kind, position, field_column, value_path),
            FOREIGN KEY (snapshot_id, entity, entity_id)
                REFERENCES entities(snapshot_id, entity, entity_id)
        )""",
        "CREATE INDEX entities_filter ON entities(snapshot_id, entity, entity_group, responsible_user_id)",
    ]
    for statement in statements:
        connection.execute(statement)
    for name in EVENT_FIELDS:
        connection.execute(f"ALTER TABLE touchpoints ADD COLUMN {quote(name)}")
    connection.execute(
        'CREATE INDEX touchpoints_filter ON touchpoints(snapshot_id, event_type, "timestamp")'
    )
    connection.execute(
        'CREATE INDEX touchpoints_actor ON touchpoints("actor__user_id", "timestamp")'
    )
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {DB_VERSION}")


def insert_row(
    connection: sqlite3.Connection, table: str, values: dict, known: dict
) -> None:
    """Добавлять новые поля источников колонками без изменения прошлых снимков."""
    for name in values:
        if name not in known[table]:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {quote(name)}")
            known[table].add(name)
    connection.execute(
        f"INSERT INTO {table} ("
        + ", ".join(quote(name) for name in values)
        + ") VALUES ("
        + ", ".join("?" for _ in values)
        + ")",
        tuple(scalar(value, name) for name, value in values.items()),
    )


def insert_items(
    connection: sqlite3.Connection, key: tuple, kind: str, position: int, fields: dict
) -> None:
    """Сохранить элементы сложных полей отдельно от основной строки сущности."""
    connection.executemany(
        "INSERT INTO field_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (*key, kind, position, field, path, value_type, value)
            for field, item in fields.items()
            if isinstance(item, (dict, list))
            for path, value_type, value in components(item)
        ),
    )


def validate_manifest(manifest: dict) -> None:
    """Проверить текущий контракт снимка, включая явную отметку полноты."""
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("export_id"), str)
        or not manifest["export_id"]
    ):
        raise SourceError("В manifest отсутствует export_id")
    stamp = datetime.fromisoformat(manifest["snapshot_at"])
    if stamp.tzinfo is None:
        raise SourceError("snapshot_at должен содержать часовой пояс")
    complete = manifest.get("is_complete")
    if type(complete) is not bool or manifest.get("status") != (
        "complete" if complete else "partial"
    ):
        raise SourceError("Несогласованные status и is_complete")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] < 1
    ):
        raise SourceError("В manifest отсутствует schema_version")


@contextmanager
def record_stream(factory):
    """Закрыть файловый генератор и при ранней ошибке записи в SQLite."""
    records = iter(factory())
    try:
        yield records
    finally:
        close = getattr(records, "close", None)
        if close is not None:
            close()


def write_snapshot(
    database: Path,
    manifest: dict,
    records_factory: Callable[[], Iterable[dict]],
    metadata: dict,
) -> dict:
    """Записать исходные сущности и касания одного снимка одной транзакцией.

    records_factory при каждом вызове возвращает новый итератор словарей
    {entity, group, deal, stats, touchpoints}. Не зависит от config или API.
    """
    validate_manifest(manifest)

    def fingerprint():
        digest = hashlib.sha256(dumps([manifest, metadata]).encode("utf-8"))
        with record_stream(records_factory) as records:
            for record in records:
                data = dumps(record).encode("utf-8")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
        return digest.hexdigest()

    expected = fingerprint()
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    snapshot_id = manifest["export_id"]
    counts = Counter()
    observed_ids = {}
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection, ExitStack() as streams:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            existing = connection.execute(
                "SELECT source_sha256 FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing:
                if existing[0] != expected:
                    raise SourceError(
                        "Конфликт export_id: первоисточники снимка изменились"
                    )
                return {"added": False, "snapshot_id": snapshot_id}
            connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    manifest["snapshot_at"],
                    manifest["status"],
                    int(manifest["is_complete"]),
                    manifest["schema_version"],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    expected,
                    dumps(manifest),
                ),
            )
            known = {
                table: {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for table in ("entities", "touchpoints")
            }
            definitions = set()
            written_digest = hashlib.sha256(dumps([manifest, metadata]).encode("utf-8"))
            for record in streams.enter_context(record_stream(records_factory)):
                data = dumps(record).encode("utf-8")
                written_digest.update(len(data).to_bytes(8, "big"))
                written_digest.update(data)
                entity, group = record["entity"], record["group"]
                deal, stats, touchpoints = (
                    record["deal"],
                    record["stats"],
                    record["touchpoints"],
                )
                if entity not in {"deal", "lead"} or group not in GROUPS:
                    raise SourceError("Неизвестный тип или группа сущности")
                entity_id = int(deal["ID"])
                id_key = "deal_id" if entity == "deal" else "lead_id"
                if (
                    int(stats[id_key]) != entity_id
                    or stats["touchpoints"] != len(touchpoints)
                    or stats.get("fields", {}) != deal.get("_fields", {})
                ):
                    raise SourceError(
                        "Не согласованы ID/число касаний сущности и индекса"
                    )
                key = (snapshot_id, entity, entity_id)
                fields = deal.get("_fields") or {}
                values = {
                    "snapshot_id": snapshot_id,
                    "entity": entity,
                    "entity_id": entity_id,
                    "entity_group": group,
                    "title": deal.get("TITLE"),
                    "responsible_user_id": scalar(
                        deal.get("ASSIGNED_BY_ID"), "ASSIGNED_BY_ID"
                    ),
                    "responsible_name": (fields.get("ASSIGNED_BY_ID") or {}).get(
                        "value"
                    ),
                    "client_name": deal.get("_contact_name"),
                    "stage_name": stats.get("stage"),
                    "country": stats.get("country", deal.get("_country")),
                    "source_name": deal.get("_source"),
                }
                values.update(
                    {
                        "raw_" + code: value
                        for code, value in deal.items()
                        if code != "_fields"
                    }
                )
                values.update(
                    {
                        "display_" + code: field.get("value")
                        for code, field in fields.items()
                    }
                )
                values.update(
                    {
                        "label_" + code: field.get("title")
                        for code, field in fields.items()
                    }
                )
                values.update(
                    {
                        "index_" + code: value
                        for code, value in stats.items()
                        if code != "fields"
                    }
                )
                insert_row(connection, "entities", values, known)
                insert_items(connection, key, "entity", 0, values)
                for code in sorted(
                    set(deal) | set(fields) | set(metadata.get(entity, {}))
                ):
                    if code.startswith("_") or (entity, code) in definitions:
                        continue
                    meta = metadata.get(entity, {}).get(code, {})
                    for prefix in ("raw_", "display_", "label_"):
                        name = prefix + code
                        if name not in known["entities"]:
                            connection.execute(
                                f"ALTER TABLE entities ADD COLUMN {quote(name)}"
                            )
                            known["entities"].add(name)
                    connection.execute(
                        "INSERT INTO field_definitions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot_id,
                            entity,
                            code,
                            (fields.get(code) or {}).get("title")
                            or meta.get("title")
                            or code,
                            meta.get("type"),
                            "raw_" + code,
                            "display_" + code,
                            dumps(meta),
                        ),
                    )
                    definitions.add((entity, code))
                ids = set()
                for position, tp in enumerate(touchpoints, 1):
                    if int(tp["deal_id"]) != entity_id or tp["touchpoint_id"] in ids:
                        raise SourceError(
                            "Касание принадлежит другой сущности или имеет повторный ID"
                        )
                    ids.add(tp["touchpoint_id"])
                    flattened = flatten(tp)
                    insert_row(
                        connection,
                        "touchpoints",
                        dict(
                            snapshot_id=snapshot_id,
                            entity=entity,
                            entity_id=entity_id,
                            position=position,
                            **flattened,
                        ),
                        known,
                    )
                    insert_items(connection, key, "touchpoint", position, flattened)
                counts[(entity, group)] += 1
                observed_ids.setdefault((entity, group), set()).add(entity_id)
            if written_digest.hexdigest() != expected:
                raise SourceError(
                    "Первоисточники изменились во время чтения; снимок отменён"
                )
            for entity, block_name in (("deal", "deals"), ("lead", "leads")):
                block = manifest.get(block_name, {})
                if sum(counts[(entity, group)] for group in GROUPS) != block.get(
                    "total", 0
                ):
                    raise SourceError("Число сущностей не совпало с manifest")
                for group, description in block.get("groups", {}).items():
                    if counts[(entity, group)] != description["count"]:
                        raise SourceError(
                            "Число сущностей группы не совпало с manifest"
                        )
                    if observed_ids.get((entity, group), set()) != {
                        int(value) for value in description["ids"]
                    }:
                        raise SourceError("ID сущностей группы не совпали с manifest")
    return {"added": True, "snapshot_id": snapshot_id, "entities": sum(counts.values())}


def read_json(path: Path) -> dict:
    """Прочитать JSON явно указанного набора, не загружая настройки приложения."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> Iterable[dict]:
    """Читать групповой JSONL потоком, без загрузки всей ленты в память."""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def store_records(data_dir: Path, export_dir: Path, manifest: dict) -> Iterable[dict]:
    """Состав/индекс/касания взять из текущего экспорта, исходные поля — из store."""
    with ExitStack() as streams:
        registry = read_json(data_dir / "registry.json")
        for entity, block_name in (("deal", "deals"), ("lead", "leads")):
            root = (
                data_dir / "store" if entity == "deal" else data_dir / "store" / "lead"
            )
            for group in GROUPS:
                block = manifest.get(block_name, {}).get("groups", {}).get(group, {})
                ids = block.get("ids", [])
                if len(ids) != block.get("count", 0) or len(set(ids)) != len(ids):
                    raise SourceError(
                        "В manifest не согласованы ID и число сущностей группы"
                    )
                if not ids:
                    continue
                id_key = "deal_id" if entity == "deal" else "lead_id"
                indexes = {}
                for stats in read_jsonl(export_dir / f"index_{entity}_{group}.jsonl"):
                    entity_id = int(stats[id_key])
                    if entity_id in indexes or stats.get("group") != group:
                        raise SourceError(
                            "Повтор ID или неверная группа в индексе экспорта"
                        )
                    indexes[entity_id] = stats
                if set(indexes) != {int(value) for value in ids}:
                    raise SourceError("Индекс экспорта и manifest содержат разные ID")
                grouped = iter(
                    groupby(
                        streams.enter_context(
                            closing(
                                read_jsonl(
                                    export_dir / f"touchpoints_{entity}_{group}.jsonl"
                                )
                            )
                        ),
                        key=lambda tp: int(tp["deal_id"]),
                    )
                )
                current = next(grouped, None)
                for entity_id in sorted(int(value) for value in ids):
                    key = str(entity_id) if entity == "deal" else f"lead:{entity_id}"
                    entry = registry.get(key, {})
                    if (
                        entry.get("exported_at") != manifest["export_id"]
                        or entry.get("group") != group
                    ):
                        raise SourceError(
                            "Registry и manifest относятся к разным снимкам"
                        )
                    deal = read_json(root / "deal" / f"{entity_id}.json")
                    stats = indexes[entity_id]
                    if int(deal["ID"]) != entity_id:
                        raise SourceError("ID файла store не соответствует содержимому")
                    if stats.get("fields", {}) != deal.get("_fields", {}):
                        raise SourceError(
                            "Поля сущности и индекса store не согласованы"
                        )
                    if current is not None and current[0] < entity_id:
                        raise SourceError(
                            "В ленте есть посторонние ID или нарушен порядок"
                        )
                    touchpoints = []
                    if current is not None and current[0] == entity_id:
                        touchpoints = list(current[1])
                        current = next(grouped, None)
                    yield {
                        "entity": entity,
                        "group": group,
                        "deal": deal,
                        "stats": stats,
                        "touchpoints": touchpoints,
                    }
                if current is not None:
                    raise SourceError(
                        "В ленте есть ID вне manifest или нарушен порядок"
                    )
