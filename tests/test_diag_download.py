"""Изолированные проверки диагностики HTTP без API, секретов и рабочего аудио."""

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = ("config", "logging_setup", "api_client", "transcribe", "diag_download")
URL = "https://example.invalid/rest/1/webhook-secret/download/?token=disk-secret"


class Response:
    """Поток учитывает размер чтения и умеет зависнуть после частичных данных."""

    def __init__(self, body=b"ID3synthetic", *, status=200, headers=None, stall=False):
        self.status_code = status
        self.headers = {"Content-Type": "audio/mpeg", "Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.buffer = io.BytesIO(body)
        self.closed = False
        self.stall = stall
        self.raw = types.SimpleNamespace(read1=self.read, read=self.read)

    def read(self, amount):
        chunk = self.buffer.read(amount)
        if not chunk and self.stall:
            raise requests.ReadTimeout(f"{URL} authorization=private-value")
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class DownloadDiagnosticTests(unittest.TestCase):
    """Проверить реальные алгоритмы на поддельных HTTP-ответах и временных логах."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(
            patch.object(
                socket.socket, "connect", side_effect=AssertionError("Сеть запрещена")
            )
        )
        self.stack.enter_context(
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("DNS запрещён")
            )
        )
        self.stack.enter_context(
            patch("subprocess.run", side_effect=AssertionError("Subprocess запрещён"))
        )
        self.previous = {name: sys.modules.get(name) for name in MODULES}
        for name in MODULES:
            sys.modules.pop(name, None)
        self.addCleanup(self.restore_modules)
        cfg = types.ModuleType("config")
        cfg.BASE_DIR = str(self.root)
        cfg.WEBHOOK_URL = "https://example.invalid/rest/1/webhook-secret/"
        cfg.ASSEMBLYAI_API_KEY = "assembly-secret"
        cfg.DEBUG_LOG_PATH = str(self.root / "debug.log")
        cfg.ERROR_LOG_PATH = str(self.root / "error.log")
        cfg.REQUEST_TIMEOUT = 1
        cfg.MAX_RETRIES = 1
        cfg.REQUEST_PAUSE = 0
        sys.modules["config"] = cfg
        specification = importlib.util.spec_from_file_location(
            "diag_download",
            Path(__file__).resolve().parents[1] / "scripts" / "diag_download.py",
        )
        self.module = importlib.util.module_from_spec(specification)
        sys.modules["diag_download"] = self.module
        specification.loader.exec_module(self.module)
        self.addCleanup(self.module.log.remove)
        self.module.log.remove()

    def restore_modules(self):
        for name, module in self.previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def session(self, responses):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = responses
        return session

    def probe(self, responses, **kwargs):
        session = self.session(responses)
        with patch.object(
            self.module.transcribe.requests, "Session", return_value=session
        ):
            return self.module.transcribe.probe_recording(URL, **kwargs), session

    def test_partial_data_survives_timeout_without_secrets(self):
        response = Response(
            b"ID3" + b"x" * 14997, headers={"Content-Length": "41645"}, stall=True
        )
        result, session = self.probe([response])
        self.assertEqual(result["bytes_read"], 15000)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["error_type"], "ReadTimeout")
        self.assertTrue(result["audio_signature"])
        self.assertTrue(response.closed)
        session.__exit__.assert_called_once()
        serialized = json.dumps(result)
        for secret in (URL, "disk-secret", "private-value", "webhook-secret"):
            self.assertNotIn(secret, serialized)

    def test_large_body_is_bounded_without_audio_files(self):
        response = Response(b"ID3" + b"x" * 200000)
        result, session = self.probe([response])
        self.assertEqual(result["bytes_read"], 65536)
        self.assertEqual(result["outcome"], "sample_complete")
        self.assertEqual(session.get.call_args.kwargs["timeout"], (8, 10))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_full_small_file_and_unexpected_eof(self):
        result, _ = self.probe([Response()])
        self.assertEqual(result["outcome"], "file_complete")
        result, _ = self.probe([Response(headers={"Content-Length": "41645"})])
        self.assertEqual(result["outcome"], "incomplete")

    def test_range_response_and_ignored_range(self):
        headers = {"Content-Range": "bytes 16384-24575/41645"}
        result, session = self.probe(
            [Response(b"x" * 8192, status=206, headers=headers)],
            byte_range=(16384, 24575),
        )
        self.assertTrue(result["range_honored"])
        self.assertEqual(result["bytes_read"], 8192)
        self.assertEqual(
            session.get.call_args.kwargs["headers"]["Range"], "bytes=16384-24575"
        )
        result, _ = self.probe([Response(b"x" * 50000)], byte_range=(16384, 24575))
        self.assertFalse(result["range_honored"])
        self.assertEqual(result["bytes_read"], 8192)

    def test_redirect_is_closed_and_http_is_rejected(self):
        redirect = Response(
            status=302, headers={"Location": "https://cdn.example.invalid/audio"}
        )
        result, session = self.probe([redirect, Response()])
        self.assertEqual(result["redirects"], 1)
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(redirect.closed)
        result, session = self.probe(
            [Response(status=302, headers={"Location": "http://example.invalid/audio"})]
        )
        self.assertEqual(result["outcome"], "unsafe_url")
        self.assertEqual(session.get.call_count, 1)

    def test_total_budget_and_http_error(self):
        with patch.object(
            self.module.transcribe.time, "monotonic", side_effect=[0, 31, 31]
        ):
            result = self.module.transcribe.probe_recording(URL)
        self.assertEqual(result["outcome"], "time_budget")
        result, _ = self.probe([Response(status=403)])
        self.assertEqual(result["outcome"], "http_error")
        self.assertEqual(result["bytes_read"], 0)

    def test_empty_or_html_response_is_not_successful_audio(self):
        result, _ = self.probe([Response(b"")])
        self.assertEqual(result["outcome"], "empty")
        result, _ = self.probe(
            [Response(b"<!doctype html><html>private content</html>")]
        )
        self.assertEqual(result["outcome"], "unexpected_content")
        self.assertNotIn("private content", json.dumps(result))

    def test_invalid_limits_do_not_start_requests(self):
        for options in (
            {"byte_limit": 100000},
            {"read_timeout": 0},
            {"byte_range": (5, 4)},
        ):
            with (
                patch.object(self.module.transcribe.requests, "Session") as session,
                self.assertRaises(ValueError),
            ):
                self.module.transcribe.probe_recording(URL, **options)
            session.assert_not_called()

    def test_curl_keeps_url_out_of_argv_and_ignores_stderr(self):
        completed = types.SimpleNamespace(
            returncode=28, stdout="200\t2\t15271\t1000\t0.5\t12", stderr=URL
        )
        with (
            patch.object(self.module.shutil, "which", return_value="curl"),
            patch.object(self.module.subprocess, "run", return_value=completed) as run,
        ):
            result = self.module.curl_probe(URL)
        self.assertEqual(result["bytes_read"], 15271)
        self.assertNotIn(URL, run.call_args.args[0])
        self.assertIn(URL, run.call_args.kwargs["input"])
        self.assertNotIn("disk-secret", json.dumps(result))
        self.assertIn("--disable", run.call_args.args[0])

    def test_main_writes_only_diagnostic_log_and_continues_after_api_error(self):
        for name in ("debug.log", "error.log"):
            (self.root / name).write_text("existing", encoding="utf-8")
        destination = self.root / "diagnostic.log"
        client = Mock()
        client.result.side_effect = [
            {"FILES": [{"id": "101"}], "SUBJECT": "PRIVATE-SUBJECT"},
            requests.ConnectionError(URL),
            {"DOWNLOAD_URL": URL, "SIZE": "41645", "NAME": "PRIVATE-NAME"},
        ]
        responses = [
            Response(
                b"ID3" + b"x" * 14997, headers={"Content-Length": "41645"}, stall=True
            )
            for _ in range(4)
        ]
        session = self.session(responses)
        with (
            patch.object(self.module, "B24Client", return_value=client),
            patch.object(
                self.module.transcribe.requests, "Session", return_value=session
            ),
            patch.object(
                self.module, "curl_probe", return_value={"outcome": "not_installed"}
            ),
            redirect_stdout(io.StringIO()),
        ):
            code = self.module.main(
                ["--activity-ids", "91", "92", "--log", str(destination)]
            )
        self.assertEqual(code, 1)
        content = destination.read_text(encoding="utf-8")
        for secret in (
            URL,
            "PRIVATE-SUBJECT",
            "PRIVATE-NAME",
            "disk-secret",
            "private-value",
        ):
            self.assertNotIn(secret, content)
        self.assertIn('"bytes_read": 15000', content)
        self.assertIn('"event": "summary"', content)
        self.assertIn('"event": "activity_error"', content)
        client.session.close.assert_called_once()
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["debug.log", "diagnostic.log", "error.log"],
        )
        for name in ("debug.log", "error.log"):
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), "existing")

    def test_existing_log_is_preserved_without_network(self):
        destination = self.root / "existing.log"
        destination.write_text("keep", encoding="utf-8")
        with (
            patch.object(self.module, "B24Client") as client,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.module.main(["--file-ids", "101", "--log", str(destination)])
        client.assert_not_called()
        self.assertEqual(destination.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
