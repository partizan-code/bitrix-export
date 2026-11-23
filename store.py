"""
Постоянное хранилище артефактов сделок (data/store/).

Кумулятивная выгрузка: каждый новый архив содержит ВСЕ сделки,
но неизменные закрытые/технические не переобрабатываются — их готовые
артefакты (markdown-протокол, касания, индекс) переносятся из store
в новый снимок как есть. Store — единственное место, откуда берётся
«прошлая жизнь» сделки; export/ остаётся стейджингом текущего снимка.

Структура (сделки — в корне store, лиды — в подпапке lead/):
  data/store/deals/<id>.md          — последний сгенерированный протокол
  data/store/tps/<id>.jsonl         — касания (последняя версия)
  data/store/index/<id>.json        — stats-запись для индекса
  data/store/deal/<id>.json         — полный JSON сущности
  data/store/lead/{deals,tps,index,deal}/<id>.*  — то же для лидов
"""

import json
import os

import config

_KINDS = ("deals", "tps", "index", "deal")


def _dir(kind, entity="deal"):
    base = os.path.join(config.DATA_DIR, "store")
    d = (
        os.path.join(base, kind)
        if entity == "deal"
        else os.path.join(base, entity, kind)
    )
    os.makedirs(d, exist_ok=True)
    return d


def _path(kind, deal_id, entity="deal"):
    ext = {"deals": ".md", "tps": ".jsonl", "index": ".json", "deal": ".json"}[kind]
    return os.path.join(_dir(kind, entity), f"{int(deal_id)}{ext}")


def save_deal(deal, md_path, touchpoints, stats, entity="deal"):
    """Сохранить артефакты обработанной сущности в store (перезапись)."""
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    save_artifacts(deal, md_text, touchpoints, stats, entity=entity)


def save_artifacts(deal, md_text, touchpoints, stats, entity="deal"):
    """Перезаписать артефакты сущности напрямую (без чтения md с диска)."""
    did = int(deal["ID"])
    with open(_path("deals", did, entity), "w", encoding="utf-8") as f:
        f.write(md_text)
    path = _path("tps", did, entity)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps(tp, ensure_ascii=False) + "\n" for tp in touchpoints
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    with open(_path("index", did, entity), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    with open(_path("deal", did, entity), "w", encoding="utf-8") as f:
        json.dump(deal, f, ensure_ascii=False, default=str)


def has_deal(deal_id, entity="deal"):
    """Полный набор артефактов сущности есть в store?"""
    return all(os.path.exists(_path(k, deal_id, entity)) for k in _KINDS)


def load_deal(deal_id, entity="deal"):
    with open(_path("deal", deal_id, entity), "r", encoding="utf-8") as f:
        return json.load(f)


def load_md(deal_id, entity="deal"):
    with open(_path("deals", deal_id, entity), "r", encoding="utf-8") as f:
        return f.read()


def load_tps(deal_id, entity="deal"):
    tps = []
    with open(_path("tps", deal_id, entity), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tps.append(json.loads(line))
    return tps


def load_stats(deal_id, entity="deal"):
    with open(_path("index", deal_id, entity), "r", encoding="utf-8") as f:
        return json.load(f)


def stored_ids(entity="deal"):
    """ID всех сущностей с полным набором артефактов в store."""
    d = _dir("deal", entity)
    out = []
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            try:
                out.append(int(fn[:-5]))
            except ValueError:
                continue
    return sorted(out)
