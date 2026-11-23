"""
Реестр выгруженных сущностей (data/registry.json).

Зачем: еженедельные пакеты без дублей. Сделка/лид, один раз выгруженная
в закрытой или технической стадии, больше не попадает в следующие
инкрементальные выгрузки (см. config.REEXPORT_CLOSED_ON_CHANGE).

Ключи: сделки — "<id>" (совместимость со старыми реестрами),
лиды — "lead:<id>".

Формат записи:
{
  "group": "active|completed|technical",
  "entity": "deal|lead",
  "stage_id": "C2:WON" (для лидов — STATUS_ID),
  "date_modify": "2026-07-30T14:02:11+03:00",
  "exported_at": "EXP_20260803T083000+0300",
  "snapshot_at": "2026-08-03T08:30:00+03:00",
  "schema_version": 2
}
"""

import json
import logging
import os

import config

log = logging.getLogger("b24")

CLOSED_GROUPS = ("completed", "technical")


def key(entity, entity_id):
    """Ключ реестра: сделки — '<id>' (совместимость), лиды — 'lead:<id>'."""
    return str(int(entity_id)) if entity == "deal" else f"lead:{int(entity_id)}"


def parse_key(k):
    """Ключ реестра -> (entity, id). Голое число — сделка (legacy)."""
    if str(k).startswith("lead:"):
        return "lead", int(str(k).split(":", 1)[1])
    return "deal", int(k)


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


def is_duplicate_closed(deal, group, reg, entity="deal"):
    """
    True, если сущность уже выгружалась закрытой/технической и по правилам
    конфига повторно выгружаться не должна.
    """
    rec = reg.get(key(entity, deal.get("ID")))
    if not rec or rec.get("group") not in CLOSED_GROUPS:
        return False
    if group not in CLOSED_GROUPS:
        return False
    if config.REEXPORT_CLOSED_ON_CHANGE:
        # перевыгружаем только если сущность изменилась с прошлого раза
        return rec.get("date_modify") == (deal.get("DATE_MODIFY") or "")
    return True


def can_reuse(rec, schema_version):
    """
    Запись реестра позволяет перенести сущность без переобработки:
    артефакты сгенерированы по актуальной версии схемы.
    """
    return rec.get("schema_version") == int(schema_version)


def mark(reg, deal, group, ctx, entity="deal"):
    """Отметить сущность как выгруженную в текущем снимке."""
    stage_id = deal.get("STAGE_ID") if entity == "deal" else deal.get("STATUS_ID")
    reg[key(entity, deal["ID"])] = {
        "group": group,
        "entity": entity,
        "stage_id": stage_id or "",
        "date_modify": deal.get("DATE_MODIFY") or "",
        "exported_at": ctx.export_id,
        "snapshot_at": ctx.started.isoformat(timespec="seconds"),
        "schema_version": int(ctx.schema_version),
    }
