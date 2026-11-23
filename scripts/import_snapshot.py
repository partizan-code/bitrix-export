"""Импорт готового ZIP-снимка в SQLite без API, config.py и чтения .env."""

import argparse
import sqlite3
import sys
from pathlib import Path
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from import_snapshot import import_snapshot


def main(argv: list[str] | None = None) -> int:
    """Принять явные пути к скачанному ZIP и отдельной SQLite-базе."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Путь к готовому ZIP-снимку")
    parser.add_argument("--db", type=Path, required=True, help="Путь к SQLite-базе")
    args = parser.parse_args(argv)
    try:
        result = import_snapshot(args.archive, args.db)
    except (OSError, ValueError, BadZipFile, sqlite3.Error, RuntimeError) as error:
        parser.exit(1, f"Импорт не выполнен: {error}\n")
    action = "Снимок добавлен" if result["added"] else "Снимок уже есть, пропущен"
    print(f"{action}: {result['snapshot_id']}; файлов: {result['files']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
