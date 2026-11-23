"""Разбор конечных Markdown-карточек с сохранением текста, времени и оформления."""

import re
from collections import Counter
from datetime import datetime

HEADER_COLUMNS = {
    "Ответственный": "responsible_name",
    "Клиент": "client_name",
    "Текущая стадия": "stage_name",
    "Текущий статус": "stage_name",
    "Источник": "source_name",
    "Страна": "country",
    "Создана": "created_at",
    "Сумма": "amount",
}
EVENT_START = re.compile(
    r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}|)\] "
    r"(?P<head>[^\r\n]*)",
    re.MULTILINE,
)
BULLET = re.compile(r"^- ([^\r\n]+?): ", re.MULTILINE)


def layout(text: str, spans: list[tuple[int, int, object]]) -> list:
    """Заменить значения ссылками; хранить оформление без копии самих значений."""
    result = []
    position = 0
    for start, end, column in sorted(spans, key=lambda item: item[0]):
        if start < position or end < start:
            raise ValueError("Пересекающиеся элементы карточки")
        if start > position:
            result.append(["literal", text[position:start]])
        result.append(["events" if column is None else "column", column])
        position = end
    if position < len(text):
        result.append(["literal", text[position:]])
    return result


def render_layout(parts: list, values: dict, events: str = "") -> str:
    """Восстановить карточку из колонок и ленты, не обращаясь к ZIP."""
    return "".join(
        value if kind == "literal" else events if kind == "events" else values[value]
        for kind, value in parts
    )


def event_details(head: str, body: str) -> dict:
    """Извлечь только сведения, явно показанные в заголовке и теле касания."""
    stage = re.search(r" \[стадия: (.*?)\]", head, re.DOTALL)
    result = {"stage_name": stage[1] if stage else None}
    clean = head[: stage.start()] + head[stage.end() :] if stage else head
    if " Звонок (" in clean:
        result.update(event_type="call", channel=clean.split(" Звонок (")[0])
        match = re.fullmatch(r".+? Звонок \((.*?)\) — (.*)", clean, re.DOTALL)
        if not match:
            raise ValueError("Не распознан заголовок звонка")
        details, actor = match.groups()
        result["actor_name"] = actor.split(" | запись: ")[0]
        direction = re.search(r"^(входящий|исходящий)", details)
        duration = re.search(r"(?:^|, )(\d+):(\d{2})$", details)
        result["direction"] = direction[1] if direction else None
        result["duration_seconds"] = (
            int(duration[1]) * 60 + int(duration[2]) if duration else None
        )
        files = re.search(r"\| запись: file_id ([\d, ]+)", clean)
        result["recording_file_ids"] = files[1].strip() if files else None
        audio = re.search(r" \(([^()]*)\)$", clean)
        if audio and not files and not re.search(r"\.[A-Za-z0-9]+$", audio[1]):
            audio = None
        result["audio_filename"] = audio[1] if audio else None
        if audio and not files:
            result["actor_name"] = result["actor_name"][: -(len(audio[1]) + 3)]
        failure = re.search(r"\(звонок не состоялся: ([^\r\n]*)\)", body)
        result["failure_code"] = failure[1] if failure else None
        result["transcript_status"] = (
            "present"
            if re.search(r"^Транскрипция:\r?$", body, re.MULTILINE)
            else "no_transcript"
            if "(запись есть, транскрибация не выполнена)" in body
            else None
        )
    elif " Задача — " in clean:
        channel, content = clean.split(" Задача — ", 1)
        actor, separator, title = content.partition(": ")
        if not separator:
            raise ValueError("Не распознан заголовок задачи")
        result.update(
            event_type="task", channel=channel, actor_name=actor, task_title=title
        )
        # Дополнение рендера располагается в конце после двух пробелов.
        extra = re.search(r"  \((.*)\)$", title, re.DOTALL)
        if extra:
            result["task_title"] = title[: extra.start()]
            details = extra[1]
            for label, column in (
                ("дедлайн", "deadline"),
                ("закрыта", "closed_at"),
            ):
                stamp = re.search(
                    rf"{label} (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}})", details
                )
                result[column] = stamp[1] if stamp else None
            creator = re.search(r"постановщик: (.*)$", details)
            result["created_by"] = creator[1] if creator else None
            first = details.split(", ")[0]
            result["task_status"] = (
                first
                if not first.startswith(("дедлайн ", "закрыта ", "постановщик: "))
                else None
            )
    elif clean.startswith("📌 Стадия: "):
        before, separator, after = clean[len("📌 Стадия: ") :].partition(" → ")
        if not separator:
            raise ValueError("Не распознана смена стадии")
        result.update(
            event_type="stage_change", channel="📌", stage_from=before, stage_to=after
        )
    else:
        channels = (
            "💬 чат сайта",
            "💬 Telegram",
            "💬 WhatsApp",
            "💬 Instagram",
            "💬 Viber",
            "💬 Avito",
            "💬 VK",
        )
        channel = next((c for c in channels if clean.startswith(c + " ")), None)
        if channel is None:
            channel = clean.partition(" ")[0]
        content = clean[len(channel) :].lstrip()
        actor, separator, _ = content.partition(":")
        if not separator:
            raise ValueError("Не распознан заголовок текстового касания")
        kind = {"📝": "comment", "✉️": "email", "🤝": "meeting"}.get(channel, "message")
        result.update(event_type=kind, channel=channel, actor_name=actor)
    return result


