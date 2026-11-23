"""Изолированные проверки запуска выгрузки из бота без Telegram и подпроцессов."""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class BotTests(unittest.TestCase):
    """Проверить атомарный PID-файл и защиту от одновременных нажатий."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(
            patch.dict(os.environ, {"TG_CHAT_ID": "7"}, clear=True)
        )
        cfg = types.ModuleType("config")
        cfg.DATA_DIR = cfg.EXPORT_DIR = str(self.root)
        logging = types.ModuleType("logging_setup")
        logging.log = Mock()
        logging.run_cli = Mock()
        aiogram = types.ModuleType("aiogram")
        aiogram.Bot = Mock()
        aiogram.F = Mock()
        dispatcher = Mock()
        dispatcher.message.side_effect = lambda *args: lambda function: function
        dispatcher.callback_query.side_effect = lambda *args: lambda function: function
        aiogram.Dispatcher = Mock(return_value=dispatcher)
        exceptions = types.ModuleType("aiogram.exceptions")
        exceptions.TelegramAPIError = type("TelegramAPIError", (Exception,), {})
        filters = types.ModuleType("aiogram.filters")
        filters.CommandStart = Mock()
        telegram_types = types.ModuleType("aiogram.types")
        for name in (
            "CallbackQuery",
            "FSInputFile",
            "InlineKeyboardButton",
            "InlineKeyboardMarkup",
            "Message",
        ):
            setattr(telegram_types, name, Mock())
        self.stack.enter_context(
            patch.dict(
                sys.modules,
                {
                    "config": cfg,
                    "logging_setup": logging,
                    "aiogram": aiogram,
                    "aiogram.exceptions": exceptions,
                    "aiogram.filters": filters,
                    "aiogram.types": telegram_types,
                },
            )
        )
        self.stack.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена"))
        )
        self.stack.enter_context(
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS запрещён"))
        )
        self.process = Mock(pid=123)
        self.popen = self.stack.enter_context(
            patch("subprocess.Popen", return_value=self.process)
        )
        spec = importlib.util.spec_from_file_location(
            "isolated_bot", Path(__file__).resolve().parents[1] / "bot.py"
        )
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)

    def test_start_holds_lock_until_pid_is_published(self):
        queries = [
            types.SimpleNamespace(
                answer=AsyncMock(),
                message=types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=7), edit_text=AsyncMock()
                ),
            )
            for _ in range(2)
        ]

        async def inline_thread(function, *args):
            # Моки завершают await сразу: цикл событий с socketpair не требуется.
            self.assertTrue(self.bot.START_LOCK.locked())
            return function(*args)

        def discard_monitor(coroutine):
            coroutine.close()

        with (
            patch.object(self.bot, "running_pid", side_effect=[None, 123]),
            patch.object(self.bot, "start_sync", return_value=self.process) as start,
            patch.object(self.bot.asyncio, "to_thread", side_effect=inline_thread),
            patch.object(
                self.bot.asyncio, "create_task", side_effect=discard_monitor
            ) as monitor,
        ):
            for query in queries:
                coroutine = self.bot.on_run(query)
                try:
                    with self.assertRaises(StopIteration):
                        coroutine.send(None)
                finally:
                    coroutine.close()
        start.assert_called_once_with()
        monitor.assert_called_once()
        self.assertFalse(self.bot.START_LOCK.locked())
        self.assertTrue(
            any("уже идёт" in q.message.edit_text.call_args.args[0] for q in queries)
        )

    def test_start_publishes_pid_and_clears_progress(self):
        Path(self.bot.PROGRESS).write_text("old", encoding="utf-8")
        self.assertIs(self.bot.start_sync(), self.process)
        self.assertEqual(Path(self.bot.LOCK).read_text(encoding="utf-8"), "123")
        self.assertFalse(Path(self.bot.LOCK + ".tmp").exists())
        self.assertFalse(Path(self.bot.PROGRESS).exists())
        self.popen.assert_called_once_with(
            [sys.executable, "sync.py"], cwd=self.bot.BASE, env=os.environ.copy()
        )

    def test_failed_pid_replace_stops_process_and_preserves_lock(self):
        Path(self.bot.LOCK).write_text("old", encoding="utf-8")
        with (
            patch.object(self.bot.os, "replace", side_effect=OSError("Недоступно")),
            self.assertRaises(OSError),
        ):
            self.bot.start_sync()
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(Path(self.bot.LOCK).read_text(encoding="utf-8"), "old")
        self.assertFalse(Path(self.bot.LOCK + ".tmp").exists())
