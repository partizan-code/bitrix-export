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
from users import user_name

DEAL_SELECT = ["*", "UF_*"]  # все стандартные + пользовательские поля


def period_filter_active():
    """Период задан явно -> он же является фильтром сделок по DATE_CREATE."""
    return bool(config.EXPORT_PERIOD_FROM and config.EXPORT_PERIOD_TO)


def get_deals(client):
    """
    Список сделок согласно config.DEAL_IDS / DATE_FROM / DEALS_LIMIT
    и флагам PROCESS_ACTIVE / PROCESS_COMPLETED (по признаку CLOSED).

    Фильтр по дате создания:
    - заданы ОБА EXPORT_PERIOD_FROM/TO -> >= и <= DATE_CREATE
      (одна одиночная граница периода игнорируется);
    - DATE_FROM (если задан) имеет приоритет над началом периода.
    """
    flt = {}
    if config.DEAL_IDS:
        flt["@ID"] = [int(i) for i in config.DEAL_IDS]
    if config.DATE_FROM:
        flt[">=DATE_CREATE"] = config.DATE_FROM
    elif period_filter_active():
        flt[">=DATE_CREATE"] = f"{config.EXPORT_PERIOD_FROM}T00:00:00"
    if period_filter_active():
        flt["<=DATE_CREATE"] = f"{config.EXPORT_PERIOD_TO}T23:59:59"
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


def get_deals_incremental(client, changed_since):
    """
    Недельная инкрементальная выборка:
      ALL ACTIVE  +  всё изменённое с changed_since
      (новые + изменённые + закрытые с прошлого снимка + технические).
    Дубли между двумя запросами убираются по ID.
    DATE_FROM (если задан) ограничивает обе выборки по дате создания —
    чтобы древние сделки не попадали в охват; для полного ALL ACTIVE
    без ограничений оставьте DATE_FROM = None.
    """
    base = {}
    if config.DEAL_IDS:
        base["@ID"] = [int(i) for i in config.DEAL_IDS]
    if config.DATE_FROM:
        base[">=DATE_CREATE"] = config.DATE_FROM

    seen, deals = set(), []

    def _pull(extra):
        flt = dict(base)
        flt.update(extra)
        rows = client.call_list("crm.deal.list", {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": DEAL_SELECT,
        })
        for d in rows:
            did = int(d["ID"])
            if did not in seen:
                seen.add(did)
                deals.append(d)

    if config.PROCESS_ACTIVE:
        _pull({"CLOSED": "N"})                      # все активные — всегда целиком
    _pull({">=DATE_MODIFY": changed_since})         # всё изменённое с прошлого снимка

    deals.sort(key=lambda d: int(d["ID"]))
    if config.DEALS_LIMIT:
        deals = deals[: config.DEALS_LIMIT]
    return deals


def is_closed(deal):
    return str(deal.get("CLOSED") or "").upper() == "Y"


# ------------------------------------------------------------------ группа "technical"
def is_technical(deal, stages_map):
    """
    Техническая сделка (дубль/тест/мусор) — не считается проигранной продажей.
    Правила (config):
    1) явный список TECHNICAL_STAGE_IDS[ID воронки] — точное совпадение STAGE_ID;
    2) ключевые слова TECHNICAL_STAGE_KEYWORDS в названии текущей стадии.
    """
    stage_id = str(deal.get("STAGE_ID") or "")
    cat = str(deal.get("CATEGORY_ID") or "0")
    explicit = (config.TECHNICAL_STAGE_IDS.get(cat)
                or config.TECHNICAL_STAGE_IDS.get("*") or [])
    if stage_id in explicit:
        return True
    stage_name = (stages_map.get(stage_id) or stage_id).lower()
    return any(kw.lower() in stage_name
               for kw in (config.TECHNICAL_STAGE_KEYWORDS or []))


def deal_group(deal, stages_map):
    """active | completed | technical"""
    if is_technical(deal, stages_map):
        return "technical"
    return "completed" if is_closed(deal) else "active"


# ------------------------------------------------------------------ метаданные полей
# Известные технические title у стандартных полей — подменяем человекочитаемыми
TECH_TITLES = {
    "MOVED_BY_ID": "Кем передвинута",
    "MOVED_TIME": "Когда передвинута",
    "LAST_ACTIVITY_TIME": "Время последней активности",
    "LAST_ACTIVITY_BY": "Автор последней активности",
    "LAST_COMMUNICATION_TIME": "Время последней коммуникации",
    "REPEAT_SALE_SEGMENT_ID": "Сегмент повторных продаж",
}


def load_deal_fields_meta(client):
    """
    Метаданные всех полей сделки одним методом — crm.deal.fields
    (он же отдаёт и UF-поля с isDynamic=true).

    Название поля берём по приоритету: listLabel -> formLabel -> filterLabel -> title,
    т.к. у UF-полей в title лежит технический код, а человекочитаемое имя — в *Label.
    Enum-варианты — из items (ключи ID/VALUE или id/value в зависимости от версии).

    {FIELD_CODE: {"title", "type", "statusType"?, "list"?}}
    """
    meta = {}
    fields = client.result("crm.deal.fields") or {}
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

    path = os.path.join(config.DATA_DIR, "deal_fields_meta.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    return meta


def _bool_label(v):
    s = str(v).strip().lower()
    if s in ("1", "y", "true", "да"):
        return "Да"
    if s in ("0", "n", "false", "нет"):
        return "Нет"
    return v


_BOOLISH = ("true", "false")

# STAGE_SEMANTIC_ID: семантика стадии (P/S/F)
SEMANTIC_LABELS = {"P": "В работе", "S": "Успешно завершена", "F": "Провал"}


def resolve_deal_fields(deal, fields_meta, users_map=None,
                        status_maps=None, category_map=None):
    """
    Все непустые поля сделки -> {CODE: {"title": ..., "value": ...}}.
    Резолв: enum-списки -> названия, crm_status (Тип, Стадия, Источник) -> названия,
    воронка -> название, поля-пользователи -> ФИО, boolean/char -> Да/Нет.
    """
    status_maps = status_maps or {}
    out = {}
    for name, value in deal.items():
        if name.startswith("_"):  # служебные наши поля
            continue
        if value in (None, "", [], {}):
            continue
        m = fields_meta.get(name, {})
        ftype = m.get("type") or ""
        display = value
        lst = m.get("list")
        if lst:
            vals = value if isinstance(value, list) else [value]
            display = ", ".join(lst.get(str(v), str(v)) for v in vals)
        elif ftype == "crm_status" and m.get("statusType") in status_maps:
            smap = status_maps[m["statusType"]]
            vals = value if isinstance(value, list) else [value]
            display = ", ".join(smap.get(str(v), str(v)) for v in vals)
        elif ftype == "crm_category" and category_map:
            display = category_map.get(str(value), str(value))
        elif name == "STAGE_SEMANTIC_ID":
            display = SEMANTIC_LABELS.get(str(value), value)
        elif ftype in ("employee", "user") and users_map:
            ids = value if isinstance(value, list) else [value]
            names = [user_name(users_map, int(v)) for v in ids
                     if str(v).lstrip("-").isdigit()]
            names = [n for n in names if n]
            if names:
                display = ", ".join(names)
        elif (ftype == "boolean" or isinstance(value, bool)
              or str(value).strip().lower() in _BOOLISH):
            display = _bool_label(value)
        elif ftype == "char" and str(value).strip() in ("Y", "N"):
            display = _bool_label(value)
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