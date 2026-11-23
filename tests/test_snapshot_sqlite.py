"""Изолированные проверки версий конечных карточек и истории ZIP в SQLite."""

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from script_utils import load_script

import snapshot_sqlite
from parse_snapshot import parse_card, render_layout

MESSAGE = "[2026-11-20 11:00] 💬 Telegram Продавец [стадия: Новая]:\n  Добрый день!\n"
CALL = (
    "[2026-11-20 12:00] 📞 Звонок (исходящий, 2:03) — Другой продавец"
    " [стадия: Новая] | запись: file_id 10, 11 (call.mp3)\n"
    "Транскрипция:\n  Разговор: ё 🎧\n\n"
)
TASK = (
    "[2026-11-20 13:00] 📋 Задача — Продавец [стадия: Новая]: Позвонить"
    "  (Новая, дедлайн 2026-11-23 10:00, постановщик: Иванов)\n  Описание задачи\n\n"
)


def card(
    events=MESSAGE + CALL + TASK,
    fields="- Страна: Беларусь\n",
    entity="deal",
    title="Карточка",
):
    """Конечная карточка принятого формата без импортов приложения."""
    return (
        f"# {'Сделка' if entity == 'deal' else 'Лид'} #123: {title}\n\n"
        f"**CRM:** https://example.invalid/crm/{entity}/details/123/\n\n"
        "- Ответственный: Продавец\n- Клиент: Покупатель\n"
        f"- {'Текущая стадия' if entity == 'deal' else 'Текущий статус'}: Новая\n"
        "- Источник: Telegram\n- Страна: RB\n"
        "- Создана: 2026-11-19T10:00:00+03:00\n- Сумма: 1000.50 BYN\n\n"
        f"## Лента касаний\n\n{events}\n"
        f"## Поля {'сделки' if entity == 'deal' else 'лида'}\n\n{fields}"
    )


