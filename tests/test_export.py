"""Изолированные регрессионные проверки выгрузки на синтетических данных."""

import copy
import hashlib
import importlib
import io
import json
import logging
import os
import runpy
import socket
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests
from loguru import logger
from script_utils import load_script

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
SANDBOX = None
STAMP = "2026-11-23T12:00:00+0300"
ISO = "2026-11-23T12:00:00+03:00"
PROJECT_MODULES = (
    "config",
    "logging_setup",
    "performance",
    "api_client",
    "export_llm",
    "fetch_activities",
    "fetch_deals",
    "fetch_leads",
    "fetch_openlines",
    "fetch_tasks",
    "normalize",
    "registry",
    "store",
    "sync",
    "transcribe",
    "users",
)


def audit(event, args):
    """Запретить сеть, subprocess, секреты и запись вне песочницы процесса тестов."""
    if event in {
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "subprocess.Popen",
        "os.system",
    }:
        raise RuntimeError(f"Изоляция: запрещено {event}")
    paths = ()
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes, os.PathLike)):
            if Path(os.fsdecode(path)).name in {".env", "config.py"}:
                raise RuntimeError("Изоляция: чтение настоящего config/.env запрещено")
            if (mode and any(c in mode for c in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                paths = ((path, None),)
    elif event in {"os.mkdir", "os.remove", "os.rmdir"}:
        paths = ((args[0], args[2] if event == "os.mkdir" else args[1]),)
    elif event in {"os.rename", "os.link", "os.symlink"}:
        paths = (
            ((args[0], args[2]), (args[1], args[3]))
            if event != "os.symlink"
            else ((args[1], args[2]),)
        )
    for path, dir_fd in paths:
        target = Path(os.fsdecode(path))
        if (
            SANDBOX is not None
            and not target.is_absolute()
            and dir_fd not in (None, -1)
        ):
            # shutil.rmtree на POSIX удаляет файлы относительно открытого каталога.
            stat = os.fstat(dir_fd)
            parent = next(
                (
                    p
                    for p in (SANDBOX, *SANDBOX.rglob("*"))
                    if p.is_dir() and os.path.samestat(p.stat(), stat)
                ),
                None,
            )
            if parent is None:
                raise RuntimeError("Изоляция: dir_fd вне временного каталога")
            target = parent / target
        target = target.resolve()
        if SANDBOX is not None and not target.is_relative_to(SANDBOX):
            raise RuntimeError(f"Изоляция: запись вне временного каталога ({event})")


sys.addaudithook(audit)


class FixedDatetime(datetime):
    """Единое воспроизводимое время снимка."""

    @classmethod
    def now(cls, tz=None):
        value = cls.fromisoformat(ISO)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def make_config(root):
    """Подменить настройки до импорта проекта, не выполняя config.py."""
    cfg = types.ModuleType("config")
    values = {
        "WEBHOOK_URL": "https://example.invalid/rest/1/test/",
        "PORTAL_URL": "https://example.invalid",
        "DEAL_IDS": [],
        "LEAD_IDS": [],
        "DEALS_LIMIT": 0,
        "LEADS_LIMIT": 0,
        "DATE_FROM": None,
        "EXPORT_PERIOD_FROM": None,
        "EXPORT_PERIOD_TO": None,
        "PROCESS_LEADS": True,
        "PROCESS_ACTIVE": True,
        "PROCESS_COMPLETED": True,
        "PROCESS_TECHNICAL": True,
        "TECHNICAL_STAGE_IDS": {"*": ["JUNK"]},
        "TECHNICAL_STAGE_KEYWORDS": ["дубль"],
        "LEAD_TECHNICAL_STATUS_IDS": ["JUNK"],
        "LEAD_TECHNICAL_STATUS_KEYWORDS": ["дубль"],
        "INCLUDE_COMMENTS": True,
        "INCLUDE_TASKS": True,
        "INCLUDE_EMAILS": True,
        "INCLUDE_MEETINGS": True,
        "DOWNLOAD_RECORDINGS": False,
        "MIN_RECORDING_DURATION_SEC": 15,
        "TRANSCRIBE_ENABLED": True,
        "ASSEMBLYAI_DIRECT_URL": True,
        "ASSEMBLYAI_API_KEY": "synthetic",
        "ASSEMBLYAI_LANGUAGE": "ru",
        "ASSEMBLYAI_SPEAKERS": 2,
        "ASSEMBLYAI_POLL_INTERVAL": 0,
        "ASSEMBLYAI_TIMEOUT": 10,
        "REQUEST_PAUSE": 0,
        "REQUEST_TIMEOUT": 1,
        "MAX_RETRIES": 3,
        "SCHEMA_VERSION": 2,
        "SNAPSHOT_TZ": "Europe/Minsk",
        "EXPORT_TYPE": "auto",
        "INCREMENTAL_OVERLAP_HOURS": 24,
        "REEXPORT_CLOSED_ON_CHANGE": False,
        "MAKE_SNAPSHOT_ZIP": True,
        "SNAPSHOT_SQLITE_ENABLED": False,
        "SNAPSHOT_SQLITE_PATH": str(root / "data" / "sqlite3_snapshots.db"),
        "FILE_BRAND": "TEST",
        "DEAL_COUNTRY_BY_CATEGORY": {
            0: "RB",
            34: "RB",
            36: "RB",
            6: "RB",
            28: "RF",
            32: "RF",
            38: "RF",
        },
        "ZIP_NAME_TEMPLATE": "Снимок {stamp}",
        "BASE_DIR": str(root),
        "DEBUG_LOG_PATH": str(root / "debug.log"),
        "ERROR_LOG_PATH": str(root / "error.log"),
    }
    for name, directory in (
        ("DATA_DIR", "data"),
        ("RAW_DIR", "raw"),
        ("EXPORT_DIR", "export"),
        ("AUDIO_DIR", "audio"),
        ("TRANSCRIPTS_DIR", "transcripts"),
    ):
        path = root / directory
        path.mkdir()
        values[name] = str(path)
    cfg.__dict__.update(values)
    return cfg


def fields():
    """Покрыть приоритеты названий, регистры enum и справочники."""
    return {
        "UF_COUNTRY": {
            "listLabel": "Страна",
            "formLabel": "Не выбирать",
            "type": "enumeration",
            "items": [{"ID": "1", "VALUE": "РБ"}],
        },
        "UF_MANAGER": {"formLabel": "Сотрудник", "type": "employee"},
        "UF_TAGS": {
            "filterLabel": "Метки",
            "type": "enumeration",
            "items": {
                "a": {"id": "2", "value": "Второй"},
                "b": {"ID": "3", "VALUE": "Третий"},
                "empty": {"VALUE": "Без ID"},
            },
        },
        "MOVED_BY_ID": {"title": "MOVED_BY_ID", "type": "employee"},
        "SOURCE_ID": {
            "title": "Источник",
            "type": "crm_status",
            "statusType": "SOURCE",
        },
        "CATEGORY_ID": {"title": "Воронка", "type": "crm_category"},
        "OPENED": {"title": "OPENED", "type": "char"},
        "UF_FLAG": {"title": "Флаг", "type": "boolean"},
        "UF_EMPTY": {},
    }


def entity_row(did, group="active", entity="deal"):
    """Сделки и лиды намеренно используют одинаковые ID."""
    row = {
        "ID": str(did),
        "TITLE": f"Пример {did}",
        "ASSIGNED_BY_ID": "42",
        "CONTACT_ID": "5",
        "SOURCE_ID": "WEB",
        "CATEGORY_ID": "0",
        "DATE_CREATE": "2026-11-01T10:00:00+03:00",
        "DATE_MODIFY": "2026-11-22T10:00:00+03:00",
        "CLOSED": "N" if group == "active" else "Y",
        "STAGE_ID": {"active": "NEW", "completed": "WON", "technical": "JUNK"}[group],
        "UF_COUNTRY": "1",
        "UF_MANAGER": "17",
        "UF_TAGS": ["2", "3"],
        "MOVED_BY_ID": "17",
        "OPENED": "Y",
        "UF_FLAG": False,
    }
    if entity == "lead":
        row.update(
            STATUS_ID=row.pop("STAGE_ID"),
            LAST_NAME="Клиентов",
            NAME="Пётр",
            STATUS_SEMANTIC_ID="P" if group == "active" else "S",
        )
    return row


class FakeClient:
    """Строгий синтетический API: неизвестный метод немедленно роняет тест."""

    def __init__(self):
        self.trace = []
        self.rows = {
            e: [
                entity_row(i, g, e)
                for i, g in enumerate(("active", "completed", "technical"), 1)
            ]
            for e in ("deal", "lead")
        }
        self.fail_ids = set()
        self.omit_closed = False
        self.chat_fallback = False
        self.bitrix_error = RuntimeError

    def ping(self):
        return self.result("profile")

    def result(self, method, params=None):
        return self.answer("result", method, params)

    def call_list(self, method, params=None):
        return self.answer("list", method, params)

    def answer(self, kind, method, params):
        params = params or {}
        self.trace.append([kind, method, copy.deepcopy(params)])
        if method == "profile":
            return {"ID": "42", "NAME": "Ответственный"}
        if method == "user.get":
            return [
                {"ID": str(i), "LAST_NAME": name, "NAME": "Иван"}
                for i, name in ((17, "Участников"), (42, "Ответственный"))
            ]
        if method == "crm.category.list":
            return {"categories": [{"id": "0", "name": "Основная"}]}
        if method == "crm.status.list":
            book = params["filter"]["ENTITY_ID"]
            if book in ("DEAL_STAGE", "STATUS"):
                return [
                    {"STATUS_ID": k, "NAME": v}
                    for k, v in (
                        ("NEW", "Новая"),
                        ("WON", "Успешно"),
                        ("JUNK", "Дубль"),
                    )
                ]
            return [{"STATUS_ID": "WEB", "NAME": "Сайт"}]
        if method == "crm.contact.get":
            return {"LAST_NAME": "Клиентов", "NAME": "Пётр"}
        if method in ("crm.deal.fields", "crm.lead.fields"):
            return copy.deepcopy(fields())
        if method in ("crm.deal.list", "crm.lead.list", "crm.deal.get", "crm.lead.get"):
            entity = method.split(".")[1]
            rows = copy.deepcopy(self.rows[entity])
            flt = params.get("filter", {})
            if method.endswith(".get"):
                return next(
                    (r for r in rows if int(r["ID"]) == int(params["id"])), None
                )
            if "@ID" in flt:
                rows = [r for r in rows if int(r["ID"]) in flt["@ID"]]
            if "CLOSED" in flt:
                rows = [r for r in rows if r["CLOSED"] == flt["CLOSED"]]
            if "STATUS_SEMANTIC_ID" in flt:
                rows = [
                    r
                    for r in rows
                    if r["STATUS_SEMANTIC_ID"] == flt["STATUS_SEMANTIC_ID"]
                ]
            if ">=DATE_MODIFY" in flt and self.omit_closed:
                rows = [r for r in rows if r["CLOSED"] == "N"]
            return rows
        if method == "crm.stagehistory.list":
            if params["filter"]["OWNER_ID"] in self.fail_ids:
                raise self.bitrix_error(method, "SYNTHETIC", "Искусственная ошибка")
            return [{"STAGE_ID": "NEW", "CREATED_TIME": "2026-11-01T10:00:00+03:00"}]
        if method == "crm.activity.list":
            if params["filter"].get("TYPE_ID") == 2:
                return [
                    {
                        "ID": "91",
                        "RESPONSIBLE_ID": "17",
                        "AUTHOR_ID": "42",
                        "DIRECTION": "1",
                        "START_TIME": "2026-11-02T11:00:00+03:00",
                        "END_TIME": "2026-11-02T11:02:00+03:00",
                        "FILES": [{"id": "501"}],
                        "SUBJECT": "Звонок +375 29 1234567",
                        "DESCRIPTION": "Разговор",
                    }
                ]
            return [
                {
                    "ID": str(100 + t),
                    "TYPE_ID": str(t),
                    "AUTHOR_ID": "17",
                    "DIRECTION": "2",
                    "CREATED": "2026-11-02T12:00:00+03:00",
                    "SUBJECT": "Тема",
                    "DESCRIPTION": "Описание",
                }
                for t in params["filter"]["@TYPE_ID"]
            ]
        if method == "crm.timeline.comment.list":
            return [
                {
                    "ID": "71",
                    "AUTHOR_ID": "17",
                    "CREATED": "2026-11-02T13:00:00+03:00",
                    "COMMENT": "Комментарий",
                }
            ]
        if method == "tasks.task.list":
            return [
                {
                    "id": "81",
                    "responsibleId": "17",
                    "createdBy": "42",
                    "status": "5",
                    "createdDate": "2026-11-02T14:00:00+03:00",
                    "title": "Проверить заказ",
                }
            ]
        if method == "imopenlines.crm.chat.get":
            return [{"CHAT_ID": "61", "CONNECTOR_ID": "telegrambot"}, {}]
        if method == "imopenlines.session.history.get":
            if self.chat_fallback:
                raise self.bitrix_error(method, "DENIED", "Проверка запасного пути")
            return {
                "message": {
                    "1": {
                        "id": "1",
                        "senderid": "17",
                        "date": ISO,
                        "text": "[b]Сообщение[/b]",
                    },
                    "2": {
                        "id": "2",
                        "senderid": "0",
                        "date": ISO,
                        "text": "Создан новый лид",
                    },
                },
                "users": {},
            }
        if method == "im.dialog.messages.get":
            return {
                "messages": [
                    {"id": "2", "author_id": "0", "date": ISO, "text": "Ответ клиента"}
                ],
                "users": [],
            }
        raise AssertionError(f"Неожиданный метод {method}: {params}")


class ExportTests(unittest.TestCase):
    """Проверить реальные сборку, перенос и служебные команды без живых данных."""

    def setUp(self):
        global SANDBOX
        self.temp = tempfile.TemporaryDirectory(prefix="bitrix-tests-")
        self.root = Path(self.temp.name).resolve()
        SANDBOX = self.root
        self.addCleanup(self.close_sandbox)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = make_config(self.root)
        self.stack.enter_context(patch.dict(sys.modules))
        for name in PROJECT_MODULES:
            sys.modules.pop(name, None)
        sys.modules["config"] = self.cfg
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        logger.remove()
        self.addCleanup(logger.remove)
        root_logger = logging.getLogger()
        self.stack.callback(setattr, root_logger, "handlers", root_logger.handlers[:])
        self.stack.callback(root_logger.setLevel, root_logger.level)
        self.sync = importlib.import_module("sync")
        self.client = FakeClient()
        self.client.bitrix_error = self.sync.BitrixError
        self.stack.enter_context(
            patch.object(self.sync, "B24Client", return_value=self.client)
        )
        self.stack.enter_context(
            patch.object(self.sync.export_llm, "datetime", FixedDatetime)
        )
        self.stack.enter_context(
            patch.object(
                self.sync.export_llm, "_tz", return_value=timezone(timedelta(hours=3))
            )
        )
        self.stack.enter_context(
            patch.object(
                self.sync,
                "time",
                types.SimpleNamespace(
                    strftime=Mock(return_value=STAMP), time=time.time
                ),
            )
        )
        self.stack.enter_context(patch("time.sleep"))

    def close_sandbox(self):
        global SANDBOX
        try:
            self.temp.cleanup()
        finally:
            SANDBOX = None

    def run_sync(self):
        with (
            patch.object(sys, "argv", ["sync.py"]),
            patch.object(
                self.sync.transcribe,
                "_transcribe_assemblyai_direct",
                return_value="Здравствуйте, синтетический разговор.",
            ),
        ):
            self.sync.main()
        return self.read_json("export/snapshot/manifest.json")

    def test_service_scripts_have_independent_entrypoints(self):
        client = Mock()
        client.call.return_value = {"total": 0}
        client.result.return_value = {}
        client.call_list.return_value = []
        for name in (
            "diag_fields",
            "diag_filter",
            "recover_registry",
            "refresh_names",
            "import_snapshot",
            "source_sqlite",
            "snapshot_sqlite",
        ):
            path = ROOT / "scripts" / f"{name}.py"
            arguments = [str(path)]
            if name in {
                "refresh_names",
                "import_snapshot",
                "source_sqlite",
                "snapshot_sqlite",
            }:
                arguments.append("--help")
            with (
                self.subTest(script=name),
                patch.object(sys, "argv", arguments),
                patch("api_client.B24Client", return_value=client),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as result,
            ):
                runpy.run_path(str(path), run_name="__main__")
            self.assertIn(result.exception.code, (None, 0))
        self.assertEqual(
            self.read_json("data/diag_fields.json")["deal_userfield_list"]["count"], 0
        )

    def read_json(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def write_json(self, relative, value):
        path = self.root / relative
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def digest(self):
        """Сравнить все артефакты, распакованный ZIP и трассу API, исключив корень tmp."""
        artifacts = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if path.suffix == ".zip":
                with ZipFile(path) as archive:
                    artifacts[rel] = {
                        name: archive.read(name).decode("utf-8").replace("\r\n", "\n")
                        for name in sorted(archive.namelist())
                    }
            else:
                artifacts[rel] = path.read_text(encoding="utf-8")
        value = json.dumps(
            [artifacts, self.client.trace], ensure_ascii=False, sort_keys=True
        )
        value = value.replace(str(self.root), "<TMP>")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def test_isolation(self):
        self.assertIs(sys.modules["config"], self.cfg)
        with self.assertRaisesRegex(RuntimeError, "Изоляция"):
            (ROOT / ".env").read_text(encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Изоляция"):
            (self.root.parent / "bitrix-forbidden-write").write_text(
                "no", encoding="utf-8"
            )
        with socket.socket() as sock, self.assertRaisesRegex(RuntimeError, "Изоляция"):
            sock.connect(("127.0.0.1", 9))

    def test_full_snapshot(self):
        manifest = self.run_sync()
        self.assertEqual(manifest["update_mode"], "FULL")
        self.assertEqual(manifest["export_type"], "FULL_SNAPSHOT")
        self.assertEqual(manifest["snapshot_at"], ISO)
        self.assertTrue(manifest["is_complete"])
        self.assertEqual(manifest["countries"], ["RB", "UNKNOWN"])
        for entity in ("deals", "leads"):
            self.assertEqual(manifest[entity]["total"], 3)
            self.assertEqual(
                [
                    manifest[entity]["groups"][g]["ids"]
                    for g in ("active", "completed", "technical")
                ],
                [[1], [2], [3]],
            )
        for entity in ("deal", "lead"):
            tps = self.sync.store.load_tps(1, entity=entity)
            self.assertEqual(
                {t["event_type"] for t in tps},
                {
                    "stage_change",
                    "call",
                    "message",
                    "comment",
                    "task",
                    "email",
                    "meeting",
                },
            )
            call = next(t for t in tps if t["event_type"] == "call")
            self.assertEqual(call["actor"]["user_id"], 17)
            self.assertEqual(call["responsible_user_id"], 42)
            self.assertEqual(call["deal_stage_at_time"]["stage_id"], "NEW")
            self.assertEqual(call["content"]["transcript_status"], "assemblyai")
            self.assertEqual(len([t for t in tps if t["event_type"] == "message"]), 1)
            md = self.sync.store.load_md(1, entity=entity)
            country = "RB" if entity == "deal" else "UNKNOWN"
            self.assertIn(f"- Страна: {country}\n", md.split("## Лента касаний")[0])
            self.assertIn("- Страна: РБ\n", md.split("## Поля ")[1])
            self.assertIn("Участников И.", md)
            self.assertIn("Ответственный И.", md)
            self.assertIn("Второй, Третий", md)
            self.assertIn("Здравствуйте, синтетический разговор.", md)
        self.assertEqual(
            set(self.read_json("data/registry.json")),
            {"1", "2", "3", "lead:1", "lead:2", "lead:3"},
        )
        archives = list((self.root / "export").glob("*.zip"))
        self.assertEqual(len(archives), 1)
        with ZipFile(archives[0]) as archive:
            self.assertEqual(
                len([n for n in archive.namelist() if n.endswith(".md")]), 6
            )
            self.assertIn("manifest.json", archive.namelist())
            self.assertFalse(any(n.endswith(".jsonl") for n in archive.namelist()))
        # Имена и export_id используют штатные форматы времени вместо общего STAMP.
        self.assertEqual(
            self.digest(),
            "b84d51594c2e7a6dbdf2d87965403006226333871832ede4ba1a3ab4fcdd3e24",
        )

    def test_snapshot_sqlite_imports_final_zip(self):
        self.cfg.SNAPSHOT_SQLITE_ENABLED = True
        manifest = self.run_sync()
        with closing(sqlite3.connect(self.cfg.SNAPSHOT_SQLITE_PATH)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM snapshot_cards").fetchone(),
                (6,),
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            with ZipFile(next((self.root / "export").glob("*.zip"))) as archive:
                for name in archive.namelist():
                    entity, entity_id, _ = self.sync.snapshot_sqlite.file_identity(name)
                    if entity:
                        restored = self.sync.snapshot_sqlite.restore_card(
                            connection, manifest["export_id"], entity, entity_id
                        )
                        self.assertEqual(restored.encode("utf-8"), archive.read(name))
        self.assertEqual(self.read_json("data/progress.json")["sqlite_status"], "added")

    def test_snapshot_sqlite_failure_preserves_zip_and_state(self):
        self.cfg.SNAPSHOT_SQLITE_ENABLED = True
        with (
            closing(sqlite3.connect(self.cfg.SNAPSHOT_SQLITE_PATH)) as connection,
            connection,
        ):
            connection.execute("CREATE TABLE keep (value TEXT)")
        before = Path(self.cfg.SNAPSHOT_SQLITE_PATH).read_bytes()
        self.run_sync()
        self.assertTrue(list((self.root / "export").glob("*.zip")))
        self.assertTrue((self.root / "data" / "state.json").exists())
        self.assertEqual(self.read_json("data/progress.json")["sqlite_status"], "error")
        self.assertEqual(Path(self.cfg.SNAPSHOT_SQLITE_PATH).read_bytes(), before)

    def test_incremental_snapshot(self):
        self.run_sync()
        self.client.omit_closed = True
        self.client.trace.clear()
        manifest = self.run_sync()
        self.assertEqual(manifest["countries"], ["RB", "UNKNOWN"])
        self.assertEqual(manifest["update_mode"], "INCREMENTAL")
        self.assertEqual(manifest["overlap_from"], "2026-11-22T12:00:00+03:00")
        for ent in ("deals", "leads"):
            self.assertEqual(manifest[ent]["total"], 3)
            self.assertEqual(manifest[ent]["groups"]["completed"]["reused"], 1)
            self.assertEqual(manifest[ent]["groups"]["technical"]["reused"], 1)
        calls = [
            p for _, method, p in self.client.trace if method == "crm.activity.list"
        ]
        self.assertTrue(calls)
        self.assertTrue(all(p["filter"]["OWNER_ID"] == 1 for p in calls))
        self.assertEqual(
            self.digest(),
            "4d58c8d153b3805f24eade12f01238e5fe45c94be788eb6af05d652d6fe565f4",
        )

    def test_country_uses_category_or_source_and_preserves_crm_fields(self):
        export = self.sync.export_llm
        for category, expected in (
            (0, "RB"),
            (34, "RB"),
            (36, "RB"),
            (6, "RB"),
            (28, "RF"),
            (32, "RF"),
            (38, "RF"),
        ):
            for value in (category, str(category)):
                record = {
                    "ID": "1",
                    "CATEGORY_ID": value,
                    "_source": "Россия ON",
                    "_country": "OLD",
                    "UF_COUNTRY": "1",
                    "_fields": {"UF_COUNTRY": {"title": "Страна", "value": "РБ"}},
                }
                before = copy.deepcopy(record)
                with self.subTest(category=value):
                    self.assertEqual(export.entity_country(record), expected)
                    self.assertEqual(record, before)
        for value in (None, "", "bad", 22, 30, 999):
            with self.subTest(unknown_category=value):
                self.assertEqual(
                    export.entity_country(
                        {
                            "CATEGORY_ID": value,
                            "_source": "РБ ON",
                            "_fields": {
                                "UF_COUNTRY": {"title": "Страна", "value": "РБ"}
                            },
                        }
                    ),
                    "UNKNOWN",
                )
        for source, expected in (
            ("РБ ON, Сайт", "RB"),
            (" Россия OFF ", "RF"),
            ("рф ON", "RF"),
            ("РБреклама", "UNKNOWN"),
            ("Сайт Россия", "UNKNOWN"),
            (None, "UNKNOWN"),
        ):
            record = {"_source": source, "CATEGORY_ID": "28", "_country": "OLD"}
            before = copy.deepcopy(record)
            with self.subTest(source=source):
                self.assertEqual(export.entity_country(record, "lead"), expected)
                self.assertEqual(record, before)
        with self.assertRaises(ValueError):
            export.entity_country({}, "contact")

    def test_country_full_export_does_not_fill_empty_crm_country(self):
        for entity in ("deal", "lead"):
            self.client.rows[entity][0]["CATEGORY_ID"] = "28"
            self.client.rows[entity][0]["SOURCE_ID"] = "Россия ON, Сайт"
            self.client.rows[entity][1]["CATEGORY_ID"] = "6"
            self.client.rows[entity][1]["SOURCE_ID"] = "РБ ON, Сайт"
            self.client.rows[entity][1]["UF_COUNTRY"] = ""
        manifest = self.run_sync()
        self.assertEqual(manifest["countries"], ["RB", "RF", "UNKNOWN"])
        for entity in ("deal", "lead"):
            for did, expected in ((1, "RF"), (2, "RB")):
                record = self.sync.store.load_deal(did, entity=entity)
                self.assertEqual(record["_country"], expected)
                self.assertEqual(
                    self.sync.store.load_stats(did, entity=entity)["country"], expected
                )
                md = self.sync.store.load_md(did, entity=entity)
                self.assertIn(
                    f"- Страна: {expected}\n", md.split("## Лента касаний")[0]
                )
                fields_text = md.split("## Поля ")[1]
                if did == 1:
                    self.assertEqual(record["UF_COUNTRY"], "1")
                    self.assertEqual(record["_fields"]["UF_COUNTRY"]["value"], "РБ")
                    self.assertIn("- Страна: РБ\n", fields_text)
                else:
                    self.assertEqual(record["UF_COUNTRY"], "")
                    self.assertNotIn("UF_COUNTRY", record["_fields"])
                    self.assertNotIn("- Страна:", fields_text)
                filename = self.sync.export_llm.card_filename(
                    record, self.sync.export_llm.ExportContext(), entity=entity
                )
                self.assertIn(f"_{expected}_", filename)
                self.assertTrue(list((self.root / "export/snapshot").rglob(filename)))

    def test_reused_country_is_recalculated_without_changing_store(self):
        for entity in ("deal", "lead"):
            self.client.rows[entity][1]["CATEGORY_ID"] = "28"
            self.client.rows[entity][1]["SOURCE_ID"] = "Россия ON, Сайт"
        self.run_sync()
        originals = {}
        for entity in ("deal", "lead"):
            record = self.sync.store.load_deal(2, entity=entity)
            record["_country"] = "RB"
            md = self.sync.store.load_md(2, entity=entity).replace(
                "- Страна: RF\n", "- Страна: RB\n", 1
            )
            stats = self.sync.store.load_stats(2, entity=entity)
            stats["country"] = "RB"
            self.sync.store.save_artifacts(
                record,
                md,
                self.sync.store.load_tps(2, entity=entity),
                stats,
                entity=entity,
            )
            originals[entity] = md
        store_before = {
            p: p.read_bytes() for p in (self.root / "data/store").rglob("2.*")
        }
        for omit in (False, True):
            self.client.omit_closed = omit
            self.client.trace.clear()
            manifest = self.run_sync()
            self.assertTrue(manifest["is_complete"])
            for entity in ("deal", "lead"):
                ent_dir = self.sync.export_llm.ENTITY_DIR[entity]
                cards = list(
                    (self.root / "export/snapshot" / ent_dir).rglob("*_2_SNAP_*.md")
                )
                self.assertEqual(len(cards), 1)
                self.assertIn("_RF_", cards[0].name)
                self.assertEqual(
                    cards[0].read_text(encoding="utf-8"),
                    originals[entity].replace("- Страна: RB\n", "- Страна: RF\n", 1),
                )
                index = self.root / f"export/index_{entity}_completed.jsonl"
                self.assertEqual(
                    json.loads(index.read_text(encoding="utf-8"))["country"], "RF"
                )
            for path, content in store_before.items():
                self.assertEqual(path.read_bytes(), content)
            calls = [
                p for _, method, p in self.client.trace if method == "crm.activity.list"
            ]
            self.assertTrue(all(p["filter"]["OWNER_ID"] == 1 for p in calls))

    def test_country_header_patch_preserves_body_and_line_endings(self):
        export = self.sync.export_llm
        for newline in ("\n", "\r\n"):
            for has_country in (False, True):
                for anchor in ("Текущая стадия", "Текущий статус", "Источник"):
                    header = f"# Карточка\n\n- {anchor}: Пример\n"
                    old = header + ("- Страна: RB\n" if has_country else "")
                    body = "- Создана: дата\n\n## Лента касаний\n- Страна: Текст сообщения\n\n## Поля сделки\n- Страна: РБ\n"
                    original = (old + body).replace("\n", newline)
                    expected = (header + "- Страна: RF\n" + body).replace("\n", newline)
                    self.assertEqual(
                        export.update_card_country(original, "RF"), expected
                    )

    def test_refresh_names_recalculates_country(self):
        for entity in ("deal", "lead"):
            self.client.rows[entity][0]["CATEGORY_ID"] = "28"
            self.client.rows[entity][0]["SOURCE_ID"] = "Россия ON, Сайт"
        self.run_sync()
        for entity in ("deal", "lead"):
            record = self.sync.store.load_deal(1, entity=entity)
            record["_country"] = "RB"
            stats = self.sync.store.load_stats(1, entity=entity)
            stats["country"] = "RB"
            self.sync.store.save_artifacts(
                record,
                self.sync.store.load_md(1, entity=entity),
                self.sync.store.load_tps(1, entity=entity),
                stats,
                entity=entity,
            )
        refresh = load_script("refresh_names")
        self.client.trace.clear()
        with patch.object(sys, "argv", ["scripts/refresh_names.py", "--offline"]):
            refresh.main()
        self.assertFalse(self.client.trace)
        for entity in ("deal", "lead"):
            record = self.sync.store.load_deal(1, entity=entity)
            self.assertEqual(record["_country"], "RF")
            self.assertEqual(record["_fields"]["UF_COUNTRY"]["value"], "РБ")
            self.assertEqual(
                self.sync.store.load_stats(1, entity=entity)["country"], "RF"
            )
            md = self.sync.store.load_md(1, entity=entity)
            self.assertIn("- Страна: RF\n", md.split("## Лента касаний")[0])
            self.assertIn("- Страна: РБ\n", md.split("## Поля ")[1])

    def test_reused_lead_keeps_cached_source_if_dictionary_is_unavailable(self):
        self.run_sync()
        record = self.sync.store.load_deal(2, entity="lead")
        record["_source"] = "Россия ON, Сайт"
        self.sync.store.save_artifacts(
            record,
            self.sync.store.load_md(2, entity="lead"),
            self.sync.store.load_tps(2, entity="lead"),
            self.sync.store.load_stats(2, entity="lead"),
            entity="lead",
        )
        answer = self.client.answer

        def without_sources(kind, method, params):
            if (
                method == "crm.status.list"
                and params.get("filter", {}).get("ENTITY_ID") == "SOURCE"
            ):
                return []
            return answer(kind, method, params)

        with patch.object(self.client, "answer", side_effect=without_sources):
            manifest = self.run_sync()
        self.assertTrue(manifest["is_complete"])
        path = next(
            (self.root / "export/snapshot/Лиды/Завершенные").glob("*_2_SNAP_*.md")
        )
        self.assertIn("_RF_", path.name)
        self.assertIn(
            "- Страна: RF\n",
            path.read_text(encoding="utf-8").split("## Лента касаний")[0],
        )

    def test_country_copy_survives_failed_atomic_replace(self):
        export = self.sync.export_llm
        record = {"ID": "1", "CATEGORY_ID": "28", "_country": "RF"}
        md = "# Карточка\n\n- Страна: RB\n\n## Поля сделки\n- Страна: РБ\n"
        ctx = export.ExportContext()
        path = Path(export.write_deal_markdown_copy(md, record, "completed", ctx, {}))
        before = path.read_bytes()
        with (
            patch("os.replace", side_effect=OSError("Замена недоступна")),
            self.assertRaises(OSError),
        ):
            export.write_deal_markdown_copy(md, record, "completed", ctx, {})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_reuse_requires_matching_schema_and_all_artifacts(self):
        self.run_sync()
        reg = self.read_json("data/registry.json")
        reg["2"]["schema_version"] = 1
        self.write_json("data/registry.json", reg)
        (self.root / "data/store/lead/tps/2.jsonl").unlink()
        self.client.trace.clear()
        manifest = self.run_sync()
        for ent in ("deals", "leads"):
            self.assertEqual(manifest[ent]["groups"]["completed"]["reused"], 0)
            self.assertEqual(manifest[ent]["groups"]["technical"]["reused"], 1)
        owners = {
            p["filter"]["OWNER_ID"]
            for _, m, p in self.client.trace
            if m == "crm.activity.list"
        }
        self.assertEqual(owners, {1, 2})

    def test_changed_closed_reexport_policy(self):
        self.run_sync()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                for ent in ("deal", "lead"):
                    self.client.rows[ent][1]["DATE_MODIFY"] = (
                        "2026-11-23T12:01:00+03:00" if enabled else ISO
                    )
                self.cfg.REEXPORT_CLOSED_ON_CHANGE = enabled
                manifest = self.run_sync()
                for ent in ("deals", "leads"):
                    self.assertEqual(
                        manifest[ent]["groups"]["completed"]["reused"],
                        0 if enabled else 1,
                    )

    def test_entity_error_continues_and_is_retried(self):
        self.client.fail_ids = {2}
        manifest = self.run_sync()
        self.assertFalse(manifest["is_complete"])
        self.assertEqual(manifest["errors_count"], 2)
        self.assertEqual(manifest["deals"]["groups"]["technical"]["ids"], [3])
        self.assertEqual(
            self.read_json("data/state.json")["errors"], {"deals": [2], "leads": [2]}
        )
        self.client.fail_ids.clear()
        self.client.omit_closed = True
        self.client.trace.clear()
        manifest = self.run_sync()
        self.assertTrue(manifest["is_complete"])
        retries = [
            m for _, m, p in self.client.trace if p.get("filter", {}).get("@ID") == [2]
        ]
        self.assertEqual(retries, ["crm.deal.list", "crm.lead.list"])

    def test_arguments_are_rejected_before_api_or_export(self):
        for arguments in (
            ["--limit", "1"],
            ["--lead-limit", "1"],
            ["--deals", "1"],
            ["--leads", "1"],
            ["--no-transcribe"],
            ["--skip-transcribe"],
            ["--help"],
            ["unexpected"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", ["sync.py", *arguments]),
                self.assertRaisesRegex(SystemExit, "не принимает аргументы"),
            ):
                self.sync.main()
        self.sync.B24Client.assert_not_called()
        self.assertEqual(self.cfg.DEAL_IDS, [])
        self.assertEqual(self.cfg.LEAD_IDS, [])
        self.assertEqual(self.cfg.DEALS_LIMIT, 0)
        self.assertEqual(self.cfg.LEADS_LIMIT, 0)
        self.assertFalse(list(self.root.rglob("*.json")))

    def test_config_limits_do_not_change_state_or_registry(self):
        self.run_sync()
        before = {
            p: (self.root / p).read_bytes()
            for p in ("data/state.json", "data/registry.json")
        }
        self.cfg.DEALS_LIMIT = self.cfg.LEADS_LIMIT = 1
        self.cfg.TRANSCRIBE_ENABLED = False
        manifest = self.run_sync()
        self.assertEqual(manifest["update_mode"], "FULL")
        self.assertEqual(manifest["deals"]["total"], 1)
        self.assertEqual(manifest["leads"]["total"], 1)
        for path, content in before.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        tps = self.sync.store.load_tps(1)
        self.assertEqual(
            next(t for t in tps if t["event_type"] == "call")["content"][
                "transcript_status"
            ],
            "pending",
        )

    def test_deal_ids_skip_leads_without_explicit_lead_limit(self):
        self.cfg.DEAL_IDS = [1]
        self.cfg.TRANSCRIBE_ENABLED = False
        manifest = self.run_sync()
        self.assertEqual(manifest["deals"]["total"], 1)
        self.assertEqual(manifest["leads"]["total"], 0)
        self.assertFalse((self.root / "data/state.json").exists())
        self.assertFalse((self.root / "data/registry.json").exists())

    def test_config_lead_ids_enable_leads_in_limited_deal_run(self):
        self.cfg.DEAL_IDS = [1]
        self.cfg.LEAD_IDS = [2]
        manifest = self.run_sync()
        self.assertEqual(manifest["update_mode"], "FULL")
        self.assertEqual(manifest["deals"]["total"], 1)
        self.assertEqual(manifest["leads"]["total"], 1)
        self.assertTrue(self.sync.store.has_deal(2, entity="lead"))
        self.assertFalse((self.root / "data/state.json").exists())
        self.assertFalse((self.root / "data/registry.json").exists())

    def test_config_disables_transcription(self):
        self.cfg.DEAL_IDS = [1]
        self.cfg.TRANSCRIBE_ENABLED = False
        with patch.object(self.sync.transcribe, "transcribe_call") as external:
            self.run_sync()
            call = next(
                t for t in self.sync.store.load_tps(1) if t["event_type"] == "call"
            )
            self.assertEqual(call["content"]["transcript_status"], "pending")
            self.assertEqual(call["content"]["text"], "")
            external.assert_not_called()

    def test_disabled_groups_and_sources(self):
        self.cfg.PROCESS_TECHNICAL = False
        self.cfg.INCLUDE_COMMENTS = self.cfg.INCLUDE_TASKS = False
        self.cfg.INCLUDE_EMAILS = self.cfg.INCLUDE_MEETINGS = False
        manifest = self.run_sync()
        self.assertEqual(len(manifest["skipped"]), 2)
        for ent in ("deals", "leads"):
            self.assertEqual(manifest[ent]["total"], 2)
            self.assertEqual(manifest[ent]["groups"]["technical"]["ids"], [])
        self.assertEqual(
            {t["event_type"] for t in self.sync.store.load_tps(1)},
            {"stage_change", "call", "message"},
        )
        self.assertFalse(
            any(
                m in ("tasks.task.list", "crm.timeline.comment.list")
                for _, m, _ in self.client.trace
            )
        )

    def test_explicit_incremental_without_state_falls_back_to_full(self):
        self.cfg.EXPORT_TYPE = "incremental"
        self.assertEqual(self.run_sync()["update_mode"], "FULL")

    def test_chat_fallback_and_empty_group_directories(self):
        self.client.chat_fallback = True
        self.cfg.DEALS_LIMIT = self.cfg.LEADS_LIMIT = 1
        self.run_sync()
        md = self.sync.store.load_md(1)
        self.assertIn("Ответ клиента", md)
        with ZipFile(next((self.root / "export").glob("*.zip"))) as archive:
            for ent in ("Сделки", "Лиды"):
                self.assertIn(f"{ent}/Завершенные/", archive.namelist())
                self.assertIn(f"{ent}/Технические/", archive.namelist())

    def test_metadata_expected_values(self):
        expected = {
            "UF_COUNTRY": {
                "title": "Страна",
                "type": "enumeration",
                "list": {"1": "РБ"},
            },
            "UF_MANAGER": {"title": "Сотрудник", "type": "employee"},
            "UF_TAGS": {
                "title": "Метки",
                "type": "enumeration",
                "list": {"2": "Второй", "3": "Третий"},
            },
            "MOVED_BY_ID": {"title": "Кем передвинута", "type": "employee"},
            "SOURCE_ID": {
                "title": "Источник",
                "type": "crm_status",
                "statusType": "SOURCE",
            },
            "CATEGORY_ID": {"title": "Воронка", "type": "crm_category"},
            "OPENED": {"title": "Доступен для всех", "type": "char"},
            "UF_FLAG": {"title": "Флаг", "type": "boolean"},
            "UF_EMPTY": {"title": "UF_EMPTY", "type": ""},
        }
        for ent, loader in (
            ("deal", self.sync.fetch_deals.load_deal_fields_meta),
            ("lead", self.sync.fetch_leads.load_lead_fields_meta),
        ):
            with self.subTest(entity=ent):
                self.assertEqual(loader(self.client), expected)
                self.assertEqual(
                    self.read_json(f"data/{ent}_fields_meta.json"), expected
                )

    def test_metadata_cache_survives_failed_update(self):
        for ent, loader in (
            ("deal", self.sync.fetch_deals.load_deal_fields_meta),
            ("lead", self.sync.fetch_leads.load_lead_fields_meta),
        ):
            with self.subTest(entity=ent):
                loader(self.client)
                path = self.root / f"data/{ent}_fields_meta.json"
                before = path.read_bytes()
                # Ошибка после начала JSON-записи не должна обнулить старый кэш.
                invalid = {"UF_BAD": {"items": [{"ID": "1", "VALUE": object()}]}}
                with (
                    patch.object(self.client, "result", return_value=invalid),
                    self.assertRaises(TypeError),
                ):
                    loader(self.client)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_suffix(".json.tmp").exists())
                with (
                    patch("os.replace", side_effect=OSError("Замена недоступна")),
                    self.assertRaisesRegex(OSError, "Замена недоступна"),
                ):
                    loader(self.client)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_full_does_not_sweep_old_closed_from_registry(self):
        self.run_sync()
        self.cfg.EXPORT_TYPE = "full"
        self.client.rows = {ent: rows[:1] for ent, rows in self.client.rows.items()}
        manifest = self.run_sync()
        self.assertEqual(manifest["update_mode"], "FULL")
        for ent in ("deals", "leads"):
            self.assertEqual(manifest[ent]["total"], 1)
            self.assertEqual(manifest[ent]["groups"]["completed"]["ids"], [])
        self.assertTrue(self.sync.store.has_deal(2))
        self.assertTrue(self.sync.store.has_deal(2, entity="lead"))

    def test_empty_export_does_not_create_zip(self):
        self.client.rows = {"deal": [], "lead": []}
        manifest = self.run_sync()
        self.assertEqual(manifest["deals"]["total"], 0)
        self.assertEqual(manifest["leads"]["total"], 0)
        self.assertFalse(list((self.root / "export").glob("*.zip")))
        self.assertIsNone(self.read_json("data/progress.json")["zip"])
        for ent in ("Сделки", "Лиды"):
            for group in ("Активные", "Завершенные", "Технические"):
                self.assertTrue((self.root / "export/snapshot" / ent / group).is_dir())

    def test_transcript_missing_recording_disabled_and_failed(self):
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, {}),
            {"text": None, "status": "no_recording"},
        )
        call = {"activity_id": "91", "file_ids": [501]}
        self.cfg.TRANSCRIBE_ENABLED = False
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, call)["status"],
            "pending",
        )
        self.cfg.TRANSCRIBE_ENABLED = True
        with patch.object(self.sync.transcribe, "transcribe_call", return_value=None):
            self.assertEqual(
                self.sync.resolve_transcript(self.client, 1, call)["status"],
                "no_transcript",
            )

    def test_refresh_names_offline_and_recovery(self):
        self.run_sync()
        before = {
            p: (self.root / p).read_bytes()
            for p in ("data/state.json", "data/registry.json")
        }
        refresh = load_script("refresh_names")
        self.client.trace.clear()
        with patch.object(sys, "argv", ["scripts/refresh_names.py", "--offline"]):
            refresh.main()
        self.assertFalse(self.client.trace)
        for path, content in before.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        self.assertEqual(len(list((self.root / "export/snapshot").rglob("*.md"))), 6)
        self.assertIn("Участников И.", self.sync.store.load_md(1, entity="lead"))
        reg = self.read_json("data/registry.json")
        del reg["lead:2"]
        self.write_json("data/registry.json", reg)
        recovery = load_script("recover_registry")
        with patch("builtins.print"):
            recovery.main()
        recovered = self.read_json("data/registry.json")
        self.assertEqual(set(recovered), set(json.loads(before["data/registry.json"])))
        self.assertEqual(recovered["lead:2"]["exported_at"], "RECOVERED_FROM_STORE")

    def test_transcript_settings_cache_and_assemblyai(self):
        call = {"activity_id": "91", "file_ids": [501]}
        self.cfg.TRANSCRIBE_ENABLED = False
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, call)["status"],
            "pending",
        )
        self.cfg.TRANSCRIBE_ENABLED = True
        self.cfg.ASSEMBLYAI_DIRECT_URL = False
        audio = self.root / "audio/synthetic.mp3"
        audio.write_bytes(b"synthetic")
        call["local_audio"] = str(audio)
        with patch.object(
            self.sync.transcribe, "_transcribe_assemblyai", return_value="Текст"
        ) as mocked:
            self.assertEqual(
                self.sync.resolve_transcript(self.client, 1, call),
                {"text": "Текст", "status": "assemblyai"},
            )
            self.sync.resolve_transcript(self.client, 1, call)
            mocked.assert_called_once()
        self.assertFalse(self.client.trace)

    def test_direct_transcript_falls_back_to_file_upload(self):
        call = {"activity_id": "92", "file_ids": [501]}
        audio = self.root / "audio/synthetic.mp3"
        audio.write_bytes(b"synthetic")
        with (
            patch.object(
                self.sync.transcribe,
                "_transcribe_assemblyai_direct",
                side_effect=RuntimeError("Недоступно"),
            ),
            patch.object(
                self.sync.transcribe, "download_call_recording", return_value=str(audio)
            ) as download,
            patch.object(
                self.sync.transcribe,
                "_transcribe_assemblyai",
                return_value="Запасной путь",
            ) as upload,
        ):
            path = self.sync.transcribe.transcribe_call(self.client, 1, call)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "Запасной путь")
            download.assert_called_once()
            upload.assert_called_once_with(str(audio))

    def test_download_url_is_used_without_auth_changes(self):
        client = Mock()
        client.result.return_value = {
            "NAME": "record.mp3",
            "DOWNLOAD_URL": "https://example.invalid/audio",
        }
        with patch.object(
            self.sync.transcribe, "download_recording", return_value="audio"
        ) as download:
            self.assertEqual(
                self.sync.transcribe.download_call_recording(
                    client, 1, {"activity_id": "91", "file_ids": [501]}
                ),
                "audio",
            )
            self.assertEqual(
                download.call_args.args[0], "https://example.invalid/audio"
            )
        with (
            patch.object(
                self.sync.transcribe,
                "get_disk_file",
                return_value=client.result.return_value,
            ),
            patch.object(
                self.sync.transcribe, "_transcribe_assemblyai_url", return_value="Текст"
            ) as direct,
        ):
            self.assertEqual(
                self.sync.transcribe._transcribe_assemblyai_direct(
                    client, {"file_ids": [501]}
                ),
                "Текст",
            )
            direct.assert_called_once_with("https://example.invalid/audio")

    def test_download_closes_response_and_reuses_complete_audio(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = iter([b"ID3", b"synthetic"])
        destination = str(self.root / "audio" / "record.mp3")
        with patch.object(
            self.sync.transcribe.requests, "get", return_value=response
        ) as get:
            self.assertEqual(
                self.sync.transcribe.download_recording(
                    "https://example.invalid/audio", destination
                ),
                destination,
            )
            self.sync.transcribe.download_recording(
                "https://example.invalid/audio", destination
            )
        self.assertEqual(Path(destination).read_bytes(), b"ID3synthetic")
        self.assertFalse(Path(destination + ".part").exists())
        get.assert_called_once_with(
            "https://example.invalid/audio", timeout=1, stream=True
        )
        response.__exit__.assert_called_once()

    def test_failed_download_removes_partial_audio_and_closes_response(self):
        def chunks():
            yield b"ID3"
            raise requests.ReadTimeout("Синтетический сбой")

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = chunks()
        destination = str(self.root / "audio" / "record.mp3")
        with (
            patch.object(self.sync.transcribe.requests, "get", return_value=response),
            self.assertRaises(requests.ReadTimeout),
        ):
            self.sync.transcribe.download_recording(
                "https://example.invalid/audio", destination
            )
        self.assertFalse(Path(destination).exists())
        self.assertFalse(Path(destination + ".part").exists())
        response.__exit__.assert_called_once()

    def test_failed_touchpoint_write_preserves_previous_jsonl(self):
        store = self.sync.store
        store.save_artifacts({"ID": "1"}, "Карточка", [{"text": "Ранее"}], {})
        path = Path(store._path("tps", 1))
        before = path.read_bytes()
        for failure in ("serialize", "replace"):
            with self.subTest(failure=failure):
                context = (
                    patch.object(store.os, "replace", side_effect=OSError("Недоступно"))
                    if failure == "replace"
                    else ExitStack()
                )
                with context, self.assertRaises((TypeError, OSError)):
                    store.save_artifacts(
                        {"ID": "1"},
                        "Карточка",
                        [{"text": "Новый"}]
                        if failure == "replace"
                        else [{"bad": object()}],
                        {},
                    )
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_api_pagination_and_retries(self):
        api = importlib.import_module("api_client")
        client = api.B24Client()
        self.addCleanup(client.session.close)
        for nested in (None, "items", "tasks"):
            with self.subTest(result=nested):
                page = [{"ID": "1"}]
                with patch.object(
                    client,
                    "call",
                    side_effect=[
                        {"result": {nested: page} if nested else page, "next": 50},
                        {
                            "result": {nested: [{"ID": "2"}]}
                            if nested
                            else [{"ID": "2"}]
                        },
                    ],
                ) as call:
                    self.assertEqual(
                        client.call_list("list"), [{"ID": "1"}, {"ID": "2"}]
                    )
                    self.assertEqual(call.call_count, 2)
        limited = Mock(status_code=429)
        query_limit = Mock(status_code=200)
        query_limit.json.return_value = {"error": "QUERY_LIMIT_EXCEEDED"}
        ok = Mock(status_code=200)
        ok.json.return_value = {"result": [1]}
        for failure in (limited, query_limit, requests.ConnectionError("synthetic")):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(client.session, "post", side_effect=[failure, ok]) as post,
            ):
                self.assertEqual(client.result("list"), [1])
                self.assertEqual(post.call_count, 2)
                self.assertEqual(post.call_args.kwargs["timeout"], 1)
        with (
            patch.object(client.session, "post", return_value=limited),
            self.assertRaisesRegex(api.BitrixError, "RETRIES_EXCEEDED"),
        ):
            client.call("list")

    def test_profile_timing_failure_and_run_isolation(self):
        performance = importlib.import_module("performance")
        failure = RuntimeError("synthetic")

        @performance.profile_run
        def failed():
            with performance.measure("outer"), performance.measure("inner"):
                performance.count("attempts")
                raise failure

        with (
            patch.object(performance.RunMetrics, "report", autospec=True) as report,
            patch.object(performance, "perf_counter", side_effect=[0, 1, 2, 5, 8, 10]),
            self.assertRaises(RuntimeError) as raised,
        ):
            failed()
        self.assertIs(raised.exception, failure)
        metrics, elapsed, completed = report.call_args.args
        self.assertEqual((elapsed, completed), (10, False))
        self.assertEqual(
            metrics.timings["inner"], {"calls": 1, "errors": 1, "seconds": 3}
        )
        self.assertEqual(
            metrics.timings["outer"], {"calls": 1, "errors": 1, "seconds": 7}
        )
        self.assertEqual(metrics.counters, {"attempts": 1})
        performance.count("outside")
        with performance.measure("outside"):
            pass
        self.assertEqual(metrics.counters, {"attempts": 1})
        self.assertNotIn("outside", metrics.timings)
        with patch.object(performance.RunMetrics, "report", autospec=True) as report:
            self.assertEqual(performance.profile_run(lambda: 42)(), 42)
        fresh, _, completed = report.call_args.args
        self.assertTrue(completed)
        self.assertEqual(fresh.timings, {})
        self.assertEqual(fresh.counters, {})

    def test_api_profile_distinguishes_calls_attempts_and_delays(self):
        api = importlib.import_module("api_client")
        performance = importlib.import_module("performance")
        client = api.B24Client()
        self.addCleanup(client.session.close)
        limited = Mock(status_code=429)
        ok = Mock(status_code=200)
        ok.json.return_value = {"result": [1]}
        with (
            patch.object(performance.RunMetrics, "report", autospec=True) as report,
            patch.object(
                client.session,
                "post",
                side_effect=[
                    limited,
                    requests.ConnectionError("synthetic"),
                    ok,
                ],
            ),
        ):
            self.assertEqual(
                performance.profile_run(lambda: client.result("list"))(), [1]
            )
        metrics = report.call_args.args[0]
        self.assertEqual(metrics.timings["bitrix.call.list"]["calls"], 1)
        self.assertEqual(metrics.timings["bitrix.call.list"]["errors"], 0)
        self.assertEqual(metrics.timings["bitrix.http.list"]["calls"], 3)
        self.assertEqual(metrics.timings["bitrix.http.list"]["errors"], 1)
        self.assertEqual(metrics.timings["bitrix.backoff.list"]["calls"], 2)
        self.assertEqual(metrics.timings["bitrix.pause.list"]["calls"], 1)
        self.assertEqual(metrics.counters["bitrix.retry.list"], 2)
        self.assertEqual(metrics.counters["bitrix.http_429.list"], 1)
        self.assertEqual(metrics.counters["bitrix.network_error.list"], 1)
        with (
            patch.object(performance.RunMetrics, "report", autospec=True) as report,
            patch.object(client.session, "post", return_value=limited),
            self.assertRaises(api.BitrixError),
        ):
            performance.profile_run(lambda: client.call("list"))()
        metrics = report.call_args.args[0]
        self.assertEqual(metrics.timings["bitrix.call.list"]["errors"], 1)
        self.assertEqual(metrics.counters["bitrix.retry.list"], 2)
        self.assertEqual(metrics.counters["bitrix.http_429.list"], 3)

    def test_profile_stages_and_audio_cache_without_sensitive_data(self):
        performance = importlib.import_module("performance")
        output = io.StringIO()
        sink = logger.add(output, format="{message}")
        self.addCleanup(logger.remove, sink)
        self.run_sync()
        profile = output.getvalue().split("Профиль выгрузки:", 1)[1]
        for name in (
            "stage.reference_books",
            "stage.selection",
            "stage.deals",
            "stage.leads",
            "source.calls",
            "source.chats",
            "stage.zip",
            "stage.entity_artifacts",
        ):
            self.assertIn(name, profile)
        self.assertNotIn("https://", profile)
        self.assertNotIn("Пример", profile)
        self.assertNotIn("Здравствуйте", profile)

        call = {"activity_id": "91", "file_ids": [501]}
        path = Path(self.sync.transcribe.transcript_path(1, call))
        path.write_text("Секретный текст", encoding="utf-8")
        with (
            patch.object(performance.RunMetrics, "report", autospec=True) as report,
            patch.object(requests, "post") as post,
            patch.object(requests, "get") as get,
        ):
            result = performance.profile_run(
                lambda: self.sync.transcribe.transcribe_call(self.client, 1, call)
            )()
        self.assertEqual(result, str(path))
        post.assert_not_called()
        get.assert_not_called()
        self.assertEqual(
            report.call_args.args[0].counters, {"audio.transcript_cache_hit": 1}
        )

    def test_audio_profile_counts_polling_and_preserves_errors(self):
        performance = importlib.import_module("performance")
        transcribe = self.sync.transcribe
        submitted = Mock()
        submitted.json.return_value = {"id": "synthetic-job"}
        pending = Mock()
        pending.json.return_value = {"status": "processing"}
        completed = Mock()
        completed.json.return_value = {
            "status": "completed",
            "text": "Синтетический текст",
        }
        failed = Mock()
        failed.json.return_value = {"status": "error", "error": "synthetic-error"}
        for final, raises in ((completed, False), (failed, True)):
            with (
                self.subTest(error=raises),
                patch.object(performance.RunMetrics, "report", autospec=True) as report,
                patch.object(requests, "post", return_value=submitted),
                patch.object(requests, "get", side_effect=[pending, final]),
            ):
                run = performance.profile_run(
                    lambda: transcribe._transcribe_assemblyai_url(
                        "https://example.invalid/audio"
                    )
                )
                if raises:
                    with self.assertRaisesRegex(RuntimeError, "synthetic-error"):
                        run()
                else:
                    self.assertEqual(run(), "Синтетический текст")
            metrics = report.call_args.args[0]
            self.assertEqual(metrics.counters["audio.jobs_submitted"], 1)
            self.assertEqual(metrics.counters["audio.poll_requests"], 2)
            self.assertEqual(
                metrics.counters.get("audio.jobs_completed", 0), int(not raises)
            )
            self.assertEqual(metrics.timings["audio.polling"]["errors"], int(raises))
            self.assertEqual(
                metrics.timings["audio.recognition"]["errors"], int(raises)
            )

    def test_logging_levels_append_and_reconfiguration(self):
        setup = importlib.import_module("logging_setup")
        setup.configure_logging()
        setup.log.opt().debug("Отладка: ID {}", 42)
        setup.log.info("Запуск снимка")
        setup.log.warning("Повтор запроса")
        setup.log.error("Ошибка сущности")
        setup.configure_logging()
        setup.log.info("Повторная настройка")
        logging.getLogger("synthetic_library").info("Шум библиотеки")
        logging.getLogger("synthetic_library").warning("Предупреждение библиотеки")
        logging.getLogger("synthetic_library").error("Ошибка библиотеки")
        setup.log.remove()

        debug = (self.root / "debug.log").read_text(encoding="utf-8")
        error = (self.root / "error.log").read_text(encoding="utf-8")
        for message in (
            "Отладка: ID 42",
            "Запуск снимка",
            "Повтор запроса",
            "Ошибка сущности",
            "Повторная настройка",
            "Предупреждение библиотеки",
            "Ошибка библиотеки",
        ):
            self.assertEqual(debug.count(message), 1)
        self.assertNotIn("Шум библиотеки", debug)
        self.assertIn("pid=", debug)
        self.assertIn("test_export:test_logging_levels", debug)
        self.assertEqual(error.count("Ошибка сущности"), 1)
        self.assertEqual(error.count("Ошибка библиотеки"), 1)
        self.assertNotIn("Предупреждение", error)
        self.assertNotIn("Отладка", error)
        self.assertNotIn("Запуск", error)
        self.assertEqual(
            sorted(p.name for p in self.root.glob("*.log")), ["debug.log", "error.log"]
        )

    def test_logging_redacts_messages_and_chained_tracebacks(self):
        setup = importlib.import_module("logging_setup")
        key = "assembly-key-for-test"
        bot_token = "123456:telegram-test-secret"
        self.cfg.ASSEMBLYAI_API_KEY = key
        with patch.dict(os.environ, {"TG_BOT_TOKEN": bot_token}):
            setup.configure_logging()
        url = (
            "https://example.invalid/rest/1/webhook-secret/download/?token=disk-secret"
        )
        setup.log.opt().warning("Ссылка {}; ключ {}; Telegram {}", url, key, bot_token)
        setup.log.warning("Authorization: Bearer unknown-bearer-secret")
        setup.log.warning("Authorization: Basic unknown-basic-secret")

        def fail():
            private_value = "LOCAL-VALUE-MUST-NOT-BE-LOGGED"
            try:
                raise ValueError(f"token=disk-secret {key} {bot_token}")
            except ValueError as cause:
                self.assertTrue(private_value)
                raise RuntimeError(f"Скачивание {url}") from cause

        try:
            fail()
        except RuntimeError:
            setup.log.opt(exception=True).error("Сущность {} звонок {}: сбой", 7, 11)
        try:
            raise RuntimeError("/rest/1/another-secret/download/?token=path-secret")
        except RuntimeError:
            logging.getLogger("synthetic_library").exception("Сбой библиотеки")
        setup.log.remove()

        for filename in ("debug.log", "error.log"):
            content = (self.root / filename).read_text(encoding="utf-8")
            for secret in (
                url,
                key,
                bot_token,
                "disk-secret",
                "webhook-secret",
                "another-secret",
                "path-secret",
                "LOCAL-VALUE-MUST-NOT-BE-LOGGED",
            ):
                self.assertNotIn(secret, content)
            self.assertIn("[REDACTED]", content)
            self.assertIn("RuntimeError", content)
            self.assertIn("ValueError", content)
            self.assertIn("Traceback", content)
            self.assertIn("Сущность 7 звонок 11", content)
            self.assertNotIn("unknown-bearer-secret", content)
            self.assertNotIn("unknown-basic-secret", content)

    def test_cli_logs_failure_and_preserves_exit_codes(self):
        setup = importlib.import_module("logging_setup")

        def fail():
            raise RuntimeError("Синтетический сбой CLI")

        with self.assertRaises(SystemExit) as failure:
            setup.run_cli(fail)
        self.assertEqual(failure.exception.code, 1)
        with self.assertRaises(SystemExit) as success:
            setup.run_cli(lambda: 0)
        self.assertEqual(success.exception.code, 0)
        with self.assertRaises(SystemExit) as config_error:
            setup.run_cli(lambda: sys.exit("Нет настройки доступа"))
        self.assertEqual(config_error.exception.code, 1)
        setup.log.remove()
        content = (self.root / "error.log").read_text(encoding="utf-8")
        self.assertEqual(content.count("Аварийное завершение"), 1)
        self.assertIn("Синтетический сбой CLI", content)
        self.assertIn("Нет настройки доступа", content)

    def test_transcription_failure_is_logged_with_call_id(self):
        setup = importlib.import_module("logging_setup")
        setup.configure_logging()
        transcribe = importlib.import_module("transcribe")
        call = {"activity_id": 123, "local_audio": "synthetic.mp3"}
        with patch.object(
            transcribe, "transcribe_file", side_effect=RuntimeError("AA failed")
        ):
            self.assertIsNone(transcribe.transcribe_call(self.client, 7, call))
        setup.log.remove()
        error = (self.root / "error.log").read_text(encoding="utf-8")
        self.assertIn("Сущность 7 звонок 123", error)
        self.assertIn("AA failed", error)


if __name__ == "__main__":
    unittest.main()