def parse_event(block: str) -> tuple[dict, str]:
    """Выделить событие и разделитель; секунды и ID не придумывать."""
    core = block.rstrip("\r\n")
    suffix = block[len(core) :]
    match = EVENT_START.match(core)
    if not match:
        raise ValueError("Не распознана временная метка касания")
    stamp, head = match["timestamp"], match["head"]
    if stamp:
        # В карточке нет часового пояса события; проверяем только дату и минуту.
        datetime.strptime(stamp, "%Y-%m-%d %H:%M")  # noqa: DTZ007
    rest = core[match.end() :]
    # Имена участников и названия задач в конечной карточке бывают многострочными.
    # Тело рендера имеет отступ, а маркер транскрипции — отдельную строку.
    while rest.startswith(("\n", "\r\n")):
        newline = "\r\n" if rest.startswith("\r\n") else "\n"
        continuation = rest[len(newline) :]
        line = continuation.partition("\n")[0].removesuffix("\r")
        if not line or line.startswith("  ") or line == "Транскрипция:":
            break
        head += newline + line
        rest = continuation[len(line) :]
    separator = (
        "\r\n" if rest.startswith("\r\n") else "\n" if rest.startswith("\n") else ""
    )
    body = rest[len(separator) :]
    event = {
        "occurred_at": stamp,
        "headline": head,
        "line_ending": separator,
        "text": body,
        **event_details(head, body),
    }
    if render_event(event) != core:
        raise ValueError("Не удалось восстановить касание")
    return event, suffix


def render_event(event: dict) -> str:
    """Восстановить событие, сохранив отступы и переносы текста."""
    return (
        f"[{event['occurred_at']}] {event['headline']}"
        f"{event['line_ending']}{event['text']}"
    )


def parse_card(text: str, entity: str, entity_id: int) -> dict:
    """Разложить карточку по колонкам и событиям; неизвестную структуру отклонить."""
    title = re.match(r"# (Сделка|Лид) #(\d+): ([^\r\n]*)", text)
    if (
        not title
        or int(title[2]) != entity_id
        or title[1] != {"deal": "Сделка", "lead": "Лид"}[entity]
    ):
        raise ValueError("Тип или ID заголовка не совпадает с именем карточки")
    timeline = re.search(r"^## Лента касаний\r?$", text, re.MULTILINE)
    if not timeline:
        raise ValueError("Отсутствует раздел ленты касаний")
    fields = re.search(
        r"^## Поля (сделки|лида)\r?$", text[timeline.end() :], re.MULTILINE
    )
    fields_start = timeline.end() + fields.start() if fields else len(text)
    header = text[: timeline.start()]
    crm = re.search(r"^\*\*CRM:\*\* ([^\r\n]*)", header, re.MULTILINE)
    if not crm:
        raise ValueError("Отсутствует CRM-ссылка")
    title_value = header[title.start(3) : crm.start()].rstrip("\r\n")
    values = {"title": title_value}
    spans = [(title.start(3), title.start(3) + len(title_value), "title")]
    values["crm_url"] = crm[1]
    spans.append((crm.start(1), crm.end(1), "crm_url"))
    header_matches = list(BULLET.finditer(header, crm.end()))
    if header[
        crm.end() : header_matches[0].start() if header_matches else len(header)
    ].strip():
        raise ValueError("Не распознана строка после CRM-ссылки")
    seen = Counter()
    for index, match in enumerate(header_matches):
        end = (
            header_matches[index + 1].start()
            if index + 1 < len(header_matches)
            else len(header)
        )
        value = header[match.end() : end].rstrip("\r\n")
        label = match[1]
        seen[label] += 1
        column = HEADER_COLUMNS.get(label, ("header", label, seen[label]))
        if column in values:
            raise ValueError("Повторяется основное поле карточки")
        values[column] = value
        spans.append((match.end(), match.end() + len(value), column))
    events_text = text[timeline.end() : fields_start]
    starts = list(EVENT_START.finditer(events_text))
    prefix_end = starts[0].start() if starts else len(events_text)
    if events_text[:prefix_end].strip():
        raise ValueError("Не распознано начало ленты касаний")
    events = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(events_text)
        events.append(parse_event(events_text[match.start() : end]))
    spans.append((timeline.end() + prefix_end, fields_start, None))
    if fields:
        section_start = timeline.end() + fields.end()
        section = text[section_start:]
        matches = list(BULLET.finditer(section))
        if section[: matches[0].start() if matches else len(section)].strip():
            raise ValueError("Не распознано начало раздела полей")
        counts = Counter()
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start() if index + 1 < len(matches) else len(section)
            )
            value = section[match.end() : end].rstrip("\r\n")
            counts[match[1]] += 1
            column = ("field", match[1], counts[match[1]])
            values[column] = value
            spans.append(
                (
                    section_start + match.end(),
                    section_start + match.end() + len(value),
                    column,
                )
            )
    return {"values": values, "layout": layout(text, spans), "events": events}
