# -*- coding: utf-8 -*-
"""
Оркестратор выгрузки.

Запуск:
    python sync.py                    # полный проход по config.py
    python sync.py --limit 3          # тест на 3 сделках
    python sync.py --deals 1234 5678  # конкретные сделки
    python sync.py --no-transcribe    # без транскрибации (только структура)
    python sync.py --skip-whisper     # штатные расшифровки есть, Whisper не запускать

Что делает за проход:
    сделки -> история стадий -> звонки (статистика телефонии + записи)
    -> чаты открытых линий -> комментарии/письма -> нормализация
    -> export/touchpoints.jsonl + export/deals/deal_<id>.md

Сырой JSON каждой сделки сохраняется в raw/ — можно пересобрать
нормализацию без повторных запросов к API.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

import config
import fetch_deals
import fetch_activities
import fetch_openlines
import fetch_tasks
import normalize
import export_llm
import transcribe
from api_client import B24Client, BitrixError
from users import (load_users, load_stages, load_sources, source_name,
                   load_status_book, load_categories, ContactBook)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("b24")


# ---------------------------------------------------------------- транскрипция одного звонка
def resolve_transcript(client, deal_id, call, args):
    """Возвращает {'text': str|None, 'status': ...} для звонка."""
    has_recording = bool(call.get("recording_url") or call.get("file_ids"))
    if args.no_transcribe:
        return {"text": None,
                "status": "pending" if has_recording else "no_recording"}

    # 1) штатная расшифровка Bitrix (CoPilot)
    if config.PREFER_BUILTIN_TRANSCRIPT:
        text = fetch_activities.get_builtin_transcript(client, call.get("activity_id"))
        if text:
            return {"text": text, "status": "builtin"}

    if not has_recording:
        return {"text": None, "status": "no_recording"}

    # 2) внешняя транскрибация (AssemblyAI / Whisper — по config.TRANSCRIPTION_PROVIDER)
    if config.TRANSCRIBE_ENABLED and not args.skip_whisper:
        txt_path = transcribe.transcribe_call(client, deal_id, call)
        if txt_path:
            with open(txt_path, "r", encoding="utf-8") as f:
                return {"text": f.read(), "status": config.TRANSCRIPTION_PROVIDER}
        return {"text": None, "status": "no_transcript"}

    return {"text": None, "status": "pending"}


# ---------------------------------------------------------------- одна сделка
def process_deal(client, deal, users_map, stages_map, contacts, args,
                 group=None, ctx=None, pipeline_map=None):
    did = int(deal["ID"])
    raw = {"deal": deal}
    touchpoints = []

    deal["_contact_name"] = contacts.name(deal.get("CONTACT_ID"))

    # --- история стадий
    history = fetch_deals.get_stage_history(client, did)
    raw["stage_history"] = history
    touchpoints += normalize.tp_stage_events(deal, history, stages_map)

    # --- звонки
    calls_raw = fetch_activities.get_deal_calls(client, did, date_from=config.DATE_FROM)
    calls = [fetch_activities.normalize_call(r) for r in calls_raw]
    raw["calls"] = calls
    for call in calls:
        # скачиваем запись состоявшегося разговора (> MIN_RECORDING_DURATION_SEC)
        if config.DOWNLOAD_RECORDINGS and call.get("file_ids"):
            call["local_audio"] = transcribe.download_call_recording(client, did, call)
        tr = resolve_transcript(client, did, call, args)
        touchpoints.append(normalize.tp_from_call(
            deal, call, users_map, history, stages_map, tr))

    # --- чаты открытых линий
    chats = fetch_openlines.get_deal_chats(client, did)
    raw["ol_chats"] = []
    for chat in chats:
        messages, users_info = fetch_openlines.get_chat_history(client, chat["chat_id"])
        raw["ol_chats"].append({"chat": chat, "messages": messages})
        for msg in messages:
            if not msg.get("text") or fetch_openlines.is_system_message(msg):
                continue
            sender = fetch_openlines.classify_sender(
                msg["sender_id"], users_map, users_info, deal["_contact_name"])
            touchpoints.append(normalize.tp_from_ol_message(
                deal, chat, msg, sender, users_map, history, stages_map))

    # --- комментарии таймлайна
    if config.INCLUDE_COMMENTS:
        comments = fetch_activities.get_timeline_comments(client, did)
        raw["comments"] = comments
        for row in comments:
            touchpoints.append(normalize.tp_from_comment(
                deal, row, users_map, history, stages_map))

    # --- задачи по сделке
    tasks = []
    if config.INCLUDE_TASKS:
        tasks = fetch_tasks.get_deal_tasks(client, did)
        raw["tasks"] = tasks
        for t in tasks:
            touchpoints.append(normalize.tp_from_task(
                deal, t, users_map, history, stages_map, fetch_tasks.task_field))

    # --- письма / встречи
    type_ids = []
    if config.INCLUDE_EMAILS:
        type_ids.append(3)
    if config.INCLUDE_MEETINGS:
        type_ids.append(1)
    if type_ids:
        acts = fetch_activities.get_activities(client, did, type_ids)
        raw["activities"] = acts
        for act in acts:
            touchpoints.append(normalize.tp_from_activity(
                deal, act, users_map, history, stages_map))

    # --- сырой слепок
    raw_path = os.path.join(config.RAW_DIR, f"deal_{did}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1, default=str)

    # --- markdown-протокол
    export_llm.write_deal_markdown(deal, touchpoints, users_map, stages_map,
                                   group=group, ctx=ctx, pipeline_map=pipeline_map)

    stats = {
        "deal_id": did,
        "title": deal.get("TITLE"),
        "responsible": deal.get("ASSIGNED_BY_ID"),
        "contact": deal.get("_contact_name"),
        "source": deal.get("_source"),
        "stage": stages_map.get(deal.get("STAGE_ID"), deal.get("STAGE_ID")),
        "closed": fetch_deals.is_closed(deal),
        "group": group,
        "touchpoints": len(touchpoints),
        "calls": len(calls),
        "ol_chats": len(chats),
        "tasks": len(tasks),
        "fields": deal.get("_fields") or {},
    }
    return touchpoints, stats


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Выгрузка касаний Bitrix24 для анализа ИИ")
    ap.add_argument("--limit", type=int, default=0, help="не больше N сделок")
    ap.add_argument("--deals", type=int, nargs="*", default=None, help="ID сделок")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="не брать транскрипции вообще (быстрая проверка структуры)")
    ap.add_argument("--skip-whisper", action="store_true",
                    help="не запускать внешнюю транскрибацию (AssemblyAI/Whisper)")
    args = ap.parse_args()

    if args.deals:
        config.DEAL_IDS = args.deals
    if args.limit:
        config.DEALS_LIMIT = args.limit

    client = B24Client()
    me = client.ping()
    log.info("Подключено: %s (%s)", f"{me.get('NAME','')} {me.get('LAST_NAME','')}".strip(),
             me.get("ID"))

    log.info("Загружаю справочники (пользователи, стадии)...")
    users_map = load_users(client)
    stages_map = load_stages(client)
    contacts = ContactBook(client)
    log.info("Пользователей: %d, стадий: %d", len(users_map), len(stages_map))

    # справочник источников CRM (SOURCE_ID -> название)
    try:
        source_map = load_sources(client)
        log.info("Источников в справочнике: %d", len(source_map))
    except Exception as e:
        log.warning("Справочник источников не получен: %s", e)
        source_map = {}

    # метаданные всех полей сделки (названия, enum-варианты)
    try:
        fields_meta = fetch_deals.load_deal_fields_meta(client)
        log.info("Метаданные полей сделки: %d (UF: %d)", len(fields_meta),
                 sum(1 for k in fields_meta if k.startswith("UF_")))
    except Exception as e:
        log.warning("Метаданные полей не получены: %s", e)
        fields_meta = {}

    # справочники для crm_status-полей (Тип, Стадия, Источник) и воронок
    status_maps = {"DEAL_STAGE": stages_map, "SOURCE": source_map}
    category_map = {}
    try:
        status_maps["DEAL_TYPE"] = load_status_book(client, "DEAL_TYPE")
        category_map = load_categories(client)
    except Exception as e:
        log.warning("Справочники типов/воронок не получены: %s", e)

    # --- предыдущий снимок -> режим выгрузки (full / incremental)
    prev_state = {}
    state_path = os.path.join(config.DATA_DIR, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                prev_state = json.load(f)
        except Exception as e:
            log.warning("state.json не прочитан: %s", e)

    prev_snap = None
    if prev_state.get("last_run"):
        try:
            prev_snap = datetime.strptime(prev_state["last_run"],
                                          "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            log.warning("last_run в state.json не распознан: %r", prev_state["last_run"])

    # тестовый запуск (конкретные ID или лимит) — всегда полный режим
    # и не является якорем инкремента (state.json не обновляется)
    is_test_run = bool(config.DEAL_IDS or config.DEALS_LIMIT)

    export_type = config.EXPORT_TYPE
    if export_type == "auto":
        export_type = "incremental" if (prev_snap and not is_test_run) else "full"
    if export_type == "incremental" and (not prev_snap or is_test_run):
        if is_test_run:
            log.info("Тестовый запуск (DEAL_IDS/DEALS_LIMIT) -> полный режим, "
                     "фильтр DATE_FROM/период применяется")
        else:
            log.warning("Инкрементальный режим невозможен (нет прошлого снимка) "
                        "-> полный проход")
        export_type = "full"

    changed_since = overlap_from = None
    if export_type == "incremental":
        changed_since = prev_snap
        overlap_from = prev_snap - timedelta(hours=config.INCREMENTAL_OVERLAP_HOURS)
        log.info("Инкрементальный режим: ALL ACTIVE + изменения с %s "
                 "(перекрытие от %s)",
                 changed_since.isoformat(timespec="seconds"),
                 overlap_from.isoformat(timespec="seconds"))
        deals = fetch_deals.get_deals_incremental(
            client, overlap_from.isoformat(timespec="seconds"))
    else:
        deals = fetch_deals.get_deals(client)
    log.info("Режим: %s. Сделок к обработке: %d", export_type, len(deals))

    # контекст снимка выгрузки (ID, дата, версия схемы)
    ctx = export_llm.ExportContext()
    log.info("Снимок: %s (schema-v%d, период %s)",
             ctx.export_id, ctx.schema_version, "—".join(ctx.period))

    # папка deals — стейджинг ТЕКУЩЕГО снимка: чистим, чтобы файлы прошлых
    # запусков не попали в новый архив (история хранится в zip-архивах export/)
    deals_dir = os.path.join(config.EXPORT_DIR, "deals")
    if os.path.isdir(deals_dir):
        shutil.rmtree(deals_dir)
    os.makedirs(deals_dir, exist_ok=True)

    groups = {g: {"tps": [], "index": []} for g in export_llm.ALL_GROUPS}
    group_enabled = {"active": config.PROCESS_ACTIVE,
                     "completed": config.PROCESS_COMPLETED,
                     "technical": config.PROCESS_TECHNICAL}
    errors, skipped = [], []
    t0 = time.time()
    for i, deal in enumerate(deals, 1):
        did = int(deal["ID"])
        group = fetch_deals.deal_group(deal, stages_map)
        if not group_enabled[group]:
            skipped.append({"deal_id": did, "reason": f"группа '{group}' отключена"})
            continue
        try:
            deal["_source"] = source_name(source_map, deal.get("SOURCE_ID"))
            deal["_fields"] = fetch_deals.resolve_deal_fields(
                deal, fields_meta, users_map, status_maps, category_map)
            tps, stats = process_deal(client, deal, users_map, stages_map,
                                      contacts, args, group=group,
                                      ctx=ctx, pipeline_map=category_map)
            groups[group]["tps"].extend(tps)
            groups[group]["index"].append(stats)
            log.info("[%d/%d] %s | Сделка %d «%s»: касаний %d (звонков %d, чатов %d, задач %d)",
                     i, len(deals), group, did, (deal.get("TITLE") or "")[:40],
                     stats["touchpoints"], stats["calls"], stats["ol_chats"], stats["tasks"])
        except BitrixError as e:
            log.error("[%d/%d] Сделка %d: ошибка API: %s", i, len(deals), did, e)
            errors.append({"deal_id": did, "error": f"API: {e}"})
        except Exception as e:
            log.exception("[%d/%d] Сделка %d: %s", i, len(deals), did, e)
            errors.append({"deal_id": did, "error": str(e)})

    total_deals = total_tps = 0
    for group, data in groups.items():
        if not data["index"]:
            continue
        data["tps"].sort(key=lambda t: (t["deal_id"], t["timestamp"] or ""))
        p1 = export_llm.write_touchpoints_jsonl(
            data["tps"], os.path.join(config.EXPORT_DIR, f"touchpoints_{group}.jsonl"))
        p2 = export_llm.write_deals_index(
            data["index"], users_map,
            os.path.join(config.EXPORT_DIR, f"deals_index_{group}.jsonl"))
        total_deals += len(data["index"])
        total_tps += len(data["tps"])
        log.info("[%s] %d сделок, %d касаний -> %s", group,
                 len(data["index"]), len(data["tps"]), p1)

    # --- воронки, участвующие в выгрузке
    present_cats = sorted({
        str(d.get("CATEGORY_ID") or "0")
        for g in groups.values() for d in g["index"]
    } | {str(d.get("CATEGORY_ID") or "0") for d in deals})
    pipelines = [
        {"id": cid, "name": category_map.get(cid, cid),
         "code": export_llm.pipeline_slug(category_map.get(cid, cid))}
        for cid in present_cats
    ]

    filter_desc = {
        "deal_ids": config.DEAL_IDS or None,
        "date_from": config.DATE_FROM,
        "period_filter": ({">=DATE_CREATE": config.EXPORT_PERIOD_FROM,
                           "<=DATE_CREATE": config.EXPORT_PERIOD_TO}
                          if fetch_deals.period_filter_active() else None),
        "deals_limit": config.DEALS_LIMIT or None,
        "limit_reached": bool(config.DEALS_LIMIT and len(deals) >= config.DEALS_LIMIT),
        # все три группы описываются всегда (true/false = включена ли)
        "groups": {"active": config.PROCESS_ACTIVE,
                   "completed": config.PROCESS_COMPLETED,
                   "technical": config.PROCESS_TECHNICAL},
        "technical_stage_ids": config.TECHNICAL_STAGE_IDS or None,
        "technical_stage_keywords": config.TECHNICAL_STAGE_KEYWORDS,
        "include": {"comments": config.INCLUDE_COMMENTS,
                    "tasks": config.INCLUDE_TASKS,
                    "emails": config.INCLUDE_EMAILS,
                    "meetings": config.INCLUDE_MEETINGS},
    }

    incremental = {
        "export_type": export_type,
        "previous_export_id": prev_state.get("export_id"),
        "previous_snapshot_at": prev_state.get("last_run"),
        "changed_since": changed_since.isoformat(timespec="seconds")
                         if changed_since else None,
        "overlap_from": overlap_from.isoformat(timespec="seconds")
                        if overlap_from else None,
    }

    mpath = export_llm.write_manifest(
        ctx, pipelines,
        {g: data["index"] for g, data in groups.items()},
        errors, skipped, filter_desc, incremental)
    log.info("Manifest: %s", mpath)

    if config.MAKE_SNAPSHOT_ZIP and total_deals:
        zpath = export_llm.make_deals_zip(ctx, pipelines)
        log.info("Архив снимка: %s", zpath)

    # state.json — якорь следующего инкрементального снимка.
    # Тестовые запуски (--deals / --limit / DEALS_LIMIT) якорь НЕ сдвигают,
    # иначе изменения между полным и тестовым запуском потеряются.
    if not is_test_run:
        with open(os.path.join(config.DATA_DIR, "state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"last_run": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "export_id": ctx.export_id,
                       "export_type": export_type,
                       "deals": total_deals, "touchpoints": total_tps},
                      f, ensure_ascii=False, indent=1)
    else:
        log.info("Тестовый запуск: state.json (якорь инкремента) не обновляю")

    log.info("Готово за %.1f мин: %d сделок, %d касаний (пропущено: %d, ошибок: %d)",
             (time.time() - t0) / 60, total_deals, total_tps, len(skipped), len(errors))
    log.info("Markdown-протоколы: %s (active|completed|technical)",
             os.path.join(config.EXPORT_DIR, "deals"))


if __name__ == "__main__":
    sys.exit(main())