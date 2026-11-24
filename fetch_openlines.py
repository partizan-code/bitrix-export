"""
Открытые линии: чаты мессенджеров (Viber, Telegram, WhatsApp, ...), привязанные к сделке.

Цепочка:
  imopenlines.v2.Session.list -> общий индекс доступных сессий за прогон
  imopenlines.crm.chat.get -> чаты сущности + связи из индекса сессий
  imopenlines.session.history.get -> все страницы каждого SESSION_ID

Идентификация автора сообщения:
  - senderid есть в справочнике пользователей портала -> менеджер (ФИО из справочника);
  - senderid помечен как bot/connector -> бот/коннектор;
  - иначе (senderid = 0 или внешний) -> клиент либо системное сообщение.
Так мы всегда знаем, КТО реально писал, даже если ответственный за сделку — другой человек.
"""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

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


SESSION_LIST = "imopenlines.v2.Session.list"
PAGE_SIZE = 200
MAX_OFFSET = 10000


class SessionListError(BitrixError):
    """Неполный список сессий нельзя выдавать за отсутствие переписки."""

    def __init__(self, reason: str):
        super().__init__(
            SESSION_LIST, reason, "Не удалось подтвердить полноту списка сессий"
        )


def stamp(value: str) -> datetime:
    """Разобрать дату API с обязательным часовым поясом."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Дата сессии без часового пояса")
    return result.astimezone(UTC)


def list_page(client, params: dict) -> tuple[list[dict], bool]:
    """Разобрать статистику без стандартного call_list/start/next."""
    result = client.result(SESSION_LIST, params)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("sessions"), list)
        or type(result.get("hasNextPage")) is not bool
    ):
        raise SessionListError("INVALID_SESSION_LIST")
    rows = result["sessions"]
    if result["hasNextPage"] and not rows:
        raise SessionListError("EMPTY_SESSION_PAGE")
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise TypeError
            for name in ("id", "chatId"):
                value = row[name]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, str))
                    or int(value) <= 0
                ):
                    raise ValueError
            stamp(row["dateCreate"])
        except (KeyError, TypeError, ValueError) as error:
            raise SessionListError("INVALID_SESSION_ROW") from error
    return rows, result["hasNextPage"]


def collect_sessions(client, cutoff: datetime, *, max_calls=None) -> dict[int, dict]:
    """Пройти всю доступную историю, дробя периоды до 365 дней и offset до 10000."""
    if max_calls is None:
        max_calls = config.OPENLINES_SESSION_LIST_MAX_CALLS
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ValueError("Предел вызовов списка сессий должен быть положительным целым")
    calls = 0

    def page(params):
        nonlocal calls
        if calls >= max_calls:
            raise SessionListError("SESSION_LIST_CALL_LIMIT")
        calls += 1
        return list_page(client, params)

    first, _ = page(
        {"order": "dateCreate", "orderDirection": "asc", "limit": 1, "offset": 0},
    )
    if not first:
        return {}
    oldest = stamp(first[0]["dateCreate"]).replace(microsecond=0)
    if oldest > cutoff:
        raise SessionListError("INVALID_OLDEST_SESSION")
    sessions = {}

    def window(start: datetime, end: datetime) -> None:
        offset, seen = 0, set()
        while True:
            rows, more = page(
                {
                    "dateCreateFrom": start.isoformat(),
                    "dateCreateTo": end.isoformat(),
                    "order": "dateCreate",
                    "orderDirection": "asc",
                    "offset": offset,
                    "limit": PAGE_SIZE,
                },
            )
            ids = {int(r["id"]) for r in rows}
            if rows and not ids.difference(seen):
                raise SessionListError("REPEATED_SESSION_PAGE")
            seen.update(ids)
            for row in rows:
                if not start <= stamp(row["dateCreate"]) <= end:
                    raise SessionListError("SESSION_OUTSIDE_WINDOW")
                sid = int(row["id"])
                if sid in sessions and int(sessions[sid]["chatId"]) != int(
                    row["chatId"]
                ):
                    raise SessionListError("SESSION_CHAT_CHANGED")
                sessions[sid] = row
            if not more:
                return
            offset += len(rows)
            if offset > MAX_OFFSET:
                seconds = int((end - start).total_seconds())
                if seconds < 2:
                    raise SessionListError("SESSION_WINDOW_TOO_DENSE")
                middle = start + timedelta(seconds=seconds // 2)
                window(start, middle)
                window(middle, end)
                return

    start = oldest
    while start <= cutoff:
        end = min(start + timedelta(days=365), cutoff)
        window(start, end)
        if end == cutoff:
            break
        start = end
    return sessions


def clean_text(text):
    if not text:
        return ""
    return BB_RE.sub("", text).strip()


def get_deal_chats(client, deal_id, entity_type="deal"):
    """
    Чаты открытых линий, привязанные к сущности (deal/lead):
    [{'chat_id': 1763, 'channel': 'viber', 'connector_title': '...'}, ...]
    """
    method = "imopenlines.crm.chat.get"
    rows = client.result(
        method,
        {
            "CRM_ENTITY_TYPE": entity_type,
            "CRM_ENTITY": int(deal_id),
            "ACTIVE_ONLY": "N",
        },
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise BitrixError(
            method, "INVALID_CHAT_BINDINGS", "Ожидался список привязок чатов"
        )
    chats = []
    skipped = 0
    for r in rows:
        # у части сделок API отдаёт записи без CHAT_ID
        # (битые/удалённые сессии открытых линий) — пропускаем
        chat_id = r.get("CHAT_ID")
        if not chat_id:
            skipped += 1
            continue
        try:
            if (
                isinstance(chat_id, bool)
                or not isinstance(chat_id, (int, str))
                or int(chat_id) <= 0
            ):
                raise ValueError
            chat_id = int(chat_id)
        except (TypeError, ValueError) as error:
            raise BitrixError(
                method, "INVALID_CHAT_BINDINGS", "Некорректный CHAT_ID"
            ) from error
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


class OpenLinesHistory:
    """Общий для сделок/лидов индекс и кэш полных историй только текущего прогона."""

    def __init__(self, client):
        self.client = client
        self.by_chat = None
        self.by_entity = defaultdict(dict)
        self.histories = {}
        self.index_error = None

    def _load_index(self) -> None:
        if self.index_error is not None:
            raise self.index_error
        if self.by_chat is not None:
            return
        log.info("Открытые линии: загружаю общий список доступных сессий")
        try:
            sessions = collect_sessions(self.client, datetime.now(UTC))
            by_chat = defaultdict(list)
            by_entity = defaultdict(dict)
            for sid, row in sessions.items():
                cid = int(row["chatId"])
                by_chat[cid].append(sid)
                entity = row.get("crmEntityType")
                if entity in ("deal", "lead") and row.get("crmEntityId"):
                    try:
                        did = int(row["crmEntityId"])
                        if isinstance(row["crmEntityId"], bool) or did < 1:
                            raise ValueError
                    except (TypeError, ValueError) as error:
                        raise SessionListError("INVALID_SESSION_CRM_ID") from error
                    conn = row.get("source") or "unknown"
                    if not isinstance(conn, str):
                        raise SessionListError("INVALID_SESSION_SOURCE")
                    by_entity[entity, did][cid] = {
                        "chat_id": cid,
                        "channel": CONNECTOR_CHANNEL.get(conn, conn),
                    }
            self.by_chat, self.by_entity = by_chat, by_entity
            log.info(
                "Открытые линии: доступно {} сессий в {} чатах",
                len(sessions),
                len(by_chat),
            )
        except BitrixError as error:
            self.index_error = error
            raise

    def get_entity_chats(self, entity_id: int, entity_type="deal") -> list[dict]:
        """Объединить текущие CRM-привязки со связями в доступных сессиях."""
        self._load_index()
        chats = dict(self.by_entity.get((entity_type, int(entity_id)), {}))
        for chat in get_deal_chats(self.client, entity_id, entity_type):
            chats[chat["chat_id"]] = chat
        return [chats[cid] for cid in sorted(chats)]

    def get_chat_history(self, chat_id: int) -> tuple[list[dict], dict, list[int]]:
        """Вернуть все сессии чата; неполная история в кэш не попадает."""
        self._load_index()
        cid = int(chat_id)
        if cid in self.histories:
            return self.histories[cid]
        sids = sorted(self.by_chat.get(cid, []))
        if not sids:
            raise SessionListError("CHAT_WITHOUT_VISIBLE_SESSIONS")
        messages, users = {}, {}
        for sid in sids:
            page_messages, info = get_chat_history(self.client, cid, session_id=sid)
            messages.update({int(msg["id"]): msg for msg in page_messages})
            users.update(info)
        result = (
            sorted(messages.values(), key=lambda m: (m["time"] or "", int(m["id"]))),
            users,
            sids,
        )
        self.histories[cid] = result
        log.debug("Чат {}: сессий {}, сообщений {}", cid, len(sids), len(messages))
        return result


def get_chat_history(client, chat_id, max_pages=None, *, session_id=None):
    """
    Все страницы выбранной сессии чата; без session_id — последней сессии.

    Перечень сессий выбирает OpenLinesHistory. Ошибка API на любой странице,
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

