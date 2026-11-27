"""Проверки ZIP SQLite и Google Drive на временных данных, без ключа и сети."""

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
    """Проверить согласованность базы, замену по ID и освобождение ресурсов."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.root / "история.db"
        self.archive = self.root / "export" / "sqlite3_snapshots.zip"
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
            "files": [{"id": "old-id"}]
        }
        self.files.update.return_value.next_chunk.side_effect = [
            (Mock(), None),
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
                self.assertEqual(archive.namelist(), [self.database.name])
                self.assertEqual(
                    archive.getinfo(self.database.name).compress_type, ZIP_DEFLATED
                )
                restored = self.root / "restored.db"
                restored.write_bytes(archive.read(self.database.name))
            with closing(sqlite3.connect(restored)) as restored_connection:
                self.assertEqual(
                    restored_connection.execute("SELECT * FROM messages").fetchall(),
                    [("Данные в WAL",)],
                )
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone(), ("wal",)
            )
        self.assertFalse(list(self.archive.parent.glob(".drive-*")))

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

    def test_update_preserves_file_id_and_sends_valid_zip_in_chunks(self):
        self.make_database()
        google_api.make_database_zip(self.database, self.archive)

        def update(**kwargs):
            self.assertEqual(kwargs["fileId"], "old-id")
            self.assertTrue(kwargs["supportsAllDrives"])
            media = kwargs["media_body"]
            self.assertTrue(media.resumable())
            self.assertEqual(media.mimetype(), "application/zip")
            with ZipFile(media.stream()) as archive:
                self.assertEqual(archive.namelist(), [self.database.name])
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

    def test_empty_page_is_followed_and_duplicates_or_incomplete_search_are_rejected(
        self,
    ):
        self.archive.parent.mkdir()
        self.archive.write_bytes(b"zip")
        self.files.list.return_value.execute.side_effect = [
            {"files": [], "nextPageToken": "next"},
            {"files": [{"id": "old-id"}]},
        ]
        google_api.replace_archive(self.service, self.archive, "folder")
        self.assertEqual(self.files.list.call_args.kwargs["pageToken"], "next")
        for pages in (
            [{"files": []}],
            [{"files": [{"id": "a"}, {"id": "b"}]}],
            [{"files": [{"id": "a"}], "incompleteSearch": True}],
            [
                {"files": [{"id": "a"}], "nextPageToken": "next"},
                {"files": [{"id": "b"}]},
            ],
            [{"nextPageToken": "next"}, {"nextPageToken": "next"}],
        ):
            with self.subTest(pages=pages):
                self.files.update.reset_mock()
                self.files.list.return_value.execute.side_effect = pages
                with self.assertRaises((ValueError, FileNotFoundError)):
                    google_api.replace_archive(self.service, self.archive, "folder")
                self.files.update.assert_not_called()

    def test_upload_error_keeps_zip_and_closes_service_and_stream(self):
        self.make_database()
        self.files.update.return_value.next_chunk.side_effect = OSError("network error")
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
            self.assertEqual(archive.namelist(), [self.database.name])


if __name__ == "__main__":
    unittest.main()
