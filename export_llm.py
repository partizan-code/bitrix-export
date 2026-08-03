# -*- coding: utf-8 -*-
"""
Экспорт для ИИ.

Два уровня:
1) export/touchpoints.jsonl — одна строка = одно касание (машинный формат,
   удобно грузить в БД / DataFrame / RAG-индекс);
2) export/deals/deal_<id>.md — хронологический протокол сделки:
   звонки (с транскрипциями), чаты и смены стадий в одной ленте,
   каждая реплика подписана тем, кто её реально сказал/написал.
"""

import json
import os

import config
from users import user_name  # noqa: F401  (re-export для sync)

CHANNEL_ICON = {
    "call": "📞", "viber": "💬 Viber", "telegram": "💬 Telegram",
    "whatsapp": "💬 WhatsApp", "vk": "💬 VK", "site_chat": "💬 чат сайта",
    "instagram": "💬 Instagram", "avito": "💬 Avito",
    "email": "✉️", "comment": "📝", "meeting": "🤝", "crm": "📌",
}


def _fmt_dur(sec):
    if not sec:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def write_touchpoints_jsonl(all_touchpoints, path=None):
    path = path or os.path.join(config.EXPORT_DIR, "touchpoints.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for tp in all_touchpoints:
            f.write(json.dumps(tp, ensure_ascii=False) + "\n")
    return path


def write_deals_index(deals_meta, users_map, path=None):
    path = path or os.path.join(config.EXPORT_DIR, "deals_index.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for d in deals_meta:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return path


def deal_markdown(deal, touchpoints, users_map, stages_map):
    """Готовый контекст по сделке для LLM."""
    did = int(deal["ID"])
    resp = user_name(users_map, deal.get("ASSIGNED_BY_ID")) or "—"
    client_name = deal.get("_contact_name") or "—"
    stage_now = stages_map.get(deal.get("STAGE_ID"), deal.get("STAGE_ID") or "—")

    lines = [
        f"# Сделка #{did}: {deal.get('TITLE', '')}",
        "",
        f"- Ответственный: {resp}",
        f"- Клиент: {client_name}",
        f"- Текущая стадия: {stage_now}",
    ]
    if deal.get("_source"):
        lines.append(f"- Источник: {deal['_source']}")
    lines += [
        f"- Создана: {deal.get('DATE_CREATE', '—')}",
    ]
    if deal.get("OPPORTUNITY"):
        lines.append(f"- Сумма: {deal['OPPORTUNITY']} {deal.get('CURRENCY_ID', '')}")
    lines += ["", "## Лента касаний", ""]

    tps = sorted(touchpoints, key=lambda t: (t["timestamp"] or "", t["touchpoint_id"]))
    for tp in tps:
        ts = (tp["timestamp"] or "")[:16].replace("T", " ")
        actor = tp["actor"]
        who = actor.get("name") or actor.get("type") or "?"
        icon = CHANNEL_ICON.get(tp["channel"], tp["channel"])
        stage = tp.get("deal_stage_at_time")
        stage_tag = f" [стадия: {stage['name']}]" if stage else ""

        if tp["event_type"] == "stage_change":
            c = tp["content"]
            frm = (c.get("stage_from") or {}).get("name") or "—"
            to = (c.get("stage_to") or {}).get("name") or "?"
            lines.append(f"[{ts}] 📌 Стадия: {frm} → {to}")
            continue

        if tp["event_type"] == "call":
            c = tp["content"]
            direction = {"in": "входящий", "out": "исходящий"}.get(tp["direction"], "")
            dur = _fmt_dur(c.get("duration_sec"))
            head = f"[{ts}] {icon} Звонок ({direction}{', ' + dur if dur else ''}) — {who}{stage_tag}"
            file_ids = (tp.get("source") or {}).get("file_ids") or []
            if file_ids:
                head += f" | запись: file_id {', '.join(str(i) for i in file_ids)}"
            if c.get("audio_path"):
                head += f" ({os.path.basename(c['audio_path'])})"
            lines.append(head)
            if c.get("failed_code") and c["failed_code"] not in ("200", ""):
                lines.append(f"  (звонок не состоялся: {c['failed_code']})")
            if c.get("text"):
                lines.append("Транскрипция:")
                for tl in c["text"].splitlines():
                    lines.append(f"  {tl}")
            elif c.get("transcript_status") == "no_transcript":
                lines.append("  (запись есть, транскрибация не выполнена)")
            lines.append("")
            continue

        # сообщения чата / комментарии / письма
        who_tag = who
        if actor.get("type") == "client":
            who_tag = f"Клиент ({client_name})"
        text = (tp["content"].get("text") or "").strip()
        if text:
            lines.append(f"[{ts}] {icon} {who_tag}{stage_tag}:")
            for tl in text.splitlines():
                lines.append(f"  {tl}")
        else:
            lines.append(f"[{ts}] {icon} {who_tag}{stage_tag}: (без текста)")
    lines.append("")

    # все поля сделки (кроме уже показанных в шапке)
    fields = deal.get("_fields") or {}
    if fields:
        shown = {"ID", "TITLE", "ASSIGNED_BY_ID", "CONTACT_ID", "STAGE_ID",
                 "OPPORTUNITY", "CURRENCY_ID", "DATE_CREATE", "SOURCE_ID"}
        lines += ["", "## Поля сделки", ""]
        for code, f in fields.items():
            if code in shown:
                continue
            lines.append(f"- {f['title']}: {f['value']}")
        lines.append("")
    return "\n".join(lines)


def write_deal_markdown(deal, touchpoints, users_map, stages_map, group=None):
    out_dir = os.path.join(config.EXPORT_DIR, "deals", group) if group \
        else os.path.join(config.EXPORT_DIR, "deals")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"deal_{int(deal['ID'])}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(deal_markdown(deal, touchpoints, users_map, stages_map))
    return path