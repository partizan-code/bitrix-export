# -*- coding: utf-8 -*-
"""
Постоянное хранилище артефактов сделок (data/store/).

Кумулятивная выгрузка: каждый новый архив содержит ВСЕ сделки,
но неизменные закрытые/технические не переобрабатываются — их готовые
артefакты (markdown-протокол, касания, индекс) переносятся из store
в новый снимок как есть. Store — единственное место, откуда берётся
«прошлая жизнь» сделки; export/ остаётся стейджингом текущего снимка.

Структура:
  data/store/deals/<deal_id>.md     — последний сгенерированный протокол
  data/store/tps/<deal_id>.jsonl    — касания сделки (последняя версия)
  data/store/index/<deal_id>.json   — stats-запись для deals_index
  data/store/deal/<deal_id>.json    — JSON сделки (нужен для имён файлов при переносе)
"""

import json
import logging
import os

import config

log = logging.getLogger("b24")

_KINDS = ("deals", "tps", "index", "deal")


def _dir(kind):
    d = os.path.join(config.DATA_DIR, "store", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _path(kind, deal_id):
    ext = {"deals": ".md", "tps": ".jsonl", "index": ".json", "deal": ".json"}[kind]
    return os.path.join(_dir(kind), f"{int(deal_id)}{ext}")


def save_deal(deal, md_path, touchpoints, stats):
    """Сохранить артефакты обработанной сделки в store (перезапись)."""
    did = int(deal["ID"])
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    with open(_path("deals", did), "w", encoding="utf-8") as f:
        f.write(md_text)
    with open(_path("tps", did), "w", encoding="utf-8") as f:
        for tp in touchpoints:
            f.write(json.dumps(tp, ensure_ascii=False) + "\n")
    with open(_path("index", did), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    with open(_path("deal", did), "w", encoding="utf-8") as f:
        json.dump(deal, f, ensure_ascii=False, default=str)


def has_deal(deal_id):
    """Полный набор артефактов сделки есть в store?"""
    return all(os.path.exists(_path(k, deal_id)) for k in _KINDS)


def load_deal(deal_id):
    with open(_path("deal", deal_id), "r", encoding="utf-8") as f:
        return json.load(f)


def load_md(deal_id):
    with open(_path("deals", deal_id), "r", encoding="utf-8") as f:
        return f.read()


def load_tps(deal_id):
    tps = []
    with open(_path("tps", deal_id), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tps.append(json.loads(line))
    return tps


def load_stats(deal_id):
    with open(_path("index", deal_id), "r", encoding="utf-8") as f:
        return json.load(f)