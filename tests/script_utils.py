"""Загрузка служебных команд по пути без превращения scripts в пакет."""

import importlib.util
from pathlib import Path
from types import ModuleType


def load_script(name: str) -> ModuleType:
    """Каждый раз загрузить команду с текущими подменами зависимостей теста."""
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    specification = importlib.util.spec_from_file_location(f"_script_{name}", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
