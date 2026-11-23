"""
Диагностика фильтра сделок: сколько сделок портал вернёт
с текущим фильтром DATE_FROM и без него.

Запуск: python scripts/diag_filter.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from api_client import B24Client
from logging_setup import run_cli


def main():
    c = B24Client()
    c.ping()

    base = {"order": {"ID": "ASC"}, "select": ["ID"]}

    r_all = c.call("crm.deal.list", dict(base))
    print("Без фильтра:            total =", r_all.get("total"))

    flt = {}
    if config.DATE_FROM:
        flt[">=DATE_CREATE"] = config.DATE_FROM
    print("Фильтр из config:       ", flt or "(пусто)")
    r_flt = c.call("crm.deal.list", {**base, "filter": flt})
    print("С фильтром DATE_FROM:   total =", r_flt.get("total"))

    # контроль: та же дата без времени и таймзоны (некоторые порталы капризны)
    if config.DATE_FROM:
        short = config.DATE_FROM[:10]
        r_short = c.call("crm.deal.list", {**base, "filter": {">=DATE_CREATE": short}})
        print(f"Контроль ('{short}'): total =", r_short.get("total"))


if __name__ == "__main__":
    run_cli(main)
