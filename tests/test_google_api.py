"""Проверки ZIP SQLite и Google Drive на временных данных, без ключа и сети."""

import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google_api


class GoogleDriveTests(unittest.TestCase):
    """Проверить согласованность базы, загрузку, очистку и освобождение ресурсов."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.root / "история.db"
        self.archive = self.root / "export" / "snapshots_2026_11_26 15-00.zip"
        self.snapshot_name = self.archive.with_suffix(".db").name
        self.logs = io.StringIO()
        sink = google_api.log.add(self.logs, format="{message}")
        self.addCleanup(google_api.log.remove, sink)
        for target in ("socket.socket.connect", "socket.getaddrinfo"):
            self.stack.enter_context(
                patch(target, side_effect=AssertionError("Сеть запрещена"))
            )
        self.credentials = self.stack.enter_context(
            patch.object(
                google_api.Credentials,
                "from_service_account_file",
                side_effect=AssertionError("Чтение ключа запрещено"),
            )
        )
        self.service = Mock()
        self.files = self.service.files.return_value
        self.files.list.return_value.execute.return_value = {
            "files": [{"id": "old-id", "name": "sqlite3_snapshots.zip"}]
        }
        self.files.update.return_value.next_chunk.side_effect = [
            (Mock(progress=Mock(return_value=0.5)), None),
            (None, {"id": "old-id"}),
        ]

    def make_database(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE messages (text TEXT)")
            connection.execute("INSERT INTO messages VALUES ('Привет')")

    def test_zip_contains_committed_wal_data_and_replaces_previous_archive(self):
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"previous archive")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE messages (text TEXT)")
            connection.execute("INSERT INTO messages VALUES ('Данные в WAL')")
            connection.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            google_api.make_database_zip(self.database, self.archive)
            with ZipFile(self.archive) as archive:
                self.assertEqual(archive.namelist(), [self.snapshot_name])
                self.assertEqual(
                    archive.getinfo(self.snapshot_name).compress_type, ZIP_DEFLATED
                )
                restored = self.root / "restored.db"
                restored.write_bytes(archive.read(self.snapshot_name))
            with closing(sqlite3.connect(restored)) as restored_connection:
                self.assertEqual(
                    restored_connection.execute("SELECT * FROM messages").fetchall(),
                    [("Данные в WAL",)],
                )
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone(), ("wal",)
            )
        self.assertFalse(list(self.archive.parent.glob(".drive-*")))
        messages = self.logs.getvalue()
        self.assertIn("создаю согласованную копию", messages)
        self.assertIn("сжимаю базу в ZIP", messages)
        self.assertIn("ZIP готов:", messages)

    def test_zip_cleans_all_exported_files_but_preserves_working_database(self):
        self.make_database()
        self.archive.parent.mkdir()
        for name in ("old.zip", "old.db", "old.db-wal", "old.db-shm", "stale.tmp"):
            (self.archive.parent / name).write_bytes(b"old")
        directory = self.archive.parent / "nested"
        directory.mkdir()
        (directory / "keep.txt").write_bytes(b"keep")
        google_api.make_database_zip(self.database, self.archive)
        self.assertEqual(set(self.archive.parent.iterdir()), {self.archive, directory})
        self.assertTrue(self.database.is_file())
        self.assertEqual((directory / "keep.txt").read_bytes(), b"keep")

    def test_exported_database_is_removed_only_after_successful_zip(self):
        self.archive.parent.mkdir()
        self.database = self.archive.parent / "old.db"
        self.make_database()
        self.archive.write_bytes(b"previous")
        with (
            patch.object(google_api.os, "replace", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            google_api.make_database_zip(self.database, self.archive)
        self.assertTrue(self.database.is_file())
        self.assertEqual(self.archive.read_bytes(), b"previous")
        self.assertFalse(list(self.archive.parent.glob(".drive-*")))
        google_api.make_database_zip(self.database, self.archive)
        self.assertEqual(list(self.archive.parent.iterdir()), [self.archive])
        with ZipFile(self.archive) as archive:
            restored = self.root / "restored.db"
            restored.write_bytes(archive.read(self.snapshot_name))
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM messages").fetchall(), [("Привет",)]
            )

    def test_missing_or_invalid_database_preserves_previous_archive(self):
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"keep")
        with self.assertRaises(FileNotFoundError):
            google_api.make_database_zip(self.database, self.archive)
        self.assertFalse(self.database.exists())
        self.database.write_bytes(b"invalid database")
        with self.assertRaises(sqlite3.DatabaseError):
            google_api.make_database_zip(self.database, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"keep")
        self.assertFalse(list(self.archive.parent.glob(".drive-*")))

    def test_service_uses_write_scope_and_timeout_without_network(self):
        self.credentials.side_effect = None
        with (
            patch.object(google_api.httplib2, "Http") as http,
            patch.object(google_api, "AuthorizedHttp") as authorized,
            patch.object(google_api, "build") as build,
        ):
            self.assertIs(
                google_api.create_drive_service(self.root / "fake.json", 12),
                build.return_value,
            )
        self.credentials.assert_called_once_with(
            str(self.root / "fake.json"),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        http.assert_called_once_with(timeout=12)
        authorized.assert_called_once_with(
            self.credentials.return_value, http=http.return_value
        )
        build.assert_called_once_with(
            "drive", "v3", http=authorized.return_value, cache_discovery=False
        )

    def test_update_preserves_id_and_renames_archive_while_uploading_valid_zip(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)

        def update(**kwargs):
            self.files.delete.assert_not_called()
            self.assertEqual(kwargs["fileId"], "old-id")
            self.assertTrue(kwargs["supportsAllDrives"])
            self.assertEqual(
                kwargs["body"],
                {"name": self.archive.name},
            )
            media = kwargs["media_body"]
            self.assertTrue(media.resumable())
            self.assertEqual(media.mimetype(), "application/zip")
            with ZipFile(media.stream()) as archive:
                self.assertEqual(archive.namelist(), [self.snapshot_name])
            return self.files.update.return_value

        self.files.update.side_effect = update
        result = google_api.replace_archive(self.service, self.archive, "folder-id", 2)
        self.assertEqual(result, "old-id")
        query = self.files.list.call_args.kwargs["q"]
        self.assertIn("'folder-id' in parents", query)
        self.assertIn("name = 'sqlite3_snapshots.zip'", query)
        self.assertIn("trashed = false", query)
        self.files.list.return_value.execute.assert_called_once_with(num_retries=2)
        self.assertEqual(self.files.update.return_value.next_chunk.call_count, 2)
        self.files.update.return_value.next_chunk.assert_called_with(num_retries=2)
        self.assertTrue(
            self.files.update.call_args.kwargs["media_body"].stream().closed
        )
        self.files.create.assert_not_called()
        self.files.delete.assert_not_called()
        messages = self.logs.getvalue()
        self.assertIn("ищу старые архивы", messages)
        self.assertIn("архив найден; загрузка 0%", messages)
        self.assertIn("загрузка 50%", messages)
        self.assertIn("загрузка 100%; существующий архив обновлён", messages)

    def test_empty_page_is_followed_and_incomplete_or_repeated_search_is_rejected(
        self,
    ):
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"zip")
        self.files.list.return_value.execute.side_effect = [
            {"files": [], "nextPageToken": "next"},
            {"files": [{"id": "old-id", "name": "sqlite3_snapshots.zip"}]},
        ]
        google_api.replace_archive(self.service, self.archive, "folder")
        self.assertEqual(self.files.list.call_args.kwargs["pageToken"], "next")
        for pages in (
            [
                {
                    "files": [{"id": "a", "name": self.archive.name}],
                    "incompleteSearch": True,
                }
            ],
            [{"nextPageToken": "next"}, {"nextPageToken": "next"}],
        ):
            with self.subTest(pages=pages):
                self.files.update.reset_mock()
                self.files.delete.reset_mock()
                self.files.list.return_value.execute.side_effect = pages
                with self.assertRaises(ValueError):
                    google_api.replace_archive(self.service, self.archive, "folder")
                self.files.update.assert_not_called()
                self.files.create.assert_not_called()
                self.files.delete.assert_not_called()

    def test_empty_folder_is_rejected_without_creating_or_deleting_files(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)
        self.files.list.return_value.execute.return_value = {"files": []}
        with self.assertRaises(FileNotFoundError):
            google_api.replace_archive(self.service, self.archive, "folder")
        self.files.create.assert_not_called()
        self.files.delete.assert_not_called()
        self.files.update.assert_not_called()

    def test_old_archives_are_deleted_after_upload_and_other_files_are_preserved(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)
        self.files.list.return_value.execute.side_effect = [
            {
                "files": [{"id": "legacy", "name": "sqlite3_snapshots.zip"}],
                "nextPageToken": "next",
            },
            {
                "files": [
                    {"id": "old-id", "name": self.archive.name},
                    {"id": "dated", "name": "snapshots_2026_11_25 12-00.zip"},
                    {"id": "colon", "name": "snapshots_2026_11_24 12:00.zip"},
                    {"id": "duplicate", "name": self.archive.name},
                    {"id": "other", "name": "snapshots_notes.zip"},
                ]
            },
        ]

        def upload(**kwargs):
            self.files.delete.assert_not_called()
            return None, {"id": "old-id"}

        self.files.update.return_value.next_chunk.side_effect = upload
        self.assertEqual(
            google_api.replace_archive(self.service, self.archive, "folder"), "old-id"
        )
        self.assertEqual(self.files.update.call_args.kwargs["fileId"], "old-id")
        self.files.create.assert_not_called()
        self.assertEqual(
            {call.kwargs["fileId"] for call in self.files.delete.call_args_list},
            {"legacy", "dated", "colon", "duplicate"},
        )
        for call in self.files.delete.call_args_list:
            self.assertTrue(call.kwargs["supportsAllDrives"])
        self.files.delete.return_value.execute.assert_called_with(num_retries=3)

    def test_latest_archive_with_old_time_format_is_updated_and_renamed(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)
        self.files.list.return_value.execute.return_value = {
            "files": [
                {"id": "old-id", "name": "snapshots_2026_11_25 15:00.zip"},
                {"id": "legacy", "name": "sqlite3_snapshots.zip"},
            ]
        }
        self.assertEqual(
            google_api.replace_archive(self.service, self.archive, "folder"), "old-id"
        )
        self.assertEqual(
            self.files.list.call_args.kwargs["orderBy"], "modifiedTime desc"
        )
        self.assertEqual(self.files.update.call_args.kwargs["fileId"], "old-id")
        self.assertEqual(
            self.files.update.call_args.kwargs["body"],
            {"name": "snapshots_2026_11_26 15-00.zip"},
        )
        self.files.delete.assert_called_once_with(
            fileId="legacy", supportsAllDrives=True
        )
        self.files.create.assert_not_called()

    def test_missing_confirmation_or_upload_error_does_not_delete_old_archives(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)
        self.files.list.return_value.execute.return_value = {
            "files": [
                {"id": "old-id", "name": "sqlite3_snapshots.zip"},
                {"id": "dated", "name": "snapshots_2026_11_25 12:00.zip"},
            ]
        }
        for outcome in (
            OSError("upload failed"),
            (None, {}),
            (None, {"id": "unexpected-id"}),
        ):
            with self.subTest(outcome=outcome):
                self.files.update.return_value.next_chunk.side_effect = [outcome]
                with self.assertRaises((OSError, ValueError)):
                    google_api.replace_archive(self.service, self.archive, "folder")
                self.files.delete.assert_not_called()

    def test_deletion_failure_keeps_updated_archive_and_reports_error(self):
        self.make_database()
        self.files.list.return_value.execute.return_value = {
            "files": [
                {"id": "old-id", "name": "sqlite3_snapshots.zip"},
                {"id": "dated", "name": "snapshots_2026_11_25 12:00.zip"},
            ]
        }
        self.files.delete.return_value.execute.side_effect = OSError("delete failed")
        with (
            patch.object(google_api, "create_drive_service", return_value=self.service),
            self.assertRaises(OSError),
        ):
            google_api.upload_database(
                self.database,
                self.archive,
                credentials_path=self.root / "fake.json",
                folder_id="folder",
            )
        self.assertEqual(self.files.delete.call_args.kwargs["fileId"], "dated")
        self.assertTrue(self.archive.is_file())
        self.assertTrue(self.database.is_file())
        self.service.close.assert_called_once_with()

    def test_upload_error_keeps_zip_and_closes_service_and_stream(self):
        self.make_database()
        self.files.update.return_value.next_chunk.side_effect = [
            (Mock(progress=Mock(return_value=1.0)), None),
            OSError("network error"),
        ]
        with (
            patch.object(google_api, "create_drive_service", return_value=self.service),
            self.assertRaises(OSError),
        ):
            google_api.upload_database(
                self.database,
                self.archive,
                credentials_path=self.root / "fake.json",
                folder_id="folder",
            )
        self.service.close.assert_called_once_with()
        self.assertTrue(
            self.files.update.call_args.kwargs["media_body"].stream().closed
        )
        with ZipFile(self.archive) as archive:
            self.assertEqual(archive.namelist(), [self.snapshot_name])
        messages = self.logs.getvalue()
        self.assertIn("прервана: OSError", messages)
        self.assertIn("загрузка 99%", messages)
        self.assertNotIn("network error", messages)
        self.assertNotIn("загрузка 100%", messages)

    def test_logging_import_does_not_load_project_config(self):
        path = Path(__file__).resolve().parents[1] / "logging_setup.py"
        spec = importlib.util.spec_from_file_location("isolated_logging", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": None}):
            spec.loader.exec_module(module)
        self.assertIsNotNone(module.log)

    def test_upload_logs_http_status_without_secret_response(self):
        self.make_database()
        error = OSError("https://example.invalid/secret?access_token=private-token")
        error.resp = type("Response", (), {"status": 403})()
        self.files.update.return_value.next_chunk.side_effect = error
        with (
            patch.object(google_api, "create_drive_service", return_value=self.service),
            self.assertRaises(OSError),
        ):
            google_api.upload_database(
                self.database,
                self.archive,
                credentials_path=self.root / "fake.json",
                folder_id="folder",
            )
        messages = self.logs.getvalue()
        self.assertIn("прервана: OSError (HTTP 403)", messages)
        self.assertNotIn("private-token", messages)
        self.assertNotIn("example.invalid", messages)


if __name__ == "__main__":
    unittest.main()
