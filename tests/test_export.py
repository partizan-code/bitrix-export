"""Изолированные регрессионные проверки выгрузки на синтетических данных."""

import copy
import hashlib
import importlib
import json
import logging
import os
import socket
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
SANDBOX = None
STAMP = "2026-11-23T12:00:00+0300"
ISO = "2026-11-23T12:00:00+03:00"
PROJECT_MODULES = (
    "config",
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
    "refresh_names",
    "recover_registry",
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
        "PREFER_BUILTIN_TRANSCRIPT": True,
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
        "FILE_BRAND": "TEST",
        "COUNTRY_FIELD": "UF_COUNTRY",
        "FILE_COUNTRY": "RB",
        "ZIP_NAME_TEMPLATE": "Снимок {stamp}",
        "BASE_DIR": str(root),
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
        if method == "crm.activity.call.getTranscript":
            return {"transcript": "Здравствуйте, синтетический разговор."}
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
        self.stack.enter_context(patch("logging.basicConfig"))
        self.stack.enter_context(
            patch.object(logging.getLogger("b24"), "disabled", True)
        )
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
        self.stack.enter_context(patch("time.strftime", return_value=STAMP))
        self.stack.enter_context(patch("time.sleep"))

    def close_sandbox(self):
        global SANDBOX
        try:
            self.temp.cleanup()
        finally:
            SANDBOX = None

    def run_sync(self, *args):
        with patch.object(sys, "argv", ["sync.py", *args]):
            self.sync.main()
        return self.read_json("export/snapshot/manifest.json")

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
            self.assertEqual(call["content"]["transcript_status"], "builtin")
            self.assertEqual(len([t for t in tps if t["event_type"] == "message"]), 1)
            md = self.sync.store.load_md(1, entity=entity)
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
        # Эталон исходной версии: все файлы и трасса API, до очистки кода.
        self.assertEqual(
            self.digest(),
            "c8dbb97c3ee13433b551ea11a09fb98ab1f4dc3f6d06a0e1778733d3291f3ac9",
        )

    def test_incremental_snapshot(self):
        self.run_sync()
        self.client.omit_closed = True
        self.client.trace.clear()
        manifest = self.run_sync()
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
            "40c7238a569d135c6a3296549e0f0c5bfb2375609aa27e8184c8d48815472280",
        )

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

    def test_cli_limits_do_not_change_state_or_registry(self):
        self.run_sync()
        before = {
            p: (self.root / p).read_bytes()
            for p in ("data/state.json", "data/registry.json")
        }
        manifest = self.run_sync("--limit", "1", "--lead-limit", "1", "--no-transcribe")
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
        manifest = self.run_sync("--deals", "1", "--no-transcribe")
        self.assertEqual(manifest["deals"]["total"], 1)
        self.assertEqual(manifest["leads"]["total"], 0)
        self.assertFalse((self.root / "data/state.json").exists())
        self.assertFalse((self.root / "data/registry.json").exists())

    def test_cli_skip_transcribe_preserves_builtin_and_skips_external(self):
        with patch.object(self.sync.transcribe, "transcribe_call") as external:
            self.run_sync("--deals", "1", "--skip-transcribe")
            call = next(
                t for t in self.sync.store.load_tps(1) if t["event_type"] == "call"
            )
            self.assertEqual(call["content"]["transcript_status"], "builtin")
            self.cfg.PREFER_BUILTIN_TRANSCRIPT = False
            self.run_sync("--deals", "1", "--skip-transcribe")
            call = next(
                t for t in self.sync.store.load_tps(1) if t["event_type"] == "call"
            )
            self.assertEqual(call["content"]["transcript_status"], "pending")
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
        self.run_sync("--limit", "1", "--lead-limit", "1")
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
        args = types.SimpleNamespace(no_transcribe=False, skip_transcribe=False)
        self.cfg.PREFER_BUILTIN_TRANSCRIPT = False
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, {}, args),
            {"text": None, "status": "no_recording"},
        )
        call = {"activity_id": "91", "file_ids": [501]}
        self.cfg.TRANSCRIBE_ENABLED = False
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, call, args)["status"],
            "pending",
        )
        self.cfg.TRANSCRIBE_ENABLED = True
        with patch.object(self.sync.transcribe, "transcribe_call", return_value=None):
            self.assertEqual(
                self.sync.resolve_transcript(self.client, 1, call, args)["status"],
                "no_transcript",
            )

    def test_refresh_names_offline_and_recovery(self):
        self.run_sync()
        before = {
            p: (self.root / p).read_bytes()
            for p in ("data/state.json", "data/registry.json")
        }
        refresh = importlib.import_module("refresh_names")
        self.client.trace.clear()
        with patch.object(sys, "argv", ["refresh_names.py", "--offline"]):
            refresh.main()
        self.assertFalse(self.client.trace)
        for path, content in before.items():
            self.assertEqual((self.root / path).read_bytes(), content)
        self.assertEqual(len(list((self.root / "export/snapshot").rglob("*.md"))), 6)
        self.assertIn("Участников И.", self.sync.store.load_md(1, entity="lead"))
        reg = self.read_json("data/registry.json")
        del reg["lead:2"]
        self.write_json("data/registry.json", reg)
        recovery = importlib.import_module("recover_registry")
        with patch("builtins.print"):
            recovery.main()
        recovered = self.read_json("data/registry.json")
        self.assertEqual(set(recovered), set(json.loads(before["data/registry.json"])))
        self.assertEqual(recovered["lead:2"]["exported_at"], "RECOVERED_FROM_STORE")

    def test_transcript_flags_builtin_cache_and_assemblyai(self):
        call = {"activity_id": "91", "file_ids": [501]}
        args = types.SimpleNamespace(no_transcribe=False, skip_transcribe=True)
        builtin = self.sync.resolve_transcript(self.client, 1, call, args)
        self.assertEqual(builtin["status"], "builtin")
        self.cfg.PREFER_BUILTIN_TRANSCRIPT = False
        self.assertEqual(
            self.sync.resolve_transcript(self.client, 1, call, args)["status"],
            "pending",
        )
        args.skip_transcribe = False
        self.cfg.ASSEMBLYAI_DIRECT_URL = False
        audio = self.root / "audio/synthetic.mp3"
        audio.write_bytes(b"synthetic")
        call["local_audio"] = str(audio)
        with patch.object(
            self.sync.transcribe, "_transcribe_assemblyai", return_value="Текст"
        ) as mocked:
            self.assertEqual(
                self.sync.resolve_transcript(self.client, 1, call, args),
                {"text": "Текст", "status": "assemblyai"},
            )
            self.sync.resolve_transcript(self.client, 1, call, args)
            mocked.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
