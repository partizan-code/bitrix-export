# -*- coding: utf-8 -*-
"""
Активности CRM: звонки, письма, встречи, комментарии таймлайна.

Звонки берём как CRM-активности (TYPE_ID=2) — это работает с любой телефонией
(штатной или внешней) и надёжно привязано к сделке:
  - RESPONSIBLE_ID -> кто фактически говорил (не ответственный сделки!)
  - DIRECTION      -> направление (1 входящий, 2 исходящий)
  - FILES          -> id записей разговоров на Диске
Транскрипцию сначала пробуем взять штатную (crm.activity.call.getTranscript —
AI-расшифровка Bitrix), если нет — помечаем звонок для Faster Whisper.
"""

import logging
import re
from datetime import datetime

import config
from api_client import BitrixError

log = logging.getLogger("b24")

ACTIVITY_DIRECTION = {1: "in", 2: "out"}  # направление дела: 1 входящее, 2 исходящее

PHONE_RE = re.compile(r"\+\d[\d\s\-()]{6,}\d")
DURATION_RES = [
    re.compile(r"(\d{1,2}):(\d{2}):(\d{2})"),          # 00:03:25
    re.compile(r"(\d+)\s*мин\w*\s*(\d+)?\s*сек", re.I),  # 3 мин 25 сек
]


def _parse_phone(subject):
    m = PHONE_RE.search(subject or "")
    if not m:
        return None
    return re.sub(r"[^\d+]", "", m.group(0))


