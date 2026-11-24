"""Изолированные проверки структурированного SQLite на схеме JSON/JSONL проекта."""

import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from script_utils import load_script

import source_sqlite


class SourceSqliteTests(unittest.TestCase):
    """Поля, вложенные данные, история и отказ от несогласованных источников."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "structured.db"
        self.metadata = {
            entity: {
                "ASSIGNED_BY_ID": {"title": "Ответственный", "type": "user"},
                "UF_TEST": {
                    "title": "Выбор",
                    "type": "enumeration",
                    "list": {"1": "Вариант"},
                },
            }
            for entity in ("deal", "lead")
        }
        self.records = [self.record("deal"), self.record("lead")]
        self.manifest = {
            "export_id": "EXP_20261123T120000+0300",
            "snapshot_at": "2026-11-23T12:00:00+03:00",
            "schema_version": 2,
            "is_complete": True,
            "status": "complete",
            **{
                name: {"total": 1, "groups": {"active": {"count": 1, "ids": [7]}}}
                for name in ("deals", "leads")
            },
        }
        network = patch(
            "socket.socket.connect", side_effect=AssertionError("Сеть запрещена")
        )
        network.start()
        self.addCleanup(network.stop)

    def record(self, entity):
        fields = {
            "ASSIGNED_BY_ID": {"title": "Ответственный", "value": "Ответственный"},
            "UF_TEST": {"title": "Выбор", "value": "Вариант"},
        }
        deal = {
            "ID": "7",
            "TITLE": "Тестовая сущность",
            "ASSIGNED_BY_ID": "42",
            "OPPORTUNITY": "123.4500",
            "CURRENCY_ID": "BYN",
            "UF_TEST": "1",
            "UF_ARRAY": [{"VALUE": "Текст", "TYPE": "WORK"}, {"VALUE": "Ещё"}],
            "UF_NULL": None,
            "UF_EMPTY": "",
            "UF_FALSE": False,
            "UF_ZERO": 0,
            "_fields": fields,
            "_country": "RB",
            "_source": "Источник",
            "_contact_name": "Клиент",
        }
        tp = {
            "deal_id": 7,
            "touchpoint_id": "call_100",
            "timestamp": "2026-11-23T11:00:05+03:00",
            "channel": "call",
            "event_type": "call",
            "direction": "out",
            "actor": {"type": "manager", "user_id": 11, "name": "Исполнитель"},
            "responsible_user_id": 42,
            "client": {"contact_id": 55, "name": "Клиент"},
            "deal_stage_at_time": {"stage_id": "NEW", "name": "Новая"},
            "content": {
                "type": "transcript",
                "text": "Первая строка\n  отступ 🎧",
                "duration_sec": 125,
                "transcript_status": "assemblyai",
                "failed_code": None,
                "phone": "+000",
            },
            "source": {"activity_id": 100, "file_ids": [500, 501]},
        }
        stats = {
            "deal_id" if entity == "deal" else "lead_id": 7,
            "entity": entity,
            "group": "active",
            "touchpoints": 1,
            "country": "RB",
            "stage": "Новая",
            "fields": copy.deepcopy(fields),
        }
        return {
            "entity": entity,
            "group": "active",
            "deal": deal,
            "stats": stats,
            "touchpoints": [tp],
        }

    def write(self, manifest=None, records=None):
        return source_sqlite.write_snapshot(
            self.database,
            self.manifest if manifest is None else manifest,
            lambda: iter(self.records if records is None else records),
            self.metadata,
        )

    def rows(self, sql):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql).fetchall()

    def test_fields_have_separate_columns_and_keep_values(self):
        self.write()
        self.assertEqual(
            self.rows(
                "SELECT entity, entity_id, raw_ASSIGNED_BY_ID, raw_OPPORTUNITY, raw_UF_TEST, "
                "display_UF_TEST, label_UF_TEST, raw_UF_NULL, raw_UF_EMPTY, raw_UF_FALSE, raw_UF_ZERO "
                "FROM entities ORDER BY entity"
            ),
            [
                (kind, 7, 42, "123.4500", "1", "Вариант", "Выбор", None, "", 0, 0)
                for kind in ("deal", "lead")
            ],
        )
        self.assertEqual(
            json.loads(self.rows("SELECT raw_UF_ARRAY FROM entities LIMIT 1")[0][0]),
            self.records[0]["deal"]["UF_ARRAY"],
        )
        self.assertEqual(
            self.rows(
                "SELECT value_path, value FROM field_items WHERE entity = 'deal' "
                "AND field_column = 'raw_UF_ARRAY' ORDER BY value_path"
            ),
            [("/0/TYPE", "WORK"), ("/0/VALUE", "Текст"), ("/1/VALUE", "Ещё")],
        )

    def test_actor_is_not_responsible_and_timestamp_keeps_seconds(self):
        self.write()
        self.assertEqual(
            self.rows(
                'SELECT "timestamp", actor__user_id, responsible_user_id, content__duration_sec, '
                "content__text, source__activity_id FROM touchpoints WHERE entity = 'deal'"
            ),
            [
                (
                    "2026-11-23T11:00:05+03:00",
                    11,
                    42,
                    125,
                    "Первая строка\n  отступ 🎧",
                    100,
                )
            ],
        )
        self.assertEqual(
            self.rows(
                "SELECT value FROM field_items WHERE entity = 'deal' "
                "AND record_kind = 'touchpoint' AND field_column = 'source__file_ids' "
                "ORDER BY value_path"
            ),
            [(500,), (501,)],
        )

    def test_labels_and_original_metadata_are_available(self):
        self.write()
        rows = self.rows(
            "SELECT title, field_type, raw_column, display_column, metadata_json "
            "FROM field_definitions WHERE entity = 'deal' AND field_code = 'UF_TEST'"
        )
        self.assertEqual(
            rows[0][:4], ("Выбор", "enumeration", "raw_UF_TEST", "display_UF_TEST")
        )
        self.assertEqual(json.loads(rows[0][4]), self.metadata["deal"]["UF_TEST"])

    def test_history_new_column_and_partial_status(self):
        self.write()
        records = copy.deepcopy(self.records)
        records[0]["deal"]["UF_NEW"] = "Новое поле"
        manifest = {
            **self.manifest,
            "export_id": "second",
            "is_complete": False,
            "status": "partial",
        }
        self.write(manifest, records)
        self.assertEqual(
            self.rows(
                "SELECT raw_UF_NEW FROM entities WHERE entity = 'deal' ORDER BY snapshot_id"
            ),
            [(None,), ("Новое поле",)],
        )
        self.assertEqual(
            self.rows("SELECT status, is_complete FROM snapshots ORDER BY snapshot_id"),
            [("complete", 1), ("partial", 0)],
        )

    def test_repeat_and_conflict_leave_database_unchanged(self):
        self.write()
        before = self.database.read_bytes()
        self.assertFalse(self.write()["added"])
        records = copy.deepcopy(self.records)
        records[0]["deal"]["TITLE"] = "Изменилось"
        with self.assertRaisesRegex(source_sqlite.SourceError, "Конфликт"):
            self.write(records=records)
        self.assertEqual(self.database.read_bytes(), before)

    def test_bad_touchpoint_rolls_back_new_snapshot_and_new_column(self):
        self.write()
        before = self.database.read_bytes()
        records = copy.deepcopy(self.records)
        records[0]["deal"]["UF_NEW"] = "Не должно сохраниться"
        records[1]["touchpoints"][0]["deal_id"] = 999
        with self.assertRaises(source_sqlite.SourceError):
            self.write({**self.manifest, "export_id": "second"}, records)
        self.assertEqual(self.database.read_bytes(), before)

    def test_source_change_during_read_is_rejected(self):
        changed = copy.deepcopy(self.records)
        changed[0]["deal"]["TITLE"] = "Изменение во время чтения"
        with self.assertRaisesRegex(source_sqlite.SourceError, "во время чтения"):
            source_sqlite.write_snapshot(
                self.database,
                self.manifest,
                unittest.mock.Mock(side_effect=[iter(self.records), iter(changed)]),
                self.metadata,
            )
        self.assertEqual(self.rows("SELECT name FROM sqlite_master"), [])

    def test_cli_uses_only_agreed_store_files(self):
        data = self.root / "data"
        export = self.root / "export"
        export.mkdir()
        registry = {}
        for record in self.records:
            entity = record["entity"]
            root = data / "store" if entity == "deal" else data / "store" / "lead"
            for directory in ("deal", "index", "tps"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for directory, value in (
                ("deal", record["deal"]),
                ("index", record["stats"]),
            ):
                (root / directory / "7.json").write_text(
                    source_sqlite.dumps(value), encoding="utf-8"
                )
            (root / "tps" / "7.jsonl").write_text(
                source_sqlite.dumps(record["touchpoints"][0]) + "\n", encoding="utf-8"
            )
            (data / f"{entity}_fields_meta.json").write_text(
                source_sqlite.dumps(self.metadata[entity]), encoding="utf-8"
            )
            (export / f"index_{entity}_active.jsonl").write_text(
                source_sqlite.dumps(record["stats"]) + "\n", encoding="utf-8"
            )
            (export / f"touchpoints_{entity}_active.jsonl").write_text(
                source_sqlite.dumps(record["touchpoints"][0]) + "\n", encoding="utf-8"
            )
            registry["7" if entity == "deal" else "lead:7"] = {
                "exported_at": self.manifest["export_id"],
                "group": "active",
            }
        registry_path = data / "registry.json"
        registry_path.write_text(source_sqlite.dumps(registry), encoding="utf-8")
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(source_sqlite.dumps(self.manifest), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                load_script("source_sqlite").main(
                    [
                        "--data-dir",
                        str(data),
                        "--export-dir",
                        str(export),
                        "--manifest",
                        str(manifest_path),
                        "--db",
                        str(self.database),
                    ]
                ),
                0,
            )
        registry["7"]["exported_at"] = "another-snapshot"
        registry_path.write_text(source_sqlite.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(source_sqlite.SourceError, "разным снимкам"):
            list(source_sqlite.store_records(data, export, self.manifest))

    def test_all_touchpoint_shapes_keep_their_fields(self):
        records = copy.deepcopy(self.records)
        events = records[0]["touchpoints"]
        for kind, content, source in (
            (
                "message",
                {"type": "text", "text": "Сообщение"},
                {"chat_id": 8, "message_id": 9},
            ),
            ("comment", {"type": "text", "text": "Комментарий"}, {"comment_id": 10}),
            ("email", {"type": "text", "text": "Тема\nПисьмо"}, {"activity_id": 11}),
            ("meeting", {"type": "text", "text": "Встреча"}, {"activity_id": 12}),
            (
                "task",
                {
                    "type": "text",
                    "text": "Задача",
                    "description": "Описание",
                    "status": "Новая",
                    "deadline": "2026-11-24T12:00:01+03:00",
                    "closed_date": None,
                    "created_by": "Постановщик",
                },
                {"task_id": 13},
            ),
            (
                "stage_change",
                {
                    "type": "meta",
                    "text": "",
                    "stage_from": {"stage_id": "NEW", "name": "Новая"},
                    "stage_to": {"stage_id": "WON", "name": "Успех"},
                },
                {},
            ),
        ):
            tp = copy.deepcopy(events[0])
            tp.update(
                touchpoint_id=kind, event_type=kind, content=content, source=source
            )
            events.append(tp)
        records[0]["stats"]["touchpoints"] = len(events)
        self.write(records=records)
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM touchpoints WHERE entity = 'deal'"), [(7,)]
        )
        self.assertEqual(
            self.rows(
                "SELECT content__deadline, content__created_by, source__task_id "
                "FROM touchpoints WHERE event_type = 'task'"
            ),
            [("2026-11-24T12:00:01+03:00", "Постановщик", 13)],
        )
        self.assertEqual(
            self.rows(
                "SELECT content__stage_from__stage_id, content__stage_to__stage_id "
                "FROM touchpoints WHERE event_type = 'stage_change'"
            ),
            [("NEW", "WON")],
        )

    def test_manifest_ids_must_match_even_when_count_matches(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["deals"]["groups"]["active"]["ids"] = [999]
        with self.assertRaisesRegex(source_sqlite.SourceError, "ID сущностей"):
            self.write(manifest=manifest)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master"), [])

    def test_declared_field_without_values_still_has_columns(self):
        self.metadata["deal"]["UF_ABSENT"] = {"title": "Пустое поле", "type": "string"}
        self.write()
        self.assertEqual(
            self.rows(
                "SELECT raw_UF_ABSENT, display_UF_ABSENT FROM entities WHERE entity = 'deal'"
            ),
            [(None, None)],
        )

    def test_foreign_database_is_not_changed(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE preserve (value TEXT)")
        before = self.database.read_bytes()
        with self.assertRaises(source_sqlite.SourceError):
            self.write()
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
