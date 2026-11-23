"""Структурированный снимок SQLite из согласованных JSON/JSONL одного прогона."""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_sqlite import SourceError, read_json, store_records, write_snapshot


def main(argv: list[str] | None = None) -> int:
    """Экспортировать согласованный комплект одного серверного запуска без API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = read_json(args.manifest)
        metadata = {
            entity: read_json(args.data_dir / f"{entity}_fields_meta.json")
            for entity in ("deal", "lead")
            if manifest.get(entity + "s", {}).get("total", 0)
        }
        result = write_snapshot(
            args.db,
            manifest,
            lambda: store_records(args.data_dir, args.export_dir, manifest),
            metadata,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        sqlite3.Error,
    ) as error:
        # Не выводим значения исходных полей и содержимое переписок при ошибке JSON.
        reason = (
            error
            if isinstance(error, (SourceError, FileNotFoundError, sqlite3.Error))
            else type(error).__name__
        )
        parser.exit(1, f"Структурированный экспорт не выполнен: {reason}\n")
    print(
        "Снимок добавлен" if result["added"] else "Снимок уже есть",
        result["snapshot_id"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
