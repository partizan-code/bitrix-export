"""Проверки импорта ZIP в SQLite на искусственных архивах во временном каталоге."""

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import warnings
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from script_utils import load_script

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_snapshot

DEAL = "Сделки/Активные/LEXS_DEAL_RB_ON_123_SNAP_20261123T120000+0300_schema-v2.md"
LEAD = "Лиды/Технические/LEXS_LEAD_RF_123_SNAP_20261123T120000+0300_schema-v2.md"


class ImportSnapshotTests(unittest.TestCase):
    """История, точность текста, идемпотентность и откат без рабочих данных."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "data" / "sqlite3.db"
        self.manifest = {
            "export_id": "EXP_20261123T120000+0300",
            "snapshot_at": "2026-11-23T12:00:00+03:00",
            "schema_version": 2,
            "status": "complete",
            "is_complete": True,
        }
        self.content = (
            "# Сделка #123: Тестовая сделка\r\n\r\n"
            "**CRM:** https://example.invalid/crm/deal/details/123/\r\n\r\n"
            "- Ответственный: Ответственный\r\n- Клиент: Клиент\r\n"
            "- Текущая стадия: Новая\r\n- Страна: RB\r\n"
            "- Создана: 2026-11-23T10:00:00+03:00\r\n- Сумма: 1500.50 BYN\r\n\r\n"
            "## Лента касаний\r\n\r\n"
            "[2026-11-23 11:00] 💬 Telegram Клиент (Клиент) [стадия: Новая]:\r\n"
            "  Текст общения: ё 🎧\r\n\r\n"
            "## Поля сделки\r\n\r\n- Планируемый бюджет: 2000\r\n"
        )
        self.network = patch(
            "socket.socket.connect", side_effect=AssertionError("Сеть запрещена")
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def make_zip(
        self, name="snapshot.zip", manifest=None, extra=None, compression=ZIP_STORED
    ):
        files = {
            "manifest.json": json.dumps(
                self.manifest if manifest is None else manifest, ensure_ascii=False
            ).encode("utf-8"),
            DEAL: self.content.encode("utf-8"),
            LEAD: self.content.replace("Сделка #", "Лид #")
            .replace("/deal/", "/lead/")
            .replace("Текущая стадия", "Текущий статус")
            .replace("Поля сделки", "Поля лида")
            .encode("utf-8"),
        }
        files.update(extra or {})
        archive = self.root / name
        with ZipFile(archive, "w", compression=compression) as output:
            output.writestr("Сделки/Завершенные/", "")
            for path, content in files.items():
                output.writestr(path, content)
        return archive, files

    def rows(self, sql, parameters=()):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def test_exact_files_and_entity_identity(self):
        archive, files = self.make_zip()
        result = import_snapshot.import_snapshot(archive, self.database)
        self.assertTrue(result["added"])
        self.assertEqual(result["files"], 3)
        rows = self.rows("SELECT path, content, size_bytes, sha256 FROM snapshot_files")
        self.assertEqual(len(rows), len(files))
        for path, text, size, digest in rows:
            self.assertEqual(text.encode("utf-8"), files[path])
            self.assertEqual(size, len(files[path]))
            self.assertEqual(digest, hashlib.sha256(files[path]).hexdigest())
        self.assertEqual(
            self.rows(
                "SELECT entity, entity_id, entity_group FROM snapshot_files "
                "WHERE entity IS NOT NULL ORDER BY entity"
            ),
            [("deal", 123, "active"), ("lead", 123, "technical")],
        )

    def test_repacked_duplicate_is_skipped(self):
        first, _ = self.make_zip()
        repacked, _ = self.make_zip("repacked.zip", compression=ZIP_DEFLATED)
        import_snapshot.import_snapshot(first, self.database)
        self.assertFalse(
            import_snapshot.import_snapshot(repacked, self.database)["added"]
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshots"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshot_files"), [(3,)])

    def test_history_and_partial_snapshot(self):
        archive, _ = self.make_zip()
        import_snapshot.import_snapshot(archive, self.database)
        manifest = {
            **self.manifest,
            "export_id": "EXP_20261130T120000+0300",
            "snapshot_at": "2026-11-30T12:00:00+03:00",
            "status": "partial",
            "is_complete": False,
            "errors": [{"lead_id": 456, "error": "Искусственная ошибка"}],
        }
        new_content = self.content.replace("Тестовая сделка", "Новый текст")
        second, _ = self.make_zip(
            "second.zip", manifest=manifest, extra={DEAL: new_content.encode("utf-8")}
        )
        import_snapshot.import_snapshot(second, self.database)
        self.assertEqual(
            self.rows("SELECT status, is_complete FROM snapshots ORDER BY snapshot_at"),
            [("complete", 1), ("partial", 0)],
        )
        self.assertEqual(
            self.rows(
                "SELECT content FROM snapshot_files WHERE entity = 'deal' "
                "AND entity_id = 123 ORDER BY snapshot_id"
            ),
            [(self.content,), (new_content,)],
        )

    def test_conflict_preserves_existing_snapshot(self):
        archive, _ = self.make_zip()
        import_snapshot.import_snapshot(archive, self.database)
        changed, _ = self.make_zip("changed.zip", extra={DEAL: b"changed"})
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "содержимое отличается"):
            import_snapshot.import_snapshot(changed, self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_insert_failure_rolls_back_entire_new_snapshot(self):
        archive, _ = self.make_zip()
        import_snapshot.import_snapshot(archive, self.database)
        second, _ = self.make_zip(
            "second.zip", manifest={**self.manifest, "export_id": "second"}
        )
        original = import_snapshot.file_identity
        calls = 0

        def fail_after_first_file(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.IntegrityError("Искусственная ошибка записи")
            return original(path)

        with (
            patch.object(import_snapshot, "file_identity", fail_after_first_file),
            self.assertRaises(sqlite3.IntegrityError),
        ):
            import_snapshot.import_snapshot(second, self.database)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshots"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshot_files"), [(3,)])

    def test_invalid_archives_do_not_create_database(self):
        invalid = [
            ({"export_id": ""}, None),
            ({"snapshot_at": "2026-11-23"}, None),
            ({"schema_version": True}, None),
            ({"status": "partial"}, None),
            ({"status": None}, None),
            ({"is_complete": None}, None),
            ({}, {"manifest.json": b"not-json"}),
            ({}, {"manifest.json": b"[]"}),
            ({}, {"broken.md": b"\xff"}),
            ({}, {"../outside.md": b"text"}),
        ]
        for changes, extra in invalid:
            with self.subTest(changes=changes, extra=extra):
                archive, _ = self.make_zip(
                    manifest={**self.manifest, **changes}, extra=extra
                )
                with self.assertRaises(ValueError):
                    import_snapshot.import_snapshot(archive, self.database)
                self.assertFalse(self.database.exists())

    def test_missing_manifest_and_duplicate_paths(self):
        archive = self.root / "bad.zip"
        with ZipFile(archive, "w") as output:
            output.writestr("card.md", "text")
        with self.assertRaisesRegex(ValueError, "manifest.json"):
            import_snapshot.import_snapshot(archive, self.database)
        archive, _ = self.make_zip()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(archive, "a") as output:
                output.writestr(DEAL, "duplicate")
        with self.assertRaisesRegex(ValueError, "повторяющиеся"):
            import_snapshot.import_snapshot(archive, self.database)
        self.assertFalse(self.database.exists())

    def test_corrupt_zip_does_not_create_database(self):
        archive, _ = self.make_zip()
        with ZipFile(archive) as source:
            info = source.getinfo(DEAL)
            offset = info.header_offset + 30 + len(DEAL.encode("utf-8"))
        damaged = bytearray(archive.read_bytes())
        damaged[offset] ^= 1
        archive.write_bytes(damaged)
        with self.assertRaises(BadZipFile):
            import_snapshot.import_snapshot(archive, self.database)
        self.assertFalse(self.database.exists())

    def test_foreign_database_is_unchanged(self):
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE user_data (value TEXT)")
            connection.execute("INSERT INTO user_data VALUES ('keep')")
        before = self.database.read_bytes()
        archive, _ = self.make_zip()
        with self.assertRaisesRegex(ValueError, "другую схему"):
            import_snapshot.import_snapshot(archive, self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_cli_success_and_error(self):
        archive, _ = self.make_zip()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                load_script("import_snapshot").main(
                    [str(archive), "--db", str(self.database)]
                ),
                0,
            )
        self.assertIn("Снимок добавлен", output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            load_script("import_snapshot").main(
                [str(self.root / "missing.zip"), "--db", str(self.database)]
            )
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