class SnapshotSqliteTests(unittest.TestCase):
    """Проверять структуру, устранение повторов и сохранность каждого слепка."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "snapshots.db"
        network = patch(
            "socket.socket.connect", side_effect=AssertionError("Сеть запрещена")
        )
        network.start()
        self.addCleanup(network.stop)

    def make_zip(
        self, export_id="first", contents=None, partial=False, compression=ZIP_STORED
    ):
        if contents is None:
            contents = [("deal", "active", card())]
        manifest = {
            "export_id": export_id,
            "snapshot_at": "2026-11-23T12:00:00+03:00",
            "snapshot_timezone": "Europe/Minsk",
            "schema_version": 2,
            "status": "partial" if partial else "complete",
            "is_complete": not partial,
        }
        for entity, label in (("deal", "deals"), ("lead", "leads")):
            selected = [group for kind, group, _ in contents if kind == entity]
            manifest[label] = {
                "total": len(selected),
                f"{entity}_ids": [123] if selected else [],
                "groups": {
                    group: {
                        "count": selected.count(group),
                        "ids": [123] if group in selected else [],
                    }
                    for group in ("active", "completed", "technical")
                },
            }
        path = self.root / f"{export_id}.zip"
        files = {
            "manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        }
        for entity, group, content in contents:
            parent = "Сделки" if entity == "deal" else "Лиды"
            group_name = {
                "active": "Активные",
                "completed": "Завершенные",
                "technical": "Технические",
            }[group]
            filename = f"LEXS_{entity.upper()}_RB_ON_123_SNAP_{export_id}_schema-v2.md"
            files[f"{parent}/{group_name}/{filename}"] = content.encode("utf-8")
        with ZipFile(path, "w", compression=compression) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return path, files

    def rows(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def restore(self, export_id="first", entity="deal"):
        with closing(sqlite3.connect(self.db)) as connection:
            return snapshot_sqlite.restore_card(connection, export_id, entity, 123)

    def test_columns_events_and_exact_reconstruction(self):
        archive, files = self.make_zip(
            contents=[
                ("deal", "active", card()),
                ("lead", "technical", card(entity="lead")),
            ]
        )
        result = snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(result["cards"], 2)
        for entity in ("deal", "lead"):
            self.assertEqual(self.restore(entity=entity), card(entity=entity))
        self.assertEqual(
            self.rows('SELECT country, "field_Страна", amount FROM cards'),
            [("RB", "Беларусь", "1000.50 BYN")] * 2,
        )
        self.assertEqual(
            self.rows(
                "SELECT occurred_at, actor_name, responsible_name, duration_seconds, recording_file_ids, transcript_status FROM event_history WHERE event_type='call' AND entity='deal'"
            ),
            [
                (
                    "2026-11-20 12:00",
                    "Другой продавец",
                    "Продавец",
                    123,
                    "10, 11",
                    "present",
                )
            ],
        )
        self.assertEqual(
            self.rows(
                "SELECT task_title, task_status, deadline, created_by FROM events WHERE event_type='task' AND entity='deal'"
            ),
            [("Позвонить", "Новая", "2026-11-23 10:00", "Иванов")],
        )
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(len(files), 3)

    def test_identical_snapshots_reuse_versions(self):
        a, _ = self.make_zip()
        b, _ = self.make_zip("second")
        snapshot_sqlite.import_snapshot(a, self.db)
        snapshot_sqlite.import_snapshot(b, self.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM card_versions"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(3,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshot_events"), [(6,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM timelines"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM timeline_events"), [(3,)])
        self.assertEqual(self.restore("first"), self.restore("second"))

    def test_scalar_update_reuses_whole_timeline(self):
        a, _ = self.make_zip()
        changed = card().replace("1000.50 BYN", "2000 BYN")
        b, _ = self.make_zip("second", [("deal", "completed", changed)])
        snapshot_sqlite.import_snapshot(a, self.db)
        snapshot_sqlite.import_snapshot(b, self.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM card_versions"), [(2,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM timelines"), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM timeline_events"), [(3,)])
        self.assertEqual(self.restore("second"), changed)

    def test_added_updated_removed_events_and_scalar_changes(self):
        a, _ = self.make_zip()
        changed_call = CALL.replace("Разговор: ё 🎧", "Исправленная транскрипция")
        changed_task = TASK.replace("дедлайн 2026-11-23", "дедлайн 2026-11-24")
        new_text = card(
            events=changed_call + changed_task, fields="- Новое поле: Добавлено\n"
        ).replace("1000.50 BYN", "2000 BYN")
        b, _ = self.make_zip("second", [("deal", "completed", new_text)], partial=True)
        c, _ = self.make_zip(
            "third",
            [
                (
                    "deal",
                    "completed",
                    new_text.replace(changed_task, changed_task + MESSAGE),
                )
            ],
        )
        for path in (a, b, c):
            snapshot_sqlite.import_snapshot(path, self.db)
        self.assertEqual(self.restore(), card())
        self.assertEqual(self.restore("second"), new_text)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(5,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM card_versions"), [(2,)])
        self.assertEqual(
            self.rows('SELECT "field_Новое поле" FROM cards ORDER BY snapshot_id'),
            [(None,), ("Добавлено",), ("Добавлено",)],
        )
        self.assertEqual(
            self.rows(
                "SELECT COUNT(*) FROM event_history WHERE snapshot_id='second' AND event_type='message'"
            ),
            [(0,)],
        )
        self.assertEqual(
            self.rows("SELECT status FROM snapshots WHERE snapshot_id='second'"),
            [("partial",)],
        )

    def test_same_minute_duplicates_and_sequence(self):
        second = MESSAGE.replace("Добрый день!", "Второе сообщение")
        text = card(events=MESSAGE + second + MESSAGE)
        archive, _ = self.make_zip(contents=[("deal", "active", text)])
        snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(2,)])
        self.assertEqual(
            self.rows("SELECT position FROM snapshot_events ORDER BY position"),
            [(0,), (1,), (2,)],
        )
        self.assertEqual(self.restore(), text)

    def test_multiline_actor_and_task_title(self):
        message = MESSAGE.replace("Продавец", "Клиент (Имя\nФамилия)")
        task = TASK.replace("Позвонить", "Позвонить\nобсудить предложение")
        text = card(events=message + task)
        archive, _ = self.make_zip(contents=[("deal", "active", text)])
        snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(self.restore(), text)
        self.assertEqual(
            self.rows("SELECT actor_name FROM events WHERE event_type='message'"),
            [("Клиент (Имя\nФамилия)",)],
        )
        self.assertEqual(
            self.rows("SELECT task_title FROM events WHERE event_type='task'"),
            [("Позвонить\nобсудить предложение",)],
        )

    def test_audio_without_file_ids_keeps_actor_separate(self):
        call = CALL.replace(" | запись: file_id 10, 11", "")
        other = CALL.replace(" | запись: file_id 10, 11 (call.mp3)", "").replace(
            "Другой продавец", "Продавец (отдел продаж)"
        )
        text = card(events=call + other)
        archive, _ = self.make_zip(contents=[("deal", "active", text)])
        snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(self.restore(), text)
        self.assertEqual(
            self.rows(
                "SELECT actor_name, audio_filename FROM events ORDER BY event_id"
            ),
            [("Другой продавец", "call.mp3"), ("Продавец (отдел продаж)", None)],
        )

    def test_roundtrip_failure_rolls_back_first_import(self):
        archive, _ = self.make_zip()
        with (
            patch.object(snapshot_sqlite, "restore_card", return_value="wrong"),
            self.assertRaisesRegex(ValueError, "восстановленный"),
        ):
            snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(
            self.rows("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"),
            [],
        )

    def test_multiline_duplicate_labels_case_and_crlf(self):
        fields = '- Имя: a\n- имя: b\n- Имя: c\n- Кавычка"; DROP TABLE events; --: x\nстрока 2\n- Пустое: \n'
        text = (
            card(fields=fields, title="Название\nпродолжение")
            .replace("Клиент: Покупатель", "Клиент: Покупатель\nпродолжение")
            .replace("\n", "\r\n")
        )
        archive, _ = self.make_zip(contents=[("deal", "active", text)])
        snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(self.restore(), text)
        self.assertEqual(
            self.rows(
                'SELECT "field_Имя", "field_Имя__2", "field_Пустое" FROM card_versions'
            ),
            [("a", "c", "")],
        )
        self.assertEqual(
            self.rows(
                "SELECT COUNT(*) FROM field_columns WHERE label IN ('Имя','имя')"
            ),
            [(3,)],
        )

    def test_empty_timeline_missing_timestamp_and_other_types(self):
        text = card(events="")
        parsed = parse_card(text, "deal", 123)
        self.assertEqual(render_layout(parsed["layout"], parsed["values"]), text)
        events = (
            "[] 💬 Telegram Продавец: (без текста)\n"
            "[2026-11-20 13:00] 📌 Стадия: Новая → Счёт\n"
            "[2026-11-20 13:01] 📝 Продавец:\n  Комментарий\n"
            "[2026-11-20 13:02] ✉️ Продавец:\n  Письмо\n"
            "[2026-11-20 13:03] 🤝 Продавец:\n  Встреча\n"
            "[2026-11-20 13:04] fbinstagramdirect Клиент (Покупатель):\n  Текст\n"
            "[2026-11-20 13:05] 📞 Звонок () — Продавец\n  (звонок не состоялся: 486)\n  (запись есть, транскрибация не выполнена)\n\n"
        )
        text = card(events=events)
        archive, _ = self.make_zip(contents=[("deal", "active", text)])
        snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(self.restore(), text)
        self.assertEqual(
            self.rows("SELECT DISTINCT event_type FROM events ORDER BY event_type"),
            [
                (kind,)
                for kind in (
                    "call",
                    "comment",
                    "email",
                    "meeting",
                    "message",
                    "stage_change",
                )
            ],
        )
        self.assertEqual(
            self.rows(
                "SELECT failure_code, transcript_status FROM events WHERE event_type='call'"
            ),
            [("486", "no_transcript")],
        )

    def test_reimport_conflict_and_repacking(self):
        archive, files = self.make_zip()
        snapshot_sqlite.import_snapshot(archive, self.db)
        before = self.db.read_bytes()
        repacked = self.root / "repacked.zip"
        with ZipFile(repacked, "w", ZIP_DEFLATED) as target:
            for name, data in files.items():
                target.writestr(name, data)
        self.assertFalse(snapshot_sqlite.import_snapshot(repacked, self.db)["added"])
        self.assertEqual(self.db.read_bytes(), before)
        changed, _ = self.make_zip(
            contents=[("deal", "active", card(title="Изменено"))]
        )
        with self.assertRaisesRegex(ValueError, "содержимое отличается"):
            snapshot_sqlite.import_snapshot(changed, self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_rollback_including_new_columns(self):
        a, _ = self.make_zip()
        snapshot_sqlite.import_snapshot(a, self.db)
        before = self.rows("SELECT sql FROM sqlite_master ORDER BY name")
        b, _ = self.make_zip(
            "second",
            [
                ("deal", "active", card(fields="- Добавленное: x\n")),
                ("lead", "technical", "broken"),
            ],
        )
        with self.assertRaises(ValueError):
            snapshot_sqlite.import_snapshot(b, self.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshots"), [(1,)])
        self.assertEqual(
            self.rows("SELECT sql FROM sqlite_master ORDER BY name"), before
        )
        self.assertEqual(self.restore(), card())

    def test_manifest_mismatch_unknown_files_and_foreign_database(self):
        archive, files = self.make_zip()
        manifest = json.loads(files["manifest.json"])
        manifest["deals"]["groups"]["active"]["ids"] = [999]
        invalid = self.root / "invalid.zip"
        with ZipFile(invalid, "w") as output:
            for name, data in files.items():
                output.writestr(
                    name, json.dumps(manifest) if name == "manifest.json" else data
                )
        with self.assertRaisesRegex(ValueError, "Группы"):
            snapshot_sqlite.import_snapshot(invalid, self.db)
        self.assertFalse(self.db.exists())
        with ZipFile(archive, "a") as output:
            output.writestr("unknown.txt", "unsupported")
        with self.assertRaisesRegex(ValueError, "вне структуры"):
            snapshot_sqlite.import_snapshot(archive, self.db)
        archive, _ = self.make_zip()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE keep (value TEXT)")
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "другую схему"):
            snapshot_sqlite.import_snapshot(archive, self.db)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_cli_and_source_mutation_rollback(self):
        archive, _ = self.make_zip()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                load_script("snapshot_sqlite").main(
                    [str(archive), "--db", str(self.db)]
                ),
                0,
            )
        real = snapshot_sqlite.read_snapshot
        second, _ = self.make_zip("second")
        calls = 0

        def changed(source):
            nonlocal calls
            calls += 1
            manifest, files, fingerprint = real(source)
            return manifest, files, fingerprint if calls == 1 else "changed"

        with (
            patch.object(snapshot_sqlite, "read_snapshot", changed),
            self.assertRaisesRegex(ValueError, "изменился"),
        ):
            snapshot_sqlite.import_snapshot(second, self.db)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM snapshots"), [(1,)])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            load_script("snapshot_sqlite").main(
                [str(self.root / "missing.zip"), "--db", str(self.db)]
            )
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
