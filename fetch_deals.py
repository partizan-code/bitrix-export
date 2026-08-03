# -*- coding: utf-8 -*-
"""
Сделки и история их движения по стадиям.

История стадий (crm.stagehistory.list) нужна для двух вещей:
1) события смены стадий вставляются в общую ленту касаний;
2) для каждого касания вычисляется, на какой стадии была сделка в тот момент.
"""

import config

DEAL_SELECT = [
    "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID",
    "CONTACT_ID", "COMPANY_ID", "OPPORTUNITY", "CURRENCY_ID",
    "DATE_CREATE", "CLOSEDATE", "CLOSED", "SOURCE_ID",
]


def get_deals(client):
    """Список сделок согласно config.DEAL_IDS / DATE_FROM / DEALS_LIMIT."""
    flt = {}
    if config.DEAL_IDS:
        flt["@ID"] = [int(i) for i in config.DEAL_IDS]
    if config.DATE_FROM:
        flt[">=DATE_CREATE"] = config.DATE_FROM

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