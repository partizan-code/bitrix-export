"""Замеры одного прогона без параметров запросов и содержимого выгрузки."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from typing import TypedDict

from logging_setup import log

_current: ContextVar["RunMetrics | None"] = ContextVar("run_metrics", default=None)


class _Timing(TypedDict):
    calls: int
    errors: int
    seconds: float


class RunMetrics:
    """Суммарные длительности и счётчики, изолированные между прогонами."""

    def __init__(self) -> None:
        self.timings: dict[str, _Timing] = {}
        self.counters: dict[str, int] = {}

    def report(self, elapsed: float, completed: bool) -> None:
        """Записать только имена операций и числовые агрегаты в общий лог."""
        log.info(
            "Профиль выгрузки: {:.3f} с; статус={}",
            elapsed,
            "завершена" if completed else "прервана",
        )
        for name, row in sorted(
            self.timings.items(), key=lambda item: (-item[1]["seconds"], item[0])
        ):
            log.info(
                "Профиль: {} | вызовов={} | ошибок={} | всего={:.3f} с | среднее={:.3f} с",
                name,
                row["calls"],
                row["errors"],
                row["seconds"],
                row["seconds"] / row["calls"],
            )
        for name, value in sorted(self.counters.items()):
            log.info("Профиль: {} | количество={}", name, value)


@contextmanager
def measure(name: str) -> Iterator[None]:
    """Учесть время и выход с исключением; вне прогона ничего не собирать."""
    metrics = _current.get()
    if metrics is None:
        yield
        return
    started = perf_counter()
    completed = False
    try:
        yield
        completed = True
    finally:
        elapsed = perf_counter() - started
        row = metrics.timings.setdefault(
            name, {"calls": 0, "errors": 0, "seconds": 0.0}
        )
        row["calls"] += 1
        row["errors"] += int(not completed)
        row["seconds"] += elapsed


def count(name: str) -> None:
    """Увеличить числовой счётчик без записи отдельных событий в лог."""
    metrics = _current.get()
    if metrics is not None:
        metrics.counters[name] = metrics.counters.get(name, 0) + 1


def timed[**P, R](name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Измерять синхронную функцию, сохраняя её результат и исключения."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with measure(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def profile_run[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Начать новый профиль и вывести накопленные замеры даже при сбое."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        metrics = RunMetrics()
        token = _current.set(metrics)
        started = perf_counter()
        completed = False
        try:
            result = function(*args, **kwargs)
            completed = True
            return result
        finally:
            elapsed = perf_counter() - started
            _current.reset(token)
            metrics.report(elapsed, completed)

    return wrapped
