"""Диагностика получения записей Bitrix24 без сохранения аудио и транскрибации.

Запуск и описание безопасного лога: docs/operations.md#диагностика-скачивания-аудио.
"""

import argparse
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Позволить прямой запуск из scripts без установки проекта как пакета.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import transcribe
from api_client import B24Client
from logging_setup import configure_logging, log


def event(name: str, **fields) -> None:
    """Записать только явно выбранные диагностические поля."""
    log.info("{}", json.dumps({"event": name, **fields}, ensure_ascii=False))


def curl_probe(url: str) -> dict:
    """Сравнить HTTP-клиенты; URL передаётся curl через stdin, а не аргументы."""
    executable = shutil.which("curl")
    if executable is None:
        return {"outcome": "not_installed"}
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        return {"outcome": "unsafe_url"}
    # Не передавать параметры через строки с переводами строк или управляющими символами.
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return {"outcome": "unsafe_url"}
    settings = 'url = "' + url.replace("\\", "\\\\").replace('"', '\\"') + '"\n'
    fields = (
        "http_status",
        "http_version",
        "bytes_read",
        "headers_bytes",
        "first_data_seconds",
        "elapsed_seconds",
    )
    output_format = (
        "%{http_code}\t%{http_version}\t%{size_download}\t%{size_header}"
        "\t%{time_starttransfer}\t%{time_total}"
    )
    try:
        completed = subprocess.run(
            [
                executable,
                "--disable",
                "--silent",
                "--proto",
                "=https",
                "--connect-timeout",
                "8",
                "--max-time",
                "12",
                "--range",
                "0-65535",
                "--max-filesize",
                "65536",
                "--output",
                os.devnull,
                "--write-out",
                output_format,
                "--config",
                "-",
            ],
            input=settings,
            text=True,
            capture_output=True,
            timeout=16,
            check=False,
        )
        values = completed.stdout.strip().split("\t")
        if len(values) != len(fields):
            return {"outcome": "invalid_metrics", "exit_code": completed.returncode}
        # Никакие stderr или произвольный текст curl в отчёт не попадают.
        metrics = [float(value) for value in values]
        outcome = "error" if completed.returncode else "sample_complete"
        if not completed.returncode and metrics[0] not in (200, 206):
            outcome = "http_error"
        return dict(
            zip(fields, metrics, strict=True),
            exit_code=completed.returncode,
            outcome=outcome,
        )
    except Exception as error:  # noqa: BLE001 — curl необязателен
        return {"outcome": "error", "error_type": type(error).__name__}


def diagnose(client: B24Client, activity_ids: list[int], file_ids: list[int]) -> int:
    """Получить свежие ссылки и выполнить ограниченные проверки каждой записи."""
    failures = 0
    files = set(file_ids)
    for activity_id in activity_ids:
        try:
            activity = client.result("crm.activity.get", {"id": activity_id}) or {}
            attached = activity.get("FILES") or []
            if not attached:
                event("activity", activity_id=activity_id, outcome="no_files")
                failures += 1
                continue
            item = attached[0]
            file_id = (
                int(item.get("id") or item.get("ID"))
                if isinstance(item, dict)
                else int(item)
            )
            files.add(file_id)
            event(
                "activity",
                activity_id=activity_id,
                file_id=file_id,
                file_count=len(attached),
            )
        except Exception as error:  # noqa: BLE001 — проверить следующие ID
            event(
                "activity_error",
                activity_id=activity_id,
                error_type=type(error).__name__,
            )
            failures += 1
    for file_id in sorted(files):
        try:
            print(f"Файл {file_id}: получение свежей ссылки…", flush=True)
            info = transcribe.get_disk_file(client, file_id)
            url = transcribe.resolve_file_url(info)
            size = int(info.get("SIZE") or 0)
            if not url:
                event("file", file_id=file_id, outcome="no_download_url")
                failures += 1
                continue
            event("file", file_id=file_id, size_bytes=size)
            for variant, options in (
                ("stream", {}),
                ("range_first_8k", {"byte_range": (0, 8191)}),
                ("range_later_8k", {"byte_range": (16384, 24575)}),
                ("connection_close", {"close_connection": True}),
            ):
                if variant == "range_later_8k" and size <= 16384:
                    event(
                        "probe",
                        file_id=file_id,
                        variant=variant,
                        outcome="file_too_small",
                    )
                    continue
                if "byte_range" in options and size > 0:
                    start, end = options["byte_range"]
                    options["byte_range"] = (start, min(end, size - 1))
                print(f"Файл {file_id}: {variant}…", flush=True)
                result = transcribe.probe_recording(url, **options)
                event("probe", file_id=file_id, variant=variant, **result)
                if variant == "stream" and result["outcome"] not in {
                    "file_complete",
                    "sample_complete",
                    "eof",
                }:
                    failures += 1
            event("probe", file_id=file_id, variant="curl_range_64k", **curl_probe(url))
        except Exception as error:  # noqa: BLE001 — проверить остальные файлы
            event("file_error", file_id=file_id, error_type=type(error).__name__)
            failures += 1
    event("summary", files=len(files), stream_or_metadata_failures=failures)
    return 1 if failures else 0


def positive_id(value: str) -> int:
    """Отклонить отрицательные и нулевые ID до сетевых запросов."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("ID должен быть положительным")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--activity-ids", type=positive_id, nargs="+")
    source.add_argument("--file-ids", type=positive_id, nargs="+")
    parser.add_argument("--log", type=Path, help="Путь нового диагностического лога")
    args = parser.parse_args(argv)
    ids = args.activity_ids or args.file_ids
    if len(ids) > 4:
        parser.error("За один запуск допускается не более четырёх ID")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (
        args.log or Path(config.BASE_DIR) / f"diag_download_{stamp}_{os.getpid()}.log"
    )
    if destination.exists():
        parser.error("Файл лога уже существует; выберите новое имя")
    temporary = None
    configured = False
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".diag_download_",
            suffix=".log",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        configure_logging(diagnostic_path=str(temporary))
        configured = True
        event(
            "environment",
            time_utc=datetime.now(timezone.utc).isoformat(),
            python=platform.python_version(),
            system=platform.system(),
            release=platform.release(),
            machine=platform.machine(),
            openssl=ssl.OPENSSL_VERSION,
            requests=transcribe.requests.__version__,
            curl_available=shutil.which("curl") is not None,
            proxy_env_present=any(
                os.environ.get(key)
                for key in (
                    "HTTPS_PROXY",
                    "https_proxy",
                    "ALL_PROXY",
                    "all_proxy",
                )
            ),
            byte_limit=65536,
            read_timeout_seconds=10,
            probe_budget_seconds=30,
            api_timeout_seconds=config.REQUEST_TIMEOUT,
            api_max_retries=config.MAX_RETRIES,
        )
        print("Диагностика запущена. Ожидайте завершения проверок…", flush=True)
        client = B24Client()
        try:
            return diagnose(client, args.activity_ids or [], args.file_ids or [])
        finally:
            client.session.close()
    except (Exception, SystemExit) as error:  # noqa: BLE001 — сохранить диагностический лог при сбое
        if configured:
            event("fatal", error_type=type(error).__name__)
        print(
            f"Диагностика завершилась с ошибкой: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        if configured:
            event("interrupted")
        return 130
    finally:
        if configured:
            log.remove()
        if temporary is not None:
            os.replace(temporary, destination)
            print(f"Диагностический лог: {destination}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
