# -*- coding: utf-8 -*-
"""
Справочники: пользователи портала, контакты, стадии воронок.

Ключевая роль модуля — резолв user_id -> ФИО, потому что именно по user_id
мы идентифицируем, кто фактически вёл диалог (а не ответственный за сделку).
Кэши лежат в data/ и обновляются при каждом запуске (пользователи/стадии)
или дозаполняются (контакты).
"""

import json
import os

import config


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------ users
def format_user_name(u):
    """'Иванов И. И.' из полей NAME/LAST_NAME/SECOND_NAME."""
    last = (u.get("LAST_NAME") or "").strip()
    first = (u.get("NAME") or "").strip()
    second = (u.get("SECOND_NAME") or "").strip()
    if not last and not first:
        return f"user_{u.get('ID')}"
    initials = ""
    if first:
        initials += first[0].upper() + "."
    if second:
        initials += second[0].upper() + "."
    return f"{last} {initials}".strip() if last else f"{first} {second}".strip()


def load_users(client):
    """Полная карта {user_id: {'name': ..., 'position': ...}}."""
    try:
        raw = client.call_list("user.get")
    except Exception:
        # на части порталов user.get не принимает filter — берём всех
        raw = client.call_list("user.get")
    users = {}
    for u in raw:
        uid = int(u["ID"])
        users[uid] = {
            "name": format_user_name(u),
            "position": u.get("WORK_POSITION") or "",
        }
    _save_json(os.path.join(config.DATA_DIR, "users_cache.json"), users)
    return users


class LazyUserMap(dict):
    """
    users_map с дозапросом неизвестных ID.

    Обычная карта содержит только то, что вернул user.get при загрузке.
    Если в поле встретился user_id, которого в карте нет (уволен до того,
    как его успели подтянуть, бот, пользователь экстранета), делаем
    точечный user.get по ID и кэшируем результат в data/users_extra.json.
    Неразрешимые ID тоже кэшируются (список missing) — повторных запросов
    к API по ним не будет.
    """

    def __init__(self, client, base):
        super().__init__(base or {})
        self.client = client
        self.path = os.path.join(config.DATA_DIR, "users_extra.json")
        cache = _load_json(self.path, {})
        self.extra = {}
        for k, v in (cache.get("resolved") or {}).items():
            try:
                self.extra[int(k)] = v
            except (TypeError, ValueError):
                continue
        self.update(self.extra)
        self._missing = set(cache.get("missing") or [])

    def get(self, uid, default=None):
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return default
        rec = super().get(uid)
        if rec is None and self.client is not None and uid not in self._missing:
            rec = self._fetch(uid)
        return rec if rec is not None else default

    def _fetch(self, uid):
        rec = None
        try:
            res = self.client.result("user.get", {"ID": uid})
            u = res if isinstance(res, dict) and res.get("ID") else \
                (res[0] if isinstance(res, list) and res else None)
            if u:
                rec = {"name": format_user_name(u),
                       "position": u.get("WORK_POSITION") or ""}
        except Exception:
            rec = None
        if rec:
            self[uid] = self.extra[uid] = rec
        else:
            self._missing.add(uid)
        _save_json(self.path, {
            "resolved": {str(k): v for k, v in self.extra.items()},
            "missing": sorted(self._missing),
        })
        return rec


def user_name(users_map, uid):
    if uid is None:
        return None
    rec = users_map.get(int(uid))
    return rec["name"] if rec else f"user_{uid}"


# ------------------------------------------------------------------ contacts
class ContactBook:
    """Ленивый справочник контактов с кэшем на диске."""

    def __init__(self, client):
        self.client = client
        self.path = os.path.join(config.DATA_DIR, "contacts_cache.json")
        self.cache = _load_json(self.path, {})

    def name(self, contact_id):
        if not contact_id:
            return None
        cid = str(contact_id)
        if cid not in self.cache:
            try:
                c = self.client.result("crm.contact.get", {"id": int(cid)}) or {}
                full = " ".join(
                    p for p in [
                        (c.get("LAST_NAME") or "").strip(),
                        (c.get("NAME") or "").strip(),
                    ] if p
                )
                self.cache[cid] = full or f"contact_{cid}"
            except Exception as e:  # контакт могли удалить
                self.cache[cid] = f"contact_{cid}"
            _save_json(self.path, self.cache)
        return self.cache[cid]


# ------------------------------------------------------------------ stages
def load_stages(client):
    """
    Карта {STATUS_ID: название стадии} по всем воронкам сделок.
    DEAL_STAGE для воронки 0, DEAL_STAGE_{id} для остальных.
    """
    stages = {}
    res = client.result("crm.category.list", {"entityTypeId": 2})
    # метод возвращает {"categories": [...]} либо сразу массив (зависит от версии)
    if isinstance(res, dict):
        categories = res.get("categories", [])
    else:
        categories = res or []
    cat_ids = [0] + [int(c["id"]) for c in categories if int(c.get("id", 0)) != 0]
    for cid in set(cat_ids):
        entity = "DEAL_STAGE" if cid == 0 else f"DEAL_STAGE_{cid}"
        try:
            for s in client.result("crm.status.list", {"filter": {"ENTITY_ID": entity}}) or []:
                stages[s["STATUS_ID"]] = s.get("NAME", s["STATUS_ID"])
        except Exception as e:
            # воронка могла быть удалена — не критично
            print(f"  ! стадии {entity}: {e}")
    _save_json(os.path.join(config.DATA_DIR, "stages.json"), stages)
    return stages


def stage_name(stages_map, stage_id):
    return stages_map.get(stage_id, stage_id)


# ------------------------------------------------------------------ sources / statuses
def load_status_book(client, entity_id):
    """
    Справочник статусов CRM: {STATUS_ID: название}.
    ENTITY_ID: SOURCE, DEAL_TYPE и т.п. (crm.status.list).
    """
    statuses = {}
    for s in client.result("crm.status.list", {"filter": {"ENTITY_ID": entity_id}}) or []:
        statuses[s["STATUS_ID"]] = s.get("NAME", s["STATUS_ID"])
    return statuses


def load_sources(client):
    """Источники CRM (SOURCE): PARTNER -> "РБ OFF, Существующий клиент..."."""
    sources = load_status_book(client, "SOURCE")
    _save_json(os.path.join(config.DATA_DIR, "sources.json"), sources)
    return sources


def load_lead_statuses(client):
    """Статусы лидов (STATUS_ID -> название), кэш data/lead_statuses.json."""
    statuses = load_status_book(client, "STATUS")
    _save_json(os.path.join(config.DATA_DIR, "lead_statuses.json"), statuses)
    return statuses


def source_name(sources_map, source_id):
    if not source_id:
        return None
    return sources_map.get(source_id, source_id)


def load_categories(client):
    """Воронки сделок: {id: название}. 0 -> 'Основная воронка'."""
    res = client.result("crm.category.list", {"entityTypeId": 2})
    if isinstance(res, dict):
        items = res.get("categories", [])
    else:
        items = res or []
    cats = {"0": "Основная воронка"}
    for c in items:
        cats[str(c.get("id"))] = c.get("name") or f"Воронка {c.get('id')}"
    return cats