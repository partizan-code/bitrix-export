"""Единые файловые логи Loguru с маскированием секретов и URL."""

import logging
import os
import re
import sys
import traceback
from collections.abc import Callable

from loguru import logger

import config

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_REST_PATH = re.compile(r"/rest/\d+/[^/\s\"'?]+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"(?i)(\b(?:token|api_key|authorization|access_token|auth)\b[\"']?\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?(?:[\"'][^\"'\r\n]*[\"']|[^\s&,;)]+)"
)
_secrets: tuple[str, ...] = ()


def _redact(message: str) -> str:
    """Скрыть известные ключи, ссылки и параметры авторизации в ошибках."""
    for secret in _secrets:
        message = message.replace(secret, "[REDACTED]")
    message = _URL.sub("[URL]", message)
    message = _REST_PATH.sub("/rest/[REDACTED]", message)
    return _CREDENTIAL.sub(r"\1[REDACTED]", message)


def _sanitize_record(record: dict) -> None:
    """Обработать и сообщение, и цепочку исключений без значений переменных."""
    message = record["message"]
    exception = record["exception"]
    if exception is not None:
        message += "\n" + "".join(
            traceback.format_exception(exception.type, exception.value, exception.traceback)
        ).rstrip()
        record["exception"] = None
    record["message"] = _redact(message)


log = logger.patch(_sanitize_record)


class _InterceptHandler(logging.Handler):
    """Направлять предупреждения и ошибки сторонних библиотек в общие файлы."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = log.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = sys._getframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        log.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(*, diagnostic_path: str | None = None) -> None:
    """Настроить общие логи либо один отдельный диагностический файл."""
    global _secrets
    values = [
        config.WEBHOOK_URL,
        config.ASSEMBLYAI_API_KEY,
        os.environ.get("TG_BOT_TOKEN"),
    ]
    # requests иногда показывает только путь вебхука, без схемы и хоста.
    if config.WEBHOOK_URL:
        values.append(config.WEBHOOK_URL.rstrip("/").rsplit("/", 1)[-1])
    _secrets = tuple(sorted({str(v) for v in values if v}, key=len, reverse=True))
    log.remove()
    options = {
        "format": "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                  "pid={process.id} | {name}:{function}:{line} | {message}",
        "encoding": "utf-8",
        "backtrace": False,
        "diagnose": False,
        "colorize": False,
    }
    if diagnostic_path is not None:
        log.add(diagnostic_path, level="DEBUG", **options)
    else:
        log.add(config.DEBUG_LOG_PATH, level="DEBUG", **options)
        log.add(config.ERROR_LOG_PATH, level="ERROR", **options)
    logging.basicConfig(
        handlers=[_InterceptHandler(level=logging.WARNING)],
        level=logging.WARNING,
        force=True,
    )


def run_cli(main: Callable[[], int | None]) -> None:
    """Запустить CLI и сохранить неожиданный сбой в файлах с ненулевым кодом выхода."""
    configure_logging()
    try:
        code = main()
    except SystemExit as error:
        if error.code:
            log.opt().error("Ошибка запуска: {}", error.code)
            if not isinstance(error.code, int):
                raise SystemExit(1) from None
        raise
    except Exception:
        log.exception("Аварийное завершение")
        raise SystemExit(1) from None
    raise SystemExit(code)
