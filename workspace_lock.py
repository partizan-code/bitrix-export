"""Общая блокировка записи для выгрузки и обновления сохранённых переписок."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class WorkspaceBusyError(RuntimeError):
    """Безопасный код отказа для интерфейсов командной строки."""

    error = "WORKSPACE_BUSY"


@contextmanager
def exclusive_workspace(data_dir: Path):
    """Не допускать параллельную запись; оставшийся после аварии lock не снимать вслепую."""
    path = Path(data_dir) / "workspace.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = json.dumps({"pid": os.getpid(), "token": uuid4().hex})
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(token)
    except FileExistsError:
        raise WorkspaceBusyError(
            "WORKSPACE_BUSY: проверьте data/workspace.lock"
        ) from None
    try:
        yield
    finally:
        if path.exists() and path.read_text(encoding="utf-8") == token:
            path.unlink()
