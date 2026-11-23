"""Структурированные слепки из ZIP: версии карточек и событий без повторов текста."""

import argparse
import sqlite3
import sys
from pathlib import Path
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapshot_sqlite import import_snapshot


def main(argv: list[str] | None = None) -> int:
    """Обработать несколько архивов в общей базе, не загружая настройки бота."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path, help="ZIP schema-v2")
    parser.add_argument("--db", required=True, type=Path, help="База новой схемы")
    args = parser.parse_args(argv)
    try:
        for path in args.archives:
            result = import_snapshot(path, args.db)
            action = "Добавлен" if result["added"] else "Уже есть"
            print(f"{action}: {result['snapshot_id']}; карточек: {result['cards']}")
    except (OSError, ValueError, BadZipFile, sqlite3.Error, RuntimeError) as error:
        parser.exit(1, f"Импорт не выполнен: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
