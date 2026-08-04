# -*- coding: utf-8 -*-
"""
Реестр выгруженных сделок (data/registry.json).

Зачем: еженедельные пакеты без дублей. Сделка, один раз выгруженная
в закрытой или технической стадии, больше не попадает в следующие
инкрементальные выгрузки (см. config.REEXPORT_CLOSED_ON_CHANGE).

Формат:
{
  "<deal_id>": {
    "group": "active|completed|technical",
    "stage_id": "C2:WON",
    "date_modify": "2026-07-30T14:02:11+03:00",
    "exported_at": "EXP_20260803T083000+0300",
    "snapshot_at": "2026-08-03T08:30:00+03:00"
  },
  ...
}
"""

import json
import logging
import os

import config

log = logging.getLogger("b24")

CLOSED_GROUPS = ("completed", "technical")


def _path():
    return os.path.join(config.DATA_DIR, "registry.json")


def load():
    """Реестр из файла; при отсутствии/битом файле — пустой."""
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("registry.json не прочитан (%s) — считаю реестр пустым", e)
        return {}


def save(reg):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)


def is_duplicate_closed(deal, group, reg):
    """
    True, если сделка уже выгружалась закрытой/технической и по правилам
    конфига повторно выгружаться не должна.
    """
    rec = reg.get(str(deal.get("ID")))
    if not rec or rec.get("group") not in CLOSED_GROUPS:
        return False
    if group not in CLOSED_GROUPS:
        return False
    if config.REEXPORT_CLOSED_ON_CHANGE:
        # перевыгружаем только если сделка изменилась с прошлого раза
        return rec.get("date_modify") == (deal.get("DATE_MODIFY") or "")
    return True


def can_reuse(rec, schema_version):
    """
    Запись реестра позволяет перенести сделку без переобработки:
    артефакты сгенерированы по актуальной версии схемы.
    """
    return rec.get("schema_version") == int(schema_version)


def mark(reg, deal, group, ctx):
    """Отметить сделку как выгруженную в текущем снимке."""
    reg[str(deal["ID"])] = {
        "group": group,
        "stage_id": deal.get("STAGE_ID") or "",
        "date_modify": deal.get("DATE_MODIFY") or "",
        "exported_at": ctx.export_id,
        "snapshot_at": ctx.started.isoformat(timespec="seconds"),
        "schema_version": int(ctx.schema_version),
    }