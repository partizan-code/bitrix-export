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
import re
import zipfile
from datetime import datetime, timedelta

import config
from users import user_name  # noqa: F401  (re-export для sync)


# ================================================================== контекст снимка
_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def pipeline_slug(name):
    """'РБ OFF' -> 'RB-OFF', 'Кухни (офлайн)' -> 'KUHNI-OFLAYN'."""
    if not name:
        return "NOPPE"
    lat = "".join(_RU2LAT.get(ch, ch) for ch in name.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", lat).strip("-").upper()
    return slug or "NOPPE"


class ExportContext:
    """Параметры текущего снимка выгрузки."""

    def __init__(self, schema_version=None):
        self.schema_version = int(schema_version or config.SCHEMA_VERSION)
        self.started = datetime.now().astimezone()

    @property
    def snap(self):
        """20260803T083000+0300"""
        return self.started.strftime("%Y%m%dT%H%M%S%z")

    @property
    def export_id(self):
        return f"EXP_{self.snap}"

    @property
    def period(self):
        """(from, to) — строки YYYY-MM-DD. Дефолт: прошлая календарная неделя."""
        if config.EXPORT_PERIOD_FROM and config.EXPORT_PERIOD_TO:
            return config.EXPORT_PERIOD_FROM, config.EXPORT_PERIOD_TO
        today = self.started.date()
        monday = today - timedelta(days=today.weekday())
        return (monday - timedelta(days=7)).isoformat(), \
               (monday - timedelta(days=1)).isoformat()

CHANNEL_ICON = {
    "call": "📞", "viber": "💬 Viber", "telegram": "💬 Telegram",
    "whatsapp": "💬 WhatsApp", "vk": "💬 VK", "site_chat": "💬 чат сайта",
    "instagram": "💬 Instagram", "avito": "💬 Avito",
    "email": "✉️", "comment": "📝", "meeting": "🤝", "crm": "📌", "task": "📋",
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

        if tp["event_type"] == "task":
            c = tp["content"]
            head = f"[{ts}] {icon} Задача — {who}{stage_tag}: {c.get('text', '')}"
            extra = []
            if c.get("status"):
                extra.append(c["status"])
            if c.get("deadline"):
                extra.append(f"дедлайн {c['deadline'][:16].replace('T', ' ')}")
            if c.get("closed_date"):
                extra.append(f"закрыта {c['closed_date'][:16].replace('T', ' ')}")
            if c.get("created_by"):
                extra.append(f"постановщик: {c['created_by']}")
            if extra:
                head += "  (" + ", ".join(extra) + ")"
            lines.append(head)
            if c.get("description"):
                for dl in c["description"].splitlines():
                    lines.append(f"  {dl}")
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


def write_deal_markdown(deal, touchpoints, users_map, stages_map, group=None,
                        ctx=None, pipeline_map=None):
    out_dir = os.path.join(config.EXPORT_DIR, "deals", group) if group \
        else os.path.join(config.EXPORT_DIR, "deals")
    os.makedirs(out_dir, exist_ok=True)
    if ctx is not None:
        cat = str(deal.get("CATEGORY_ID") or "0")
        slug = pipeline_slug((pipeline_map or {}).get(cat, cat))
        fname = (f"{config.FILE_BRAND}_DEAL_{config.FILE_COUNTRY}_{slug}"
                 f"_{int(deal['ID'])}_SNAP_{ctx.snap}"
                 f"_schema-v{ctx.schema_version}.md")
    else:
        fname = f"deal_{int(deal['ID'])}.md"
    path = os.path.join(out_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(deal_markdown(deal, touchpoints, users_map, stages_map))
    return path


# ================================================================== manifest + zip
ALL_GROUPS = ("active", "completed", "technical")


def write_manifest(ctx, pipelines, groups_index, errors, skipped, filter_desc,
                   incremental=None):
    """
    manifest.json в export/deals:
    ID выгрузки, тип выгрузки, версия схемы, дата снимка, страна, воронки,
    период, фильтр, связь с предыдущим снимком, количество файлов,
    ID всех сделок (по группам — всегда все три), ошибки/пропуски.
    """
    deal_ids = sorted(d["deal_id"] for g in groups_index.values() for d in g)
    files_count = len(deal_ids)
    period_from, period_to = ctx.period
    incremental = incremental or {}
    manifest = {
        "export_id": ctx.export_id,
        "export_type": incremental.get("export_type", "full"),
        "schema_version": ctx.schema_version,
        "snapshot_at": self_iso(ctx.started),
        "country": config.FILE_COUNTRY,
        "pipelines": pipelines,
        "period": {"from": period_from, "to": period_to},
        "previous_export_id": incremental.get("previous_export_id"),
        "previous_snapshot_at": incremental.get("previous_snapshot_at"),
        "changed_since": incremental.get("changed_since"),
        "overlap_from": incremental.get("overlap_from"),
        "filter": filter_desc,
        "files_count": files_count,
        "deal_ids": deal_ids,
        # все три группы описываются всегда, даже пустые
        "groups": {
            g: {"count": len(groups_index.get(g) or []),
                "deal_ids": sorted(d["deal_id"] for d in (groups_index.get(g) or []))}
            for g in ALL_GROUPS
        },
        "errors": errors,
        "skipped": skipped,
    }
    deals_dir = os.path.join(config.EXPORT_DIR, "deals")
    os.makedirs(deals_dir, exist_ok=True)
    path = os.path.join(deals_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return path


def self_iso(dt):
    return dt.isoformat(timespec="seconds")


def make_deals_zip(ctx, pipelines):
    """
    Запаковывает export/deals в
    LEXS_SALES_RB_<PIPELINE|MULTI>_WEEK_<from>_<to>_SNAP_<snap>_schema-v<N>.zip
    Служебные файлы macOS (.DS_Store, ._*) в архив не попадают.
    """
    slugs = sorted({p["code"] for p in pipelines})
    pipe_part = slugs[0] if len(slugs) == 1 else ("MULTI" if slugs else "ALL")
    period_from, period_to = ctx.period
    frm = period_from.replace("-", "")
    to = period_to.replace("-", "")
    name = (f"{config.FILE_BRAND}_SALES_{config.FILE_COUNTRY}_{pipe_part}"
            f"_WEEK_{frm}_{to}_SNAP_{ctx.snap}_schema-v{ctx.schema_version}")
    archive = os.path.join(config.EXPORT_DIR, name + ".zip")
    root = os.path.join(config.EXPORT_DIR, "deals")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn == ".DS_Store" or fn.startswith("._"):
                    continue
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, root))
    return archive