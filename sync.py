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
import sys
import time

import config
import fetch_deals
import fetch_activities
import fetch_openlines
import normalize
import export_llm
import transcribe
from api_client import B24Client, BitrixError
from users import load_users, load_stages, load_sources, source_name, ContactBook

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
def process_deal(client, deal, users_map, stages_map, contacts, args, group=None):
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
    export_llm.write_deal_markdown(deal, touchpoints, users_map, stages_map, group=group)

    stats = {
        "deal_id": did,
        "title": deal.get("TITLE"),
        "responsible": deal.get("ASSIGNED_BY_ID"),
        "contact": deal.get("_contact_name"),
        "source": deal.get("_source"),
        "stage": stages_map.get(deal.get("STAGE_ID"), deal.get("STAGE_ID")),
        "closed": fetch_deals.is_closed(deal),
        "touchpoints": len(touchpoints),
        "calls": len(calls),
        "ol_chats": len(chats),
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
        log.info("Метаданные полей сделки: %d", len(fields_meta))
    except Exception as e:
        log.warning("Метаданные полей не получены: %s", e)
        fields_meta = {}

    deals = fetch_deals.get_deals(client)
    log.info("Сделок к обработке: %d", len(deals))

    groups = {"active": {"tps": [], "index": []},
              "completed": {"tps": [], "index": []}}
    skipped = 0
    t0 = time.time()
    for i, deal in enumerate(deals, 1):
        did = int(deal["ID"])
        closed = fetch_deals.is_closed(deal)
        group = "completed" if closed else "active"
        if (group == "active" and not config.PROCESS_ACTIVE) or \
           (group == "completed" and not config.PROCESS_COMPLETED):
            skipped += 1
            continue
        try:
            deal["_source"] = source_name(source_map, deal.get("SOURCE_ID"))
            deal["_fields"] = fetch_deals.resolve_deal_fields(deal, fields_meta)
            tps, stats = process_deal(client, deal, users_map, stages_map,
                                      contacts, args, group=group)
            groups[group]["tps"].extend(tps)
            groups[group]["index"].append(stats)
            log.info("[%d/%d] %s | Сделка %d «%s»: касаний %d (звонков %d, чатов %d)",
                     i, len(deals), group, did, (deal.get("TITLE") or "")[:40],
                     stats["touchpoints"], stats["calls"], stats["ol_chats"])
        except BitrixError as e:
            log.error("[%d/%d] Сделка %d: ошибка API: %s", i, len(deals), did, e)
        except Exception as e:
            log.exception("[%d/%d] Сделка %d: %s", i, len(deals), did, e)

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

    with open(os.path.join(config.DATA_DIR, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"last_run": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "deals": total_deals, "touchpoints": total_tps},
                  f, ensure_ascii=False, indent=1)

    log.info("Готово за %.1f мин: %d сделок, %d касаний (пропущено фильтром: %d)",
             (time.time() - t0) / 60, total_deals, total_tps, skipped)
    log.info("Markdown-протоколы: %s (active|completed)",
             os.path.join(config.EXPORT_DIR, "deals"))


if __name__ == "__main__":
    sys.exit(main())