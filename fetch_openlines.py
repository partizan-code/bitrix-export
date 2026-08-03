# -*- coding: utf-8 -*-
"""
Открытые линии: чаты мессенджеров (Viber, Telegram, WhatsApp, ...), привязанные к сделке.

Цепочка:
  imopenlines.crm.chat.get   -> чаты сделки (CHAT_ID + тип коннектора)
  imopenlines.session.history.get -> история сообщений чата
      (fallback: im.dialog.messages.get, если первый метод недоступен)

Идентификация автора сообщения:
  - senderid есть в справочнике пользователей портала -> менеджер (ФИО из справочника);
  - senderid помечен как bot/connector -> бот/коннектор;
  - иначе (senderid = 0 или внешний) -> клиент либо системное сообщение.
Так мы всегда знаем, КТО реально писал, даже если ответственный за сделку — другой человек.
"""

import logging
import re

import config
from api_client import BitrixError

log = logging.getLogger("b24")

BB_RE = re.compile(r"\[/?[a-zA-Z][^\]]*\]")  # [b]..[/b], [br], [url=..] и т.п.

CONNECTOR_CHANNEL = {
    "livechat": "site_chat",
    "viber": "viber",
    "viberbot": "viber",
    "telegrambot": "telegram",
    "whatsappbyedna": "whatsapp",
    "wazzup24": "whatsapp",
    "vkgroup": "vk",
    "instagram": "instagram",
    "facebookcomments": "facebook",
    "avito": "avito",
    "olx": "olx",
}


def clean_text(text):
    if not text:
        return ""
    return BB_RE.sub("", text).strip()


def get_deal_chats(client, deal_id):
    """
    Чаты открытых линий, привязанные к сделке:
    [{'chat_id': 1763, 'channel': 'viber', 'connector_title': '...'}, ...]
    """
    try:
        rows = client.result("imopenlines.crm.chat.get", {
            "CRM_ENTITY_TYPE": "deal",
            "CRM_ENTITY": int(deal_id),
            "ACTIVE_ONLY": "N",
        }) or []
    except BitrixError as e:
        log.warning("Сделка %s: imopenlines.crm.chat.get: %s", deal_id, e)
        return []
    chats = []
    for r in rows:
        conn = (r.get("CONNECTOR_ID") or "").lower()
        chats.append({
            "chat_id": int(r["CHAT_ID"]),
            "channel": CONNECTOR_CHANNEL.get(conn, conn or "unknown"),
            "connector_title": r.get("CONNECTOR_TITLE") or conn,
        })
    return chats


def get_chat_history(client, chat_id):
    """
    История сообщений чата открытой линии.
    Возвращает (messages, users_info):
      messages   — [{'id','sender_id','time','text','params'}] по возрастанию времени
      users_info — {'<id>': {'name':..., 'is_bot': bool}} из ответа API
    """
    try:
        res = client.result("imopenlines.session.history.get", {"CHAT_ID": int(chat_id)})
    except BitrixError as e:
        log.warning("Чат %s: session.history.get: %s; fallback на im.dialog.messages.get",
                    chat_id, e)
        return _history_via_im(client, chat_id)

    if not res:
        return [], {}

    raw_msgs = res.get("message") or {}
    users_info = {}
    for uid, u in (res.get("users") or {}).items():
        users_info[str(uid)] = {
            "name": u.get("name"),
            "is_bot": bool(u.get("bot")) or bool(u.get("connector")),
        }

    messages = []
    for m in raw_msgs.values():
        messages.append({
            "id": m.get("id"),
            "sender_id": str(m.get("senderid", "0")),
            "time": m.get("date"),
            "text": clean_text(m.get("text") or m.get("textlegacy") or ""),
            "params": m.get("params") or {},
        })
    messages.sort(key=lambda x: (x["time"] or "", str(x["id"])))
    return messages, users_info


def _history_via_im(client, chat_id, max_pages=20):
    """Запасной путь: история через IM API (постранично, от новых к старым)."""
    messages, users_info = [], {}
    last_id = None
    try:
        for _ in range(max_pages):
            params = {"DIALOG_ID": f"chat{int(chat_id)}", "LIMIT": 200}
            if last_id:
                params["LAST_ID"] = last_id
            res = client.result("im.dialog.messages.get", params) or {}
            batch = res.get("messages")
            if isinstance(batch, dict):
                batch = list(batch.values())
            if not batch:
                break
            for m in batch:
                messages.append({
                    "id": m.get("id"),
                    "sender_id": str(m.get("author_id", "0")),
                    "time": m.get("date"),
                    "text": clean_text(m.get("text") or ""),
                    "params": m.get("params") or {},
                })
            for u in res.get("users") or []:
                users_info[str(u.get("id"))] = {
                    "name": u.get("name"),
                    "is_bot": bool(u.get("bot")),
                }
            last_id = batch[0].get("id")
            if len(batch) < 50:
                break
    except BitrixError as e:
        log.warning("Чат %s: im.dialog.messages.get: %s", chat_id, e)
    messages.sort(key=lambda x: (x["time"] or "", str(x["id"])))
    return messages, users_info


SYSTEM_HINTS = ("создан новый лид", "создана новая сделка", "оценка работы",
                "a new lead has been created", "рейтинг диалога")


def classify_sender(sender_id, users_map, users_info, client_name):
    """
    Кто написал сообщение.
    Приоритет: справочник пользователей портала -> users_info из API -> эвристики.
    Возвращает dict: {'type': 'manager'|'client'|'bot'|'system', 'name': ...}
    """
    sid = str(sender_id)
    if sid.isdigit() and int(sid) > 0:
        rec = users_map.get(int(sid))
        if rec:
            return {"type": "manager", "user_id": int(sid), "name": rec["name"]}
        info = users_info.get(sid)
        if info:
            if info.get("is_bot"):
                return {"type": "bot", "user_id": int(sid), "name": info.get("name") or "bot"}
            return {"type": "manager", "user_id": int(sid),
                    "name": info.get("name") or f"user_{sid}"}
    return {"type": "client", "name": client_name or "Клиент"}


def is_system_message(msg):
    """Служебные сообщения коннектора/CRM (не контент диалога)."""
    if msg["sender_id"] != "0":
        return False
    text = (msg.get("text") or "").lower()
    params = msg.get("params") or {}
    if params.get("attach"):  # карточки «создан лид» и т.п.
        return True
    return any(h in text for h in SYSTEM_HINTS)