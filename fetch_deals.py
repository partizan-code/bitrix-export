# -*- coding: utf-8 -*-
"""
Сделки и история их движения по стадиям.

История стадий (crm.stagehistory.list) нужна для двух вещей:
1) события смены стадий вставляются в общую ленту касаний;
2) для каждого касания вычисляется, на какой стадии была сделка в тот момент.
"""

import json
import os

import config

DEAL_SELECT = ["*", "UF_*"]  # все стандартные + пользовательские поля


def get_deals(client):
    """
    Список сделок согласно config.DEAL_IDS / DATE_FROM / DEALS_LIMIT
    и флагам PROCESS_ACTIVE / PROCESS_COMPLETED (по признаку CLOSED).
    """
    flt = {}
    if config.DEAL_IDS:
        flt["@ID"] = [int(i) for i in config.DEAL_IDS]
    if config.DATE_FROM:
        flt[">=DATE_CREATE"] = config.DATE_FROM
    # фильтр по завершённости, если включена только одна группа
    if config.PROCESS_ACTIVE and not config.PROCESS_COMPLETED:
        flt["CLOSED"] = "N"
    elif config.PROCESS_COMPLETED and not config.PROCESS_ACTIVE:
        flt["CLOSED"] = "Y"

    if len(flt.get("@ID", [])) == 1:
        # одна сделка — дешевле взять напрямую
        deal = client.result("crm.deal.get", {"id": flt["@ID"][0]})
        return [deal] if deal else []

    deals = client.call_list("crm.deal.list", {
        "order": {"ID": "ASC"},
        "filter": flt,
        "select": DEAL_SELECT,
    })
    if config.DEALS_LIMIT:
        deals = deals[: config.DEALS_LIMIT]
    return deals


def is_closed(deal):
    return str(deal.get("CLOSED") or "").upper() == "Y"


# ------------------------------------------------------------------ метаданные полей
def load_deal_fields_meta(client):
    """
    Метаданные всех полей сделки:
    {FIELD_CODE: {"title": "...", "type": "...", "list": {id: название}}}
    Стандартные поля — crm.deal.fields, пользовательские — crm.deal.userfield.list.
    """
    meta = {}

    std = client.result("crm.deal.fields") or {}
    for name, f in std.items():
        m = {"title": f.get("title") or name, "type": f.get("type") or ""}
        items = f.get("items")
        if items:
            if isinstance(items, dict):
                items = items.values()
            m["list"] = {str(i.get("id")): i.get("value") for i in items if i.get("id")}
        meta[name] = m

    rows = client.result("crm.deal.userfield.list") or []
    if isinstance(rows, dict):
        rows = rows.values()
    for f in rows:
        name = f.get("FIELD_NAME")
        if not name:
            continue
        m = {
            "title": (f.get("LIST_COLUMN_LABEL") or f.get("EDIT_FORM_LABEL")
                      or name),
            "type": f.get("USER_TYPE_ID") or "",
        }
        lst = f.get("LIST") or []
        if isinstance(lst, dict):
            lst = lst.values()
        if lst:
            m["list"] = {str(i.get("ID")): i.get("VALUE") for i in lst if i.get("ID")}
        meta[name] = m

    path = os.path.join(config.DATA_DIR, "deal_fields_meta.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    return meta


def resolve_deal_fields(deal, fields_meta):
    """
    Все непустые поля сделки -> {CODE: {"title": ..., "value": ...}}.
    Enum-поля (стандартные items и UF-списки) резолвятся в названия.
    """
    out = {}
    for name, value in deal.items():
        if name.startswith("_"):  # служебные наши поля
            continue
        if value in (None, "", [], {}):
            continue
        m = fields_meta.get(name, {})
        display = value
        lst = m.get("list")
        if lst:
            if isinstance(value, list):
                display = ", ".join(lst.get(str(v), str(v)) for v in value)
            else:
                display = lst.get(str(value), str(value))
        out[name] = {"title": m.get("title") or name, "value": display}
    return out


def get_stage_history(client, deal_id):
    """
    Хронология смены стадий сделки:
    [{'stage_id': 'NEW', 'time': '2026-07-10T09:15:00+03:00'}, ...] по возрастанию.
    """
    rows = client.call_list("crm.stagehistory.list", {
        "entityTypeId": 2,
        "order": {"ID": "ASC"},
        "filter": {"OWNER_ID": int(deal_id)},
        "select": ["ID", "STAGE_ID", "CREATED_TIME"],
    })
    return [
        {"stage_id": r.get("STAGE_ID"), "time": r.get("CREATED_TIME")}
        for r in rows if r.get("STAGE_ID")
    ]


def stage_at(history, ts):
    """
    На какой стадии была сделка в момент ts (строки ISO-8601 сравнимы,
    т.к. Bitrix отдаёт время с таймзоной в одном формате).
    """
    current = None
    for h in history:
        if h["time"] and ts and h["time"] <= ts:
            current = h["stage_id"]
        else:
            break
    return current