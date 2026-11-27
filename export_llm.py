#!/usr/bin/env python3
"""
Экспорт для ИИ.

Два уровня:
1) export/touchpoints.jsonl — одна строка = одно касание (машинный формат,
   удобно грузить в БД / DataFrame / RAG-индекс);
2) export/snapshot/<Сделки|Лиды>/<группа>/LEXS_*_<id>_SNAP_..._.md —
   хронологический протокол сущности:
   звонки (с транскрипциями), чаты и смены стадий в одной ленте,
   каждая реплика подписана тем, кто её реально сказал/написал.
"""

import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import config
from logging_setup import log
from users import user_name


def _tz():
    try:
        return ZoneInfo(config.SNAPSHOT_TZ)
    except Exception:  # noqa: BLE001 — сбой системной TZ не должен прерывать выгрузку
        return datetime.now().astimezone().tzinfo


# ================================================================== контекст снимка
_RU2LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def pipeline_slug(name):
    """'РБ OFF' -> 'RB-OFF', 'Кухни (офлайн)' -> 'KUHNI-OFLAYN'."""
    if not name:
        return "NOPPE"
    lat = "".join(_RU2LAT.get(ch, ch) for ch in name.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", lat).strip("-").upper()
    return slug or "NOPPE"


# ------------------------------------------------------------- служебная страна
def entity_country(record: dict, entity: str = "deal") -> str:
    """Страна выгрузки: воронка сделки или источник лида; поля CRM не меняются."""
    if entity == "deal":
        try:
            category_id = int(record.get("CATEGORY_ID"))
        except (TypeError, ValueError):
            category_id = None
        code = config.DEAL_COUNTRY_BY_CATEGORY.get(category_id)
        if code:
            return code
    elif entity == "lead":
        source = str(record.get("_source") or "").strip()
        match = re.match(r"^(РБ|Россия|РФ)\b", source, re.IGNORECASE)
        if match:
            return "RB" if match[1].lower() == "рб" else "RF"
    else:
        raise ValueError(f"Неизвестный тип сущности: {entity}")
    log.warning(
        "{} {}: страна не определена, использую UNKNOWN", entity, record.get("ID")
    )
    return "UNKNOWN"


def update_card_country(md_text: str, country: str) -> str:
    """Обновить только страну в шапке сохранённой карточки, сохранив её формат."""
    header, separator, body = md_text.partition("\n## ")
    pattern = r"(?m)^- Страна: [^\r\n]*"
    if re.search(pattern, header):
        header = re.sub(pattern, lambda _: f"- Страна: {country}", header, count=1)
    else:
        # Позиция соответствует deal_markdown: после источника или стадии/статуса.
        anchor = r"(?m)^- (?:Источник|Текущая стадия|Текущий статус): [^\r\n]*"
        matches = list(re.finditer(anchor, header))
        if not matches:
            raise ValueError("В карточке не найдена шапка для обновления страны")
        pos = matches[-1].end()
        newline = "\r\n" if "\r\n" in header else "\n"
        header = header[:pos] + newline + f"- Страна: {country}" + header[pos:]
    return header + separator + body


class ExportContext:
    """Параметры текущего снимка выгрузки.

    Единый момент времени в config.SNAPSHOT_TZ: из него формируются
    snapshot_at в manifest, SNAP_ в именах карточек и дата в имени ZIP —
    они не могут разойтись.
    """

    def __init__(self, schema_version=None):
        self.schema_version = int(schema_version or config.SCHEMA_VERSION)
        self.started = datetime.now(_tz())

    @property
    def snap(self):
        """20260803T083000+0300"""
        return self.started.strftime("%Y%m%dT%H%M%S%z")

    @property
    def zip_stamp(self):
        """2026-08-03 08-30 — дата-время для имени архива (та же TZ)."""
        return self.started.strftime("%Y-%m-%d %H-%M")

    @property
    def database_stamp(self):
        """Единая для всех устройств дата базы в TZ снимка: 2026_11_26 15-00."""
        return self.started.strftime("%Y_%m_%d %H-%M")

    @property
    def export_id(self):
        return f"EXP_{self.snap}"


CHANNEL_ICON = {
    "call": "📞",
    "viber": "💬 Viber",
    "telegram": "💬 Telegram",
    "whatsapp": "💬 WhatsApp",
    "vk": "💬 VK",
    "site_chat": "💬 чат сайта",
    "instagram": "💬 Instagram",
    "avito": "💬 Avito",
    "email": "✉️",
    "comment": "📝",
    "meeting": "🤝",
    "crm": "📌",
    "task": "📋",
}


def _fmt_dur(sec):
    if not sec:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def write_touchpoints_jsonl(all_touchpoints, path=None):
    path = path or os.path.join(config.EXPORT_DIR, "touchpoints.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(
            json.dumps(tp, ensure_ascii=False) + "\n" for tp in all_touchpoints
        )
    return path


def write_deals_index(deals_meta, path=None):
    path = path or os.path.join(config.EXPORT_DIR, "deals_index.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(d, ensure_ascii=False) + "\n" for d in deals_meta)
    return path


# ---------------------------------------------------------------- структура снимка
# Папки внутри архива/стейджинга — человекочитаемые, русские (ТЗ заказчика);
# имена карточек остаются машинными (LEXS_DEAL_... / LEXS_LEAD_...).
ENTITY_DIR = {"deal": "Сделки", "lead": "Лиды"}
GROUP_DIR = {
    "active": "Активные",
    "completed": "Завершенные",
    "technical": "Технические",
}
# поля, уже показанные в шапке карточки (не дублируем в «## Поля»)
_SHOWN_FIELDS = {
    "deal": {
        "ID",
        "TITLE",
        "ASSIGNED_BY_ID",
        "CONTACT_ID",
        "STAGE_ID",
        "OPPORTUNITY",
        "CURRENCY_ID",
        "DATE_CREATE",
        "SOURCE_ID",
    },
    "lead": {
        "ID",
        "TITLE",
        "NAME",
        "LAST_NAME",
        "SECOND_NAME",
        "ASSIGNED_BY_ID",
        "STATUS_ID",
        "OPPORTUNITY",
        "CURRENCY_ID",
        "DATE_CREATE",
        "SOURCE_ID",
        "COMPANY_TITLE",
    },
}


def snapshot_root():
    """Стейджинг текущего снимка: export/snapshot/ (пакуется в ZIP 1:1)."""
    return os.path.join(config.EXPORT_DIR, "snapshot")


def ensure_snapshot_dirs(root=None):
    """Все папки структуры снимка — всегда, даже пустые (контракт ТЗ)."""
    root = root or snapshot_root()
    for ed in ENTITY_DIR.values():
        for gd in GROUP_DIR.values():
            os.makedirs(os.path.join(root, ed, gd), exist_ok=True)
    return root


def deal_markdown(deal, touchpoints, users_map, stages_map, entity="deal"):
    """Готовый контекст по сделке/лиду для LLM."""
    did = int(deal["ID"])
    is_lead = entity == "lead"
    resp = user_name(users_map, deal.get("ASSIGNED_BY_ID")) or "—"
    client_name = deal.get("_contact_name") or "—"
    stage_id = deal.get("STATUS_ID") if is_lead else deal.get("STAGE_ID")
    stage_now = stages_map.get(stage_id, stage_id or "—")

    lines = [
        f"# {'Лид' if is_lead else 'Сделка'} #{did}: {deal.get('TITLE', '')}",
        "",
        # ссылка на сущность в CRM (базовый URL портала — config.PORTAL_URL)
        f"**CRM:** {config.PORTAL_URL}/crm/{'lead' if is_lead else 'deal'}/details/{did}/",
        "",
        f"- Ответственный: {resp}",
        f"- Клиент: {client_name}",
        f"- {'Текущий статус' if is_lead else 'Текущая стадия'}: {stage_now}",
    ]
    if deal.get("_source"):
        lines.append(f"- Источник: {deal['_source']}")
    if deal.get("_country"):
        lines.append(f"- Страна: {deal['_country']}")
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

    # все поля сделки/лида (кроме уже показанных в шапке)
    fields = deal.get("_fields") or {}
    if fields:
        shown = _SHOWN_FIELDS.get(entity, _SHOWN_FIELDS["deal"])
        lines += ["", f"## Поля {'лида' if is_lead else 'сделки'}", ""]
        for code, f in fields.items():
            if code in shown:
                continue
            lines.append(f"- {f['title']}: {f['value']}")
        lines.append("")
    return "\n".join(lines)


def card_filename(deal, ctx, pipeline_map=None, entity="deal"):
    """
    Имя карточки:
      сделка: LEXS_DEAL_<CTRY>_<PIPELINE>_<ID>_SNAP_<ts>_schema-vN.md
      лид:    LEXS_LEAD_<CTRY>_<ID>_SNAP_<ts>_schema-vN.md  (воронки нет)
    """
    country = deal.get("_country") or entity_country(deal, entity)
    kind = "LEAD" if entity == "lead" else "DEAL"
    parts = [config.FILE_BRAND, kind, country]
    if entity != "lead":
        cat = str(deal.get("CATEGORY_ID") or "0")
        parts.append(pipeline_slug((pipeline_map or {}).get(cat, cat)))
    parts += [str(int(deal["ID"])), "SNAP", ctx.snap, f"schema-v{ctx.schema_version}"]
    return "_".join(parts) + ".md"


def write_deal_markdown(
    deal,
    touchpoints,
    users_map,
    stages_map,
    group=None,
    ctx=None,
    pipeline_map=None,
    entity="deal",
):
    out_dir = os.path.join(
        snapshot_root(), ENTITY_DIR[entity], GROUP_DIR.get(group, group or "")
    )
    os.makedirs(out_dir, exist_ok=True)
    fname = (
        card_filename(deal, ctx, pipeline_map, entity)
        if ctx is not None
        else f"{entity}_{int(deal['ID'])}.md"
    )
    path = os.path.join(out_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(deal_markdown(deal, touchpoints, users_map, stages_map, entity=entity))
    return path


def write_deal_markdown_copy(md_text, deal, group, ctx, pipeline_map, entity="deal"):
    """
    Перенос готового протокола из store в текущий снимок:
    Страна шапки пересчитана, имя файла — с датой ЭТОГО снимка.
    Для неизменных закрытых/технических сделок/лидов (без переобработки).
    """
    out_dir = os.path.join(
        snapshot_root(), ENTITY_DIR[entity], GROUP_DIR.get(group, group)
    )
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, card_filename(deal, ctx, pipeline_map, entity))
    md_text = update_card_country(
        md_text, deal.get("_country") or entity_country(deal, entity)
    )
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(md_text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


# ================================================================== manifest + zip
ALL_GROUPS = ("active", "completed", "technical")
ENTITIES = ("deals", "leads")  # блоки manifest (сделки / лиды)


def _entity_block(groups_index, errors):
    """Блок manifest по одной сущности (сделки или лиды)."""
    entity = (
        "leads"
        if any("lead_id" in d for g in groups_index.values() for d in g)
        else "deals"
    )
    id_key = "lead_ids" if entity == "leads" else "deal_ids"

    def _id(d):
        return d.get("lead_id", d.get("deal_id"))

    ids = sorted(_id(d) for g in groups_index.values() for d in g)
    return {
        "total": len(ids),
        "active_count": len(groups_index.get("active") or []),
        "completed_count": len(groups_index.get("completed") or []),
        "technical_count": len(groups_index.get("technical") or []),
        "files_count": len(ids),
        id_key: ids,
        # все три группы описываются всегда, даже пустые;
        # reused — сколько перенесено из store без переобработки
        "groups": {
            g: {
                "count": len(groups_index.get(g) or []),
                "reused": sum(
                    1 for d in (groups_index.get(g) or []) if d.get("reused")
                ),
                "ids": sorted(_id(d) for d in (groups_index.get(g) or [])),
            }
            for g in ALL_GROUPS
        },
        "errors_count": len(errors or []),
        "errors": errors or [],
    }


def write_manifest(
    ctx, pipelines, entities, errors, skipped, filter_desc, incremental=None
):
    """
    manifest.json — в корень снимка (export/snapshot/).

    Контракт: КАЖДЫЙ архив — полный актуальный снимок CRM
    (export_type = FULL_SNAPSHOT). update_mode лишь указывает СПОСОБ
    получения данных из Bitrix (FULL / INCREMENTAL), архив дельтой
    не является никогда.

    is_complete: снимок признаётся полностью успешным ТОЛЬКО если
    выгружены все требуемые объекты без ошибок (errors пуст по обеим
    сущностям). Если часть объектов не выгружена из-за ошибок —
    is_complete = false и status = "partial".
    """
    incremental = incremental or {}
    entities = entities or {}
    blocks = {}
    all_stats = []
    for ent in ENTITIES:
        data = entities.get(ent) or {}
        blocks[ent] = _entity_block(data.get("groups") or {}, data.get("errors") or [])
        all_stats += [d for g in (data.get("groups") or {}).values() for d in g]
    all_errors = list(errors or [])
    is_complete = not all_errors and all(
        b["errors_count"] == 0 for b in blocks.values()
    )
    creates = [d.get("date_create") for d in all_stats if d.get("date_create")]
    countries = sorted({d.get("country") for d in all_stats if d.get("country")})
    manifest = {
        "export_id": ctx.export_id,
        "export_type": "FULL_SNAPSHOT",
        # полнота выгрузки: false, если были ошибки выгрузки объектов
        "is_complete": is_complete,
        "status": "complete" if is_complete else "partial",
        "schema_version": ctx.schema_version,
        "snapshot_at": self_iso(ctx.started),
        "snapshot_timezone": config.SNAPSHOT_TZ,
        # Служебные страны: воронки сделок и источники лидов.
        "countries": countries,
        # --- техническая информация о способе сборки (не про состав архива)
        "update_mode": incremental.get("update_mode", "FULL"),
        "previous_export_id": incremental.get("previous_export_id"),
        "previous_snapshot_at": incremental.get("previous_snapshot_at"),
        "changed_since": incremental.get("changed_since"),
        "overlap_from": incremental.get("overlap_from"),
        # ---
        "pipelines": pipelines,
        # фактический охват снимка: от самого старого объекта до даты снимка
        "coverage": {
            "from": min(creates) if creates else None,
            "to": self_iso(ctx.started),
        },
        "filter": filter_desc,
        # логически отдельные блоки по сущностям (количества, ID, ошибки)
        "deals": blocks["deals"],
        "leads": blocks["leads"],
        # совокупный список ошибок (сделки + лиды) и пропуски
        "errors": all_errors,
        "errors_count": len(all_errors),
        "skipped": skipped,
    }
    root = snapshot_root()
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return path


def self_iso(dt):
    return dt.isoformat(timespec="seconds")


def make_snapshot_zip(ctx):
    """
    Запаковывает export/snapshot/ в
    «LEXS выгрузка из Битрикс24 на дату ГГГГ-ММ-ДД ЧЧ-ММ.zip»
    (шаблон — config.ZIP_NAME_TEMPLATE, дата — из snapshot_at,
    тот же момент и TZ, что у manifest и имён карточек).

    Структура внутри — 1:1 как в ТЗ: manifest.json в корне,
    Сделки/{Активные,Завершенные,Технические}, Лиды/{...}.
    Пустые папки включаются всегда. Вторичные файлы (touchpoints_*,
    deals_index_*) в архив НЕ попадают — они лежат в export/ рядом.
    Служебные файлы macOS (.DS_Store, ._*) исключаются.
    """
    name = config.ZIP_NAME_TEMPLATE.format(stamp=ctx.zip_stamp)
    archive = os.path.join(config.EXPORT_DIR, name + ".zip")
    root = snapshot_root()
    with TemporaryDirectory(prefix=".snapshot-", dir=config.EXPORT_DIR) as directory:
        temporary_zip = os.path.join(directory, "snapshot.zip")
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _dirs, files in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root)
                if rel_dir != "." and not files:
                    # пустые папки — тоже часть контракта структуры
                    z.writestr(rel_dir.replace(os.sep, "/") + "/", "")
                for fn in files:
                    if fn == ".DS_Store" or fn.startswith("._"):
                        continue
                    full = os.path.join(dirpath, fn)
                    z.write(full, os.path.relpath(full, root))
        os.replace(temporary_zip, archive)
    for previous in Path(config.EXPORT_DIR).glob("*.zip"):
        if previous != Path(archive) and previous.is_file():
            previous.unlink()
    log.info("ZIP карточек сохранён; предыдущие архивы удалены")
    return archive