# Шаблоны подтверждены сохранённой историей ОЛ, а не контрактом REST.
# Полное совпадение и sender_id=0 ограничивают отсев служебными уведомлениями.
SYSTEM_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"начат новый диалог №\s*\d+\.?",
        r"обращение (?:также )?направлено (?:на )?\S.+",
        r".+ (?:начал[аи]?|завершил[аи]?) работу с диалогом\.?",
        r".+ завершил[аи]? текущий диалог\.?",
        r".+ переадресовал[аи]? диалог на \S.+",
        r"к чату присоединил(?:ся|ась) \S.+",
        r"автоматическое завершение диалога\.?",
        r"диалог заверш[её]н\.?",
        r"диалог снят с оператора, так как оператор уволен\.?",
        r"у .+ начался рабочий день\. теперь этот оператор тоже может видеть обращение\.?",
        r"в данном канале у оператора ограничено время на ответ\. подробнее в статье\.?",
    )
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
    """Отсеять известные уведомления ОЛ, сохранив обычный текст отправителя 0."""
    if str(msg.get("sender_id")) != "0":
        return False
    text = " ".join(clean_text(msg.get("text") or "").casefold().split())
    params = msg.get("params") or {}
    if params.get("attach"):  # карточки «создан лид» и т.п.
        return True
    return any(h in text for h in SYSTEM_HINTS) or any(
        pattern.fullmatch(text) for pattern in SYSTEM_PATTERNS
    )