def _parse_duration(text):
    """Длительность звонка из описания/параметров активности (best effort)."""
    if not text:
        return None
    m = DURATION_RES[0].search(text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = DURATION_RES[1].search(text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    return None


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _duration_from_times(start, end):
    """Запасной источник длительности: END_TIME - START_TIME активности."""
    s, e = _parse_dt(start), _parse_dt(end)
    if s and e and e > s:
        return int((e - s).total_seconds())
    return None


# ------------------------------------------------------------------ звонки
def get_deal_calls(client, deal_id, date_from=None):
    """
    Все звонки сделки — как CRM-активности (TYPE_ID=2).

    Источник выбран по активностям, а не по voximplant.statistic.get, потому что:
    - активность создаётся при любой телефонии (штатная, Mango/UIS/Zadarma и т.п.);
    - активность надёжно привязана к сделке (OWNER_TYPE_ID/OWNER_ID);
    - RESPONSIBLE_ID активности = сотрудник, который фактически говорил;
    - записи разговоров лежат в FILES активности (id файлов Диска).
    """
    flt = {"OWNER_TYPE_ID": 2, "OWNER_ID": int(deal_id), "TYPE_ID": 2}
    if date_from or config.DATE_FROM:
        flt[">=START_TIME"] = date_from or config.DATE_FROM
    acts = client.call_list("crm.activity.list", {
        "order": {"START_TIME": "ASC"},
        "filter": flt,
        "select": [
            "ID", "SUBJECT", "DESCRIPTION", "START_TIME", "END_TIME",
            "CREATED", "COMPLETED", "AUTHOR_ID", "RESPONSIBLE_ID",
            "DIRECTION", "PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_PARAMS",
            "COMMUNICATIONS", "FILES",
        ],
    })
    # часть порталов не отдаёт FILES в списочном методе — добираем точечно
    for act in acts:
        if "FILES" not in act:
            try:
                full = client.result("crm.activity.get", {"id": int(act["ID"])}) or {}
                act["FILES"] = full.get("FILES") or []
            except BitrixError as e:
                log.debug("activity.get(%s): %s", act["ID"], e)
                act["FILES"] = []
    return acts


def get_builtin_transcript(client, activity_id):
    """
    Штатная AI-расшифровка звонка. Возвращает текст или None
    (метод отдаёт null, если расшифровки нет / она ещё не готова).
    """
    if not activity_id:
        return None
    try:
        res = client.result("crm.activity.call.getTranscript",
                            {"activityId": int(activity_id)})
    except BitrixError as e:
        log.debug("getTranscript(%s): %s", activity_id, e)
        return None
    if isinstance(res, str):
        return res or None
    if isinstance(res, dict):
        return res.get("TRANSCRIPT") or res.get("transcript") or None
    return None


def normalize_call(act):
    """Активность звонка -> единый вид."""
    subject = act.get("SUBJECT") or ""
    descr = act.get("DESCRIPTION") or ""

    # id файлов записей из FILES (формат элемента: {"id": ...} или просто число)
    file_ids = []
    for f in act.get("FILES") or []:
        fid = f.get("id") if isinstance(f, dict) else f
        if fid:
            file_ids.append(int(fid))

    params = act.get("PROVIDER_PARAMS") or {}
    if not isinstance(params, dict):
        params = {}
    phone = (params.get("PHONE_NUMBER") or params.get("PHONE")
             or _parse_phone(subject))
    duration = (_parse_duration(descr) or _parse_duration(str(params))
                or _duration_from_times(act.get("START_TIME"), act.get("END_TIME")))

    try:
        direction_code = int(act.get("DIRECTION") or 0)
    except (TypeError, ValueError):
        direction_code = 0

    missed = ("пропущен" in (subject + descr).lower()
              or "missed" in (subject + descr).lower())

    # Запись берём в расчёт только для состоявшегося разговора
    # длиннее MIN_RECORDING_DURATION_SEC. Если длительность неизвестна,
    # но файл есть — звонок точно состоялся (иначе записи бы не было),
    # такую запись сохраняем.
    if missed or not file_ids:
        file_ids = []
    elif duration is not None and duration <= config.MIN_RECORDING_DURATION_SEC:
        file_ids = []

    uid = act.get("RESPONSIBLE_ID") or act.get("AUTHOR_ID")
    return {
        "source": "activity",
        "stat_id": None,
        "activity_id": act.get("ID"),
        "time": act.get("START_TIME") or act.get("CREATED"),
        "direction": ACTIVITY_DIRECTION.get(direction_code, "unknown"),
        "phone": phone,
        "duration_sec": duration,
        "recording_url": None,
        "file_ids": file_ids,
        "failed_code": "missed" if missed else "",
        # КТО ВЁЛ ДИАЛОГ — сотрудник, принявший/совершивший звонок,
        # а не ответственный за сделку
        "actor_user_id": int(uid) if uid else None,
        "subject": subject,
        "provider": act.get("PROVIDER_ID"),
        "comment": descr or None,
    }


# ------------------------------------------------------------------ письма / встречи
def get_activities(client, deal_id, type_ids):
    """Активности сделки заданных типов (1 встреча, 3 письмо)."""
    if not type_ids:
        return []
    return client.call_list("crm.activity.list", {
        "order": {"CREATED": "ASC"},
        "filter": {
            "OWNER_TYPE_ID": 2,  # сделка
            "OWNER_ID": int(deal_id),
            "@TYPE_ID": list(type_ids),
        },
        "select": [
            "ID", "TYPE_ID", "SUBJECT", "DESCRIPTION", "DESCRIPTION_TYPE",
            "START_TIME", "END_TIME", "CREATED", "COMPLETED",
            "AUTHOR_ID", "RESPONSIBLE_ID", "DIRECTION",
            "PROVIDER_ID", "PROVIDER_TYPE_ID",
            "COMMUNICATIONS",
        ],
    })


# ------------------------------------------------------------------ комментарии
def get_timeline_comments(client, deal_id):
    """Комментарии менеджеров в таймлайне сделки."""
    return client.call_list("crm.timeline.comment.list", {
        "order": {"CREATED": "ASC"},
        "filter": {"ENTITY_ID": int(deal_id), "ENTITY_TYPE": "deal"},
        "select": ["ID", "ENTITY_ID", "ENTITY_TYPE", "COMMENT", "AUTHOR_ID", "CREATED"],
    })