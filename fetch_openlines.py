"""
Открытые линии: чаты мессенджеров (Viber, Telegram, WhatsApp, ...), привязанные к сделке.

Цепочка:
  imopenlines.crm.chat.get   -> чаты сделки (CHAT_ID + тип коннектора)
  imopenlines.session.history.get -> все страницы последней сессии чата

Идентификация автора сообщения:
  - senderid есть в справочнике пользователей портала -> менеджер (ФИО из справочника);
  - senderid помечен как bot/connector -> бот/коннектор;
  - иначе (senderid = 0 или внешний) -> клиент либо системное сообщение.
Так мы всегда знаем, КТО реально писал, даже если ответственный за сделку — другой человек.
"""

import re

import config
from api_client import BitrixError
from logging_setup import log

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


def get_deal_chats(client, deal_id, entity_type="deal"):
    """
    Чаты открытых линий, привязанные к сущности (deal/lead):
    [{'chat_id': 1763, 'channel': 'viber', 'connector_title': '...'}, ...]
    """
    try:
        rows = (
            client.result(
                "imopenlines.crm.chat.get",
                {
                    "CRM_ENTITY_TYPE": entity_type,
                    "CRM_ENTITY": int(deal_id),
                    "ACTIVE_ONLY": "N",
                },
            )
            or []
        )
    except BitrixError as e:
        log.warning("{} {}: imopenlines.crm.chat.get: {}", entity_type, deal_id, e)
        return []
    chats = []
    skipped = 0
    for r in rows:
        # у части сделок API отдаёт записи без CHAT_ID
        # (битые/удалённые сессии открытых линий) — пропускаем
        chat_id = r.get("CHAT_ID")
        if not chat_id:
            skipped += 1
            continue
        conn = (r.get("CONNECTOR_ID") or "").lower()
        chats.append(
            {
                "chat_id": int(chat_id),
                "channel": CONNECTOR_CHANNEL.get(conn, conn or "unknown"),
                "connector_title": r.get("CONNECTOR_TITLE") or conn,
            }
        )
    if skipped:
        log.info(
            "{} {}: привязок чатов без CHAT_ID: {} (пропущено)",
            entity_type,
            deal_id,
            skipped,
        )
    return chats


def get_chat_history(client, chat_id, max_pages=None, *, session_id=None):
    """
    Все страницы выбранной сессии чата; без session_id — последней сессии.

    Предыдущие сессии этот путь не охватывает. Ошибка API на любой странице,
    некорректный курсор или предел страниц дают BitrixError, а не часть истории.
    Возвращает (messages, users_info):
      messages   — [{'id','sender_id','time','text','params'}] по возрастанию времени
      users_info — {'<id>': {'name':..., 'is_bot': bool}} из ответа API
    """
    if max_pages is None:
        max_pages = config.OPENLINES_SESSION_MAX_PAGES
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("Предел страниц сессии должен быть положительным целым")

    method = "imopenlines.session.history.get"
    params = {"CHAT_ID": int(chat_id)}
    messages, users_info, cursors = {}, {}, set()
    explicit_session = session_id is not None

    def incomplete(reason: str) -> BitrixError:
        return BitrixError(method, "INCOMPLETE_HISTORY", f"Чат {chat_id}: {reason}")

    def positive_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError
        result = int(value)
        if result < 1:
            raise ValueError
        return result

    if explicit_session:
        session_id = positive_int(session_id)
        params = {"SESSION_ID": session_id}

    for _ in range(max_pages):
        res = client.result(method, dict(params))

        if not isinstance(res, dict):
            raise incomplete("ожидался объект истории сессии")
        if explicit_session:
            try:
                same_chat = positive_int(res.get("chatId")) == int(chat_id)
            except (TypeError, ValueError):
                same_chat = False
            if not same_chat:
                raise incomplete("ответ относится к другому или неизвестному чату")
        if session_id is not None:
            try:
                same_session = positive_int(res.get("sessionId")) == session_id
            except (TypeError, ValueError):
                same_session = False
            if not same_session:
                raise incomplete("ответ относится к другой или неизвестной сессии")

        raw_msgs = res.get("message") or {}
        raw_users = res.get("users") or {}
        if not isinstance(raw_msgs, dict) or not isinstance(raw_users, dict):
            raise incomplete("некорректная структура сообщений или пользователей")
        for uid, u in raw_users.items():
            if not isinstance(u, dict):
                raise incomplete("некорректная запись пользователя")
            users_info[str(uid)] = {
                "name": u.get("name"),
                "is_bot": bool(u.get("bot")) or bool(u.get("connector")),
            }
        for m in raw_msgs.values():
            try:
                mid = positive_int(m.get("id"))
            except (AttributeError, TypeError, ValueError) as e:
                raise incomplete("некорректный ID сообщения") from e
            # Сохраняем форму сообщения; числовой ключ исключает дубли int/str.
            messages.setdefault(
                mid,
                {
                    "id": m.get("id"),
                    "sender_id": str(m.get("senderid", "0")),
                    "time": m.get("date"),
                    "text": clean_text(m.get("text") or m.get("textlegacy") or ""),
                    "params": m.get("params") or {},
                },
            )
        if res.get("hasNextPage") is False:
            result = sorted(
                messages.values(), key=lambda x: (x["time"] or "", int(x["id"]))
            )
            return result, users_info
        if res.get("hasNextPage") is not True:
            raise incomplete("отсутствует признак завершения страниц hasNextPage")
        try:
            if session_id is None:
                session_id = positive_int(res.get("sessionId"))
            cursor = (
                positive_int(res.get("nextId")),
                positive_int(res.get("nextDate")),
            )
        except (TypeError, ValueError) as e:
            raise incomplete("некорректный sessionId или курсор nextId/nextDate") from e
        if cursor in cursors:
            raise incomplete("повтор курсора nextId/nextDate")
        cursors.add(cursor)
        # Фиксируем сессию: CHAT_ID на следующей странице может выбрать уже новую.
        params = {
            "SESSION_ID": session_id,
            "LAST_ID": cursor[0],
            "LAST_DATE": cursor[1],
        }
    raise incomplete(f"достигнут предел страниц сессии ({max_pages})")


SYSTEM_HINTS = (
    "создан новый лид",
    "создана новая сделка",
    "оценка работы",
    "a new lead has been created",
    "рейтинг диалога",
)


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
                return {
                    "type": "bot",
                    "user_id": int(sid),
                    "name": info.get("name") or "bot",
                }
            return {
                "type": "manager",
                "user_id": int(sid),
                "name": info.get("name") or f"user_{sid}",
            }
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
