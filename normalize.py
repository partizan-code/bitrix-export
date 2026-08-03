# -*- coding: utf-8 -*-
"""
Нормализация: всё собранное -> единый формат «касание» (touchpoint).

Схема касания:
{
  "deal_id": 1234,
  "touchpoint_id": "call_991",
  "timestamp": "2026-07-15T14:32:10+03:00",
  "channel": "call|viber|telegram|whatsapp|email|comment|meeting|...",
  "event_type": "call|message|comment|email|meeting|stage_change",
  "direction": "in|out|internal|null",
  "actor": {"type": "manager|client|bot|system", "user_id": 17, "name": "Иванов И.И."},
  "responsible_user_id": 42,          # ответственный за сделку (для сравнения)
  "client": {"contact_id": 555, "name": "Петров П."},
  "deal_stage_at_time": {"stage_id": "...", "name": "..."},
  "content": {
      "type": "text|transcript",
      "text": "...",
      "duration_sec": 143,            # только звонки
      "recording_url": "...",         # только звонки
      "transcript_status": "builtin|whisper|no_recording|no_transcript|pending"
  },
  "source": {"activity_id": ..., "stat_id": ..., "chat_id": ..., "message_id": ...}
}
"""

from fetch_deals import stage_at
from users import user_name


def _base(deal, ts, channel, event_type):
    return {
        "deal_id": int(deal["ID"]),
        "timestamp": ts,
        "channel": channel,
        "event_type": event_type,
        "direction": None,
        "actor": {"type": "system", "user_id": None, "name": None},
        "responsible_user_id": int(deal["ASSIGNED_BY_ID"]) if deal.get("ASSIGNED_BY_ID") else None,
        "client": {
            "contact_id": int(deal["CONTACT_ID"]) if deal.get("CONTACT_ID") else None,
            "name": deal.get("_contact_name"),
        },
        "deal_stage_at_time": None,
        "content": {"type": "text", "text": ""},
        "source": {},
    }


def _fill_stage(tp, history, stages_map):
    sid = stage_at(history, tp["timestamp"])
    if sid:
        tp["deal_stage_at_time"] = {"stage_id": sid, "name": stages_map.get(sid, sid)}
    return tp


# ------------------------------------------------------------------ звонки
def tp_from_call(deal, call, users_map, history, stages_map, transcript_info):
    tp = _base(deal, call["time"], "call", "call")
    tp["touchpoint_id"] = f"call_{call.get('stat_id') or call.get('activity_id')}"
    tp["direction"] = call["direction"]

    uid = call.get("actor_user_id")
    tp["actor"] = {
        "type": "manager" if uid else "unknown",
        "user_id": uid,
        "name": user_name(users_map, uid) if uid else None,
    }

    text, status = transcript_info.get("text"), transcript_info.get("status")
    tp["content"] = {
        "type": "transcript" if text else "meta",
        "text": text or "",
        "phone": call.get("phone"),
        "duration_sec": call.get("duration_sec"),
        "failed_code": call.get("failed_code") or None,
        "recording_url": call.get("recording_url"),
        "audio_path": call.get("local_audio"),
        "transcript_status": status,
        "comment": call.get("comment"),
    }
    tp["source"] = {"activity_id": call.get("activity_id"),
                    "stat_id": call.get("stat_id"),
                    "file_ids": call.get("file_ids")}
    return _fill_stage(tp, history, stages_map)


# ------------------------------------------------------------------ чаты ОЛ
def tp_from_ol_message(deal, chat, msg, sender, users_map, history, stages_map):
    tp = _base(deal, msg["time"], chat["channel"], "message")
    tp["touchpoint_id"] = f"ol_{chat['chat_id']}_{msg['id']}"
    tp["actor"] = sender
    if sender["type"] == "manager":
        tp["direction"] = "out"
    elif sender["type"] == "client":
        tp["direction"] = "in"
    else:
        tp["direction"] = "internal"
    tp["content"] = {"type": "text", "text": msg["text"]}
    tp["source"] = {"chat_id": chat["chat_id"], "message_id": msg["id"]}
    return _fill_stage(tp, history, stages_map)


# ------------------------------------------------------------------ комментарии
def tp_from_comment(deal, row, users_map, history, stages_map):
    tp = _base(deal, row.get("CREATED"), "comment", "comment")
    tp["touchpoint_id"] = f"comment_{row['ID']}"
    uid = int(row["AUTHOR_ID"]) if row.get("AUTHOR_ID") else None
    tp["actor"] = {
        "type": "manager" if uid else "system",
        "user_id": uid,
        "name": user_name(users_map, uid) if uid else None,
    }
    tp["direction"] = "internal"
    tp["content"] = {"type": "text", "text": row.get("COMMENT") or ""}
    tp["source"] = {"comment_id": row["ID"]}
    return _fill_stage(tp, history, stages_map)


# ------------------------------------------------------------------ письма / встречи
def tp_from_activity(deal, act, users_map, history, stages_map):
    is_email = str(act.get("TYPE_ID")) == "3"
    channel = "email" if is_email else "meeting"
    tp = _base(deal, act.get("START_TIME") or act.get("CREATED"), channel,
               "email" if is_email else "meeting")
    tp["touchpoint_id"] = f"act_{act['ID']}"
    uid = int(act["AUTHOR_ID"]) if act.get("AUTHOR_ID") else None
    tp["actor"] = {
        "type": "manager" if uid else "system",
        "user_id": uid,
        "name": user_name(users_map, uid) if uid else None,
    }
    tp["direction"] = {1: "in", 2: "out"}.get(int(act.get("DIRECTION") or 0), "internal")
    subject = act.get("SUBJECT") or ""
    descr = act.get("DESCRIPTION") or ""
    tp["content"] = {
        "type": "text",
        "text": (subject + "\n" + descr).strip() if is_email else (subject or descr),
    }
    tp["source"] = {"activity_id": act["ID"]}
    return _fill_stage(tp, history, stages_map)


# ------------------------------------------------------------------ смена стадий
def tp_stage_events(deal, history, stages_map):
    """Смены стадий как системные события ленты."""
    events = []
    prev = None
    for i, h in enumerate(history):
        sid = h["stage_id"]
        tp = _base(deal, h["time"], "crm", "stage_change")
        tp["touchpoint_id"] = f"stage_{i}_{sid}"
        tp["actor"] = {"type": "system", "user_id": None, "name": None}
        tp["content"] = {
            "type": "meta",
            "text": "",
            "stage_from": {"stage_id": prev, "name": stages_map.get(prev)} if prev else None,
            "stage_to": {"stage_id": sid, "name": stages_map.get(sid, sid)},
        }
        tp["deal_stage_at_time"] = {"stage_id": sid, "name": stages_map.get(sid, sid)}
        events.append(tp)
        prev = sid
    return events