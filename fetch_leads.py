# -*- coding: utf-8 -*-
"""
Лиды: выборка, классификация по группам, метаданные полей.

Группы лидов (по семантике статуса STATUS_SEMANTIC_ID):
  Активные     — в работе (P);
  Завершенные  — ВСЕ закрытые (S — успех/конверсия и F — провал),
                 кроме технических;
  Технические  — закрытые, чей статус попал в LEAD_TECHNICAL_STATUS_IDS
                 или содержит ключевое слово LEAD_TECHNICAL_STATUS_KEYWORDS
                 (брак/мусор/дубли — не считаются проигранной продажей).

Касания лидов собираются теми же модулями, что у сделок
(fetch_activities / fetch_openlines / fetch_tasks) с owner_type=1.
"""

import json
import logging
import os

import config
from fetch_deals import TECH_TITLES

log = logging.getLogger("b24")

LEAD_SELECT = ["*", "UF_*"]

# OWNER_TYPE_ID сущностей (crm.activity.*, stagehistory)
OWNER_TYPE = {"lead": 1, "deal": 2}


def period_filter_active():
    """Общий с сделками явный период по DATE_CREATE."""
    return bool(config.EXPORT_PERIOD_FROM and config.EXPORT_PERIOD_TO)


def get_leads(client):
    """
    Список лидов согласно config.LEAD_IDS / DATE_FROM / LEADS_LIMIT.
    Фильтр по дате создания — тот же, что у сделок (общий DATE_FROM
    или явный период EXPORT_PERIOD_FROM/TO).
    """
    flt = {}
    if config.LEAD_IDS:
        flt["@ID"] = [int(i) for i in config.LEAD_IDS]
    if config.DATE_FROM:
        flt[">=DATE_CREATE"] = config.DATE_FROM
    elif period_filter_active():
        flt[">=DATE_CREATE"] = f"{config.EXPORT_PERIOD_FROM}T00:00:00"
    if period_filter_active():
        flt["<=DATE_CREATE"] = f"{config.EXPORT_PERIOD_TO}T23:59:59"

    if len(flt.get("@ID", [])) == 1:
        lead = client.result("crm.lead.get", {"id": flt["@ID"][0]})
        return [lead] if lead else []

    log.info("Фильтр crm.lead.list: %s", flt or "(без фильтра)")
    leads = client.call_list("crm.lead.list", {
        "order": {"ID": "ASC"},
        "filter": flt,
        "select": LEAD_SELECT,
    })
    if config.LEADS_LIMIT:
        leads = leads[: config.LEADS_LIMIT]
    return leads


def get_leads_by_ids(client, ids):
    """Лиды по списку ID (повтор ранее упавших с ошибкой)."""
    ids = [int(i) for i in ids]
    if not ids:
        return []
    flt = {"@ID": ids}
    log.info("Фильтр crm.lead.list: %s", flt)
    return client.call_list("crm.lead.list", {
        "order": {"ID": "ASC"},
        "filter": flt,
        "select": LEAD_SELECT,
    })


def get_leads_incremental(client, changed_since):
    """
    Инкрементальная выборка (та же логика, что у сделок):
      ВСЕ АКТИВНЫЕ (семантика P)  +  всё изменённое с changed_since.
    Дубли между двумя запросами убираются по ID.
    DATE_FROM (если задан) ограничивает обе выборки по дате создания.
    """
    base = {}
    if config.LEAD_IDS:
        base["@ID"] = [int(i) for i in config.LEAD_IDS]
    if config.DATE_FROM:
        base[">=DATE_CREATE"] = config.DATE_FROM

    seen, leads = set(), []

    def _pull(extra):
        flt = dict(base)
        flt.update(extra)
        log.info("Фильтр crm.lead.list: %s", flt)
        rows = client.call_list("crm.lead.list", {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": LEAD_SELECT,
        })
        for d in rows:
            lid = int(d["ID"])
            if lid not in seen:
                seen.add(lid)
                leads.append(d)

    _pull({"STATUS_SEMANTIC_ID": "P"})          # все активные — всегда целиком
    _pull({">=DATE_MODIFY": changed_since})     # всё изменённое с прошлого снимка

    leads.sort(key=lambda d: int(d["ID"]))
    if config.LEADS_LIMIT:
        leads = leads[: config.LEADS_LIMIT]
    return leads


# ------------------------------------------------------------------ группы
def is_closed(lead):
    """Лид закрыт: семантика статуса S (успех/конверсия) или F (провал)."""
    return str(lead.get("STATUS_SEMANTIC_ID") or "").upper() in ("S", "F")


def is_technical(lead, lead_statuses):
    """
    Технический лид (брак/мусор/дубль) — не считается проигранной продажей.
    Правила (config):
    1) явный список LEAD_TECHNICAL_STATUS_IDS — точное совпадение STATUS_ID;
    2) ключевые слова LEAD_TECHNICAL_STATUS_KEYWORDS в названии статуса.
    """
    status_id = str(lead.get("STATUS_ID") or "")
    if status_id in (config.LEAD_TECHNICAL_STATUS_IDS or []):
        return True
    status_name = (lead_statuses.get(status_id) or status_id).lower()
    return any(kw.lower() in status_name
               for kw in (config.LEAD_TECHNICAL_STATUS_KEYWORDS or []))


def lead_group(lead, lead_statuses):
    """active | completed | technical"""
    if is_closed(lead) and is_technical(lead, lead_statuses):
        return "technical"
    return "completed" if is_closed(lead) else "active"


def display_name(lead):
    """Имя клиента по лиду: ФИО, иначе компания, иначе заголовок."""
    parts = [(lead.get("LAST_NAME") or "").strip(),
             (lead.get("NAME") or "").strip(),
             (lead.get("SECOND_NAME") or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or (lead.get("COMPANY_TITLE") or "").strip() \
        or (lead.get("TITLE") or "").strip() or None


# ------------------------------------------------------------------ метаданные полей
def load_lead_fields_meta(client):
    """
    Метаданные всех полей лида — crm.lead.fields.
    Формат тот же, что у fetch_deals.load_deal_fields_meta:
    {FIELD_CODE: {"title", "type", "statusType"?, "list"?}}
    """
    meta = {}
    fields = client.result("crm.lead.fields") or {}
    for name, f in fields.items():
        title = (f.get("listLabel") or f.get("formLabel") or f.get("filterLabel")
                 or f.get("title") or name)
        if title == name and name in TECH_TITLES:
            title = TECH_TITLES[name]
        m = {"title": title, "type": f.get("type") or ""}
        if f.get("statusType"):
            m["statusType"] = f["statusType"]
        items = f.get("items")
        if items:
            if isinstance(items, dict):
                items = items.values()
            lst = {}
            for i in items:
                iid = i.get("ID", i.get("id"))
                if iid is not None:
                    lst[str(iid)] = i.get("VALUE", i.get("value"))
            if lst:
                m["list"] = lst
        meta[name] = m

    path = os.path.join(config.DATA_DIR, "lead_fields_meta.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    return meta