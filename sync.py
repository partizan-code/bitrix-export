#!/usr/bin/env python3
"""
Оркестратор выгрузки (сделки + лиды).

Запускается Telegram-ботом без аргументов; все параметры — в config.py.
Ручной запуск тем же способом: python sync.py.

Что делает за проход:
    сделки и лиды -> история стадий/статусов -> звонки (записи, транскрипции)
    -> чаты открытых линий -> комментарии/письма/задачи -> нормализация
    -> export/snapshot/{Сделки,Лиды}/{Активные,Завершенные,Технические}/*.md
    -> manifest.json -> ZIP «LEXS выгрузка из Битрикс24 на дату ....zip»

Каждый ZIP — ПОЛНЫЙ актуальный снимок (FULL_SNAPSHOT), не дельта:
закрытые/технические сущности, уже выгруженные и не изменившиеся,
переносятся из store БЕЗ переобработки (ни запросов активностей,
ни аудио, ни транскрибаций).

Сырой JSON каждой сущности сохраняется в raw/ — можно пересобрать
нормализацию без повторных запросов к API.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

import config
import export_llm
import fetch_activities
import fetch_deals
import fetch_leads
import fetch_openlines
import fetch_tasks
import normalize
import registry
import snapshot_sqlite
import store
import transcribe
from api_client import B24Client, BitrixError
from logging_setup import log, run_cli
from performance import measure, profile_run, timed
from users import (
    ContactBook,
    LazyUserMap,
    load_categories,
    load_lead_statuses,
    load_sources,
    load_stages,
    load_status_book,
    load_users,
    source_name,
)

# параметры сущностей: различия сделка/лид собраны здесь
_ENTITY = {
    "deal": {
        "owner_type": 2,
        "task_prefix": "D",
        "title": "Сделка",
        "fetch": staticmethod(lambda c: fetch_deals.get_deals(c)),
        "fetch_inc": staticmethod(lambda c, s: fetch_deals.get_deals_incremental(c, s)),
        "fetch_ids": staticmethod(lambda c, ids: fetch_deals.get_deals_by_ids(c, ids)),
        "group": staticmethod(lambda d, sm: fetch_deals.deal_group(d, sm)),
        "is_closed": staticmethod(fetch_deals.is_closed),
        "stage_field": "STAGE_ID",
    },
    "lead": {
        "owner_type": 1,
        "task_prefix": "L",
        "title": "Лид",
        "fetch": staticmethod(lambda c: fetch_leads.get_leads(c)),
        "fetch_inc": staticmethod(lambda c, s: fetch_leads.get_leads_incremental(c, s)),
        "fetch_ids": staticmethod(lambda c, ids: fetch_leads.get_leads_by_ids(c, ids)),
        "group": staticmethod(lambda d, sm: fetch_leads.lead_group(d, sm)),
        "is_closed": staticmethod(fetch_leads.is_closed),
        "stage_field": "STATUS_ID",
    },
}


def _sid(stats):
    """ID сущности из stats-записи (deal_id у сделок, lead_id у лидов)."""
    return stats.get("deal_id", stats.get("lead_id"))


# ---------------------------------------------------------------- прогресс для внешних пультов
PROGRESS_PATH = os.path.join(config.DATA_DIR, "progress.json")


def _progress(**kw):
    """
    Состояние прогона в data/progress.json (атомарно).
    Читает telegram-бот (bot.py) и любые другие наблюдатели.
    """
    kw["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = PROGRESS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
        os.replace(tmp, PROGRESS_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- транскрипция одного звонка
def resolve_transcript(client, deal_id, call):
    """Получает транскрипцию звонка через AssemblyAI и возвращает текст/статус."""
    has_recording = bool(call.get("file_ids"))
    if not has_recording:
        return {"text": None, "status": "no_recording"}

    if config.TRANSCRIBE_ENABLED:
        txt_path = transcribe.transcribe_call(client, deal_id, call)
        if txt_path:
            with open(txt_path, "r", encoding="utf-8") as f:
                return {"text": f.read(), "status": "assemblyai"}
        return {"text": None, "status": "no_transcript"}

    return {"text": None, "status": "pending"}


# ---------------------------------------------------------------- одна сущность (сделка/лид)
@timed("stage.process_entity")
def process_entity(
    client,
    deal,
    users_map,
    stages_map,
    contacts,
    group=None,
    ctx=None,
    pipeline_map=None,
    entity="deal",
):
    ep = _ENTITY[entity]
    owner_type = ep["owner_type"]
    did = int(deal["ID"])
    raw = {entity: deal}
    touchpoints = []

    if entity == "lead":
        deal["_contact_name"] = fetch_leads.display_name(deal)
    else:
        deal["_contact_name"] = contacts.name(deal.get("CONTACT_ID"))

    with measure("source.stage_history"):
        # --- история стадий/статусов
        history = fetch_deals.get_stage_history(client, did, entity_type_id=owner_type)
        raw["stage_history"] = history
        touchpoints += normalize.tp_stage_events(deal, history, stages_map)

    with measure("source.calls"):
        # --- звонки
        calls_raw = fetch_activities.get_deal_calls(
            client, did, date_from=config.DATE_FROM, owner_type=owner_type
        )
        calls = [fetch_activities.normalize_call(r) for r in calls_raw]
        raw["calls"] = calls
        for call in calls:
            # Скачивание записи нужно только если: есть file_ids, транскрипции
            # ещё нет в кэше И не работает прямой режим AssemblyAI
            # (при ASSEMBLYAI_DIRECT_URL запись забирает сам AssemblyAI).
            if (
                config.DOWNLOAD_RECORDINGS
                and call.get("file_ids")
                and not transcribe.use_assemblyai_direct()
                and not transcribe.has_transcript(did, call)
            ):
                call["local_audio"] = transcribe.download_call_recording(
                    client, did, call
                )
            tr = resolve_transcript(client, did, call)
            touchpoints.append(
                normalize.tp_from_call(deal, call, users_map, history, stages_map, tr)
            )

    with measure("source.chats"):
        # --- чаты открытых линий
        chats = fetch_openlines.get_deal_chats(client, did, entity_type=entity)
        raw["ol_chats"] = []
        for chat in chats:
            messages, users_info = fetch_openlines.get_chat_history(
                client, chat["chat_id"]
            )
            raw["ol_chats"].append({"chat": chat, "messages": messages})
            for msg in messages:
                if not msg.get("text") or fetch_openlines.is_system_message(msg):
                    continue
                sender = fetch_openlines.classify_sender(
                    msg["sender_id"], users_map, users_info, deal["_contact_name"]
                )
                touchpoints.append(
                    normalize.tp_from_ol_message(
                        deal, chat, msg, sender, history, stages_map
                    )
                )

    with measure("source.comments"):
        # --- комментарии таймлайна
        if config.INCLUDE_COMMENTS:
            comments = fetch_activities.get_timeline_comments(
                client, did, entity_type=entity
            )
            raw["comments"] = comments
            for row in comments:
                touchpoints.append(
                    normalize.tp_from_comment(deal, row, users_map, history, stages_map)
                )

    with measure("source.tasks"):
        # --- задачи по сущности
        tasks = []
        if config.INCLUDE_TASKS:
            tasks = fetch_tasks.get_deal_tasks(client, did, prefix=ep["task_prefix"])
            raw["tasks"] = tasks
            for t in tasks:
                touchpoints.append(
                    normalize.tp_from_task(
                        deal, t, users_map, history, stages_map, fetch_tasks.task_field
                    )
                )

    with measure("source.activities"):
        # --- письма / встречи
        type_ids = []
        if config.INCLUDE_EMAILS:
            type_ids.append(3)
        if config.INCLUDE_MEETINGS:
            type_ids.append(1)
        if type_ids:
            acts = fetch_activities.get_activities(
                client, did, type_ids, owner_type=owner_type
            )
            raw["activities"] = acts
            for act in acts:
                touchpoints.append(
                    normalize.tp_from_activity(
                        deal, act, users_map, history, stages_map
                    )
                )

    with measure("stage.entity_artifacts"):
        # --- сырой слепок
        raw_path = os.path.join(config.RAW_DIR, f"{entity}_{did}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1, default=str)

        # --- markdown-протокол
        md_path = export_llm.write_deal_markdown(
            deal,
            touchpoints,
            users_map,
            stages_map,
            group=group,
            ctx=ctx,
            pipeline_map=pipeline_map,
            entity=entity,
        )

        id_key = "lead_id" if entity == "lead" else "deal_id"
        stats = {
            id_key: did,
            "entity": entity,
            "title": deal.get("TITLE"),
            "date_create": deal.get("DATE_CREATE"),
            "date_modify": deal.get("DATE_MODIFY"),
            "responsible": deal.get("ASSIGNED_BY_ID"),
            "contact": deal.get("_contact_name"),
            "source": deal.get("_source"),
            "country": deal.get("_country"),
            "stage": stages_map.get(
                deal.get(ep["stage_field"]), deal.get(ep["stage_field"])
            ),
            "closed": ep["is_closed"](deal),
            "group": group,
            "touchpoints": len(touchpoints),
            "calls": len(calls),
            "ol_chats": len(chats),
            "tasks": len(tasks),
            "fields": deal.get("_fields") or {},
        }
        # артефакты — в постоянное хранилище (для кумулятивных снимков)
        store.save_deal(deal, md_path, touchpoints, stats, entity=entity)
    return touchpoints, stats


# ---------------------------------------------------------------- проход по одной сущности
def run_entity(
    entity,
    client,
    ctx,
    books,
    prev_state,
    export_type,
    is_test_run,
    changed_since_iso,
):
    """
    Выборка -> обработка -> перенос из store -> индексы.
    Возвращает {"groups": {g: {"tps","index"}}, "errors", "skipped",
                "total", "total_tps", "filter_desc"}.
    """
    ep = _ENTITY[entity]
    title = ep["title"]
    id_key = "lead_id" if entity == "lead" else "deal_id"
    users_map = books["users_map"]
    stages_map = books["lead_statuses"] if entity == "lead" else books["stages_map"]
    fields_meta = (
        books["lead_fields_meta"] if entity == "lead" else books["fields_meta"]
    )
    status_maps = dict(books["status_maps"])
    if entity == "lead":
        status_maps["STATUS"] = books["lead_statuses"]
    category_map = books["category_map"] if entity == "deal" else {}
    source_map = books["source_map"]
    contacts = books["contacts"]
    reg = books["reg"]

    # --- выборка
    _progress(
        entity=entity,
        stage="выборка из Bitrix24",
        processed=0,
        total=0,
        reused=0,
        errors=0,
    )
    with measure("stage.selection"):
        if export_type == "incremental":
            deals = ep["fetch_inc"](client, changed_since_iso)
            fetched_count = len(deals)
            # повтор сущностей, упавших с ошибкой в прошлый запуск, —
            # иначе они навсегда выпали бы из кумулятивных архивов
            have = {int(d["ID"]) for d in deals}
            prev_errors = prev_state.get("errors") or {}
            if isinstance(prev_errors, list):  # старый формат — это сделки
                prev_errors = {"deals": prev_errors}
            retry = [
                int(i)
                for i in (prev_errors.get(f"{entity}s") or [])
                if int(i) not in have
            ]
            if retry:
                log.info(
                    "{}: повторяю с ошибкой прошлого запуска: {}", title, len(retry)
                )
                deals += ep["fetch_ids"](client, retry)
                deals.sort(key=lambda d: int(d["ID"]))
        else:
            deals = ep["fetch"](client)
            fetched_count = len(deals)

    # --- реестр + store: кумулятивный снимок.
    # Закрытые/технические, выгруженные ранее и не изменившиеся,
    # НЕ переобрабатываются: артефакты переносятся из store.
    reused = []  # (deal, group) — перенести из store без обработки
    deal_groups = {}
    if reg:
        kept = []
        for deal in deals:
            group = ep["group"](deal, stages_map)
            if registry.is_duplicate_closed(deal, group, reg, entity=entity):
                rec = reg[registry.key(entity, deal["ID"])]
                if registry.can_reuse(rec, config.SCHEMA_VERSION) and store.has_deal(
                    deal["ID"], entity=entity
                ):
                    reused.append((deal, group))
                    continue
                # артефактов нет или схема устарела -> обрабатываем заново
            kept.append((deal, group))
        if reused:
            log.info(
                "{}: из store без переобработки: {} закрытых/технических",
                title,
                len(reused),
            )
        deal_groups = {int(d["ID"]): g for d, g in kept + reused}
        deals = [d for d, _g in kept]
    log.info("{}: к обработке {}", title, len(deals))
    _progress(
        entity=entity,
        stage="обработка",
        processed=0,
        total=len(deals),
        reused=len(reused),
        errors=0,
    )

    groups = {g: {"tps": [], "index": []} for g in export_llm.ALL_GROUPS}
    group_enabled = {
        "active": config.PROCESS_ACTIVE,
        "completed": config.PROCESS_COMPLETED,
        "technical": config.PROCESS_TECHNICAL,
    }
    errors, skipped = [], []
    for i, deal in enumerate(deals, 1):
        did = int(deal["ID"])
        group = deal_groups.get(did) or ep["group"](deal, stages_map)
        if not group_enabled[group]:
            skipped.append(
                {id_key: did, "entity": entity, "reason": f"группа '{group}' отключена"}
            )
            continue
        try:
            deal["_source"] = source_name(source_map, deal.get("SOURCE_ID"))
            deal["_fields"] = fetch_deals.resolve_deal_fields(
                deal, fields_meta, users_map, status_maps, category_map
            )
            deal["_country"] = export_llm.entity_country(deal, entity)
            tps, stats = process_entity(
                client,
                deal,
                users_map,
                stages_map,
                contacts,
                group=group,
                ctx=ctx,
                pipeline_map=category_map,
                entity=entity,
            )
            groups[group]["tps"].extend(tps)
            groups[group]["index"].append(stats)
            registry.mark(reg, deal, group, ctx, entity=entity)
            log.debug(
                "{} [{}/{}] {} | ID {}: касаний {} (звонков {}, чатов {}, задач {})",
                title,
                i,
                len(deals),
                group,
                did,
                stats["touchpoints"],
                stats["calls"],
                stats["ol_chats"],
                stats["tasks"],
            )
        except BitrixError as e:
            log.error("{} [{}/{}] {}: ошибка API: {}", title, i, len(deals), did, e)
            errors.append({id_key: did, "entity": entity, "error": f"API: {e}"})
        except Exception as e:  # noqa: BLE001 — продолжаем выгрузку остальных сущностей
            log.opt(exception=e).error(
                "{} [{}/{}] ID {}: ошибка обработки", title, i, len(deals), did
            )
            errors.append({id_key: did, "entity": entity, "error": str(e)})

        _progress(
            entity=entity,
            stage="обработка",
            processed=i,
            total=len(deals),
            reused=len(reused),
            errors=len(errors),
        )

        # периодический чекпоинт реестра: при обрыве прогона все закрытые
        # сущности, успевшие обработаться, будут добраны из store без
        # переобработки (store пишется по каждой сущности сразу)
        if not is_test_run and i % 25 == 0:
            registry.save(reg)

    # --- перенос закрытых/технических из store
    # (никаких запросов к API: ни активностей, ни аудио, ни транскрибаций)
    def reuse_from_store(deal, group):
        did = int(deal["ID"])
        if entity == "lead":
            # При отсутствии справочника сохраняем расшифрованный источник store.
            saved = store.load_deal(did, entity=entity)
            deal["_source"] = (
                source_map.get(str(deal.get("SOURCE_ID")))
                or deal.get("_source")
                or (
                    saved.get("_source")
                    if saved.get("SOURCE_ID") == deal.get("SOURCE_ID")
                    else None
                )
            )
        deal["_country"] = export_llm.entity_country(deal, entity)
        stats = store.load_stats(did, entity=entity)
        stats["reused"] = True
        stats["group"] = group
        stats["country"] = deal["_country"]
        export_llm.write_deal_markdown_copy(
            store.load_md(did, entity=entity),
            deal,
            group,
            ctx,
            category_map,
            entity=entity,
        )
        groups[group]["tps"].extend(store.load_tps(did, entity=entity))
        groups[group]["index"].append(stats)
        registry.mark(reg, deal, group, ctx, entity=entity)

    # 1) закрытые из этой выборки (изменились до закрытия на этой неделе и т.п.)
    for deal, group in reused:
        try:
            reuse_from_store(deal, group)
        except Exception as e:  # noqa: BLE001 — ошибка одной сущности не прерывает перенос остальных
            log.error("{} {}: не удалось перенести из store: {}", title, deal["ID"], e)
            errors.append(
                {id_key: int(deal["ID"]), "entity": entity, "error": f"store: {e}"}
            )

    # 2) добор остальных закрытых из реестра: архив КУМУЛЯТИВНЫЙ —
    # сущности прошлых недель тоже должны быть в каждом новом пакете
    # (только в инкрементальном режиме; full — это ребейз по своему фильтру)
    present_ids = {_sid(d) for g in groups.values() for d in g["index"]}
    swept = 0
    for k, rec in reg.items() if export_type == "incremental" else ():
        ent_k, did = registry.parse_key(k)
        if (
            ent_k != entity
            or did in present_ids
            or rec.get("group") not in registry.CLOSED_GROUPS
            or not group_enabled.get(rec.get("group"))
            or not registry.can_reuse(rec, config.SCHEMA_VERSION)
            or not store.has_deal(did, entity=entity)
        ):
            continue
        try:
            reuse_from_store(store.load_deal(did, entity=entity), rec["group"])
            swept += 1
        except Exception as e:  # noqa: BLE001 — ошибка одной сущности не прерывает добор остальных
            log.error("{} {}: добор из store не удался: {}", title, did, e)
            errors.append({id_key: did, "entity": entity, "error": f"store: {e}"})
    total_reused = len(reused) + swept
    if total_reused:
        log.info("{}: перенесено из store без переобработки: {}", title, total_reused)
    _progress(
        entity=entity,
        stage="готово",
        processed=len(deals),
        total=len(deals),
        reused=total_reused,
        errors=len(errors),
    )

    total = total_tps = 0
    for group, data in groups.items():
        if not data["index"]:
            continue
        data["tps"].sort(key=lambda t: (t["deal_id"], t["timestamp"] or ""))
        export_llm.write_touchpoints_jsonl(
            data["tps"],
            os.path.join(config.EXPORT_DIR, f"touchpoints_{entity}_{group}.jsonl"),
        )
        export_llm.write_deals_index(
            data["index"],
            os.path.join(config.EXPORT_DIR, f"index_{entity}_{group}.jsonl"),
        )
        total += len(data["index"])
        total_tps += len(data["tps"])
        log.info(
            "{} [{}] {} объектов, {} касаний",
            title,
            group,
            len(data["index"]),
            len(data["tps"]),
        )

    filter_desc = {
        "ids": (config.LEAD_IDS if entity == "lead" else config.DEAL_IDS) or None,
        "limit": (config.LEADS_LIMIT if entity == "lead" else config.DEALS_LIMIT)
        or None,
        "limit_reached": bool(
            (config.LEADS_LIMIT if entity == "lead" else config.DEALS_LIMIT)
            and fetched_count
            >= (config.LEADS_LIMIT if entity == "lead" else config.DEALS_LIMIT)
        ),
    }
    return {
        "groups": groups,
        "errors": errors,
        "skipped": skipped,
        "total": total,
        "total_tps": total_tps,
        "filter_desc": filter_desc,
    }


# ---------------------------------------------------------------- main
@profile_run
def main():
    t0 = perf_counter()
    if len(sys.argv) > 1:
        raise SystemExit(
            "sync.py не принимает аргументы. Задайте параметры в config.py "
            "и запустите выгрузку кнопкой бота или командой python sync.py."
        )

    # тестовый запуск (конкретные ID или лимит) — всегда полный режим
    # и не является якорем инкремента (state.json/registry не обновляются)
    deal_test = bool(config.DEAL_IDS or config.DEALS_LIMIT)
    lead_test = bool(config.LEAD_IDS or config.LEADS_LIMIT)
    is_test_run = deal_test or lead_test
    # защита от сюрприза: тестовый прогон сделок не запускает ПОЛНУЮ
    # выгрузку лидов — лиды в тесте идут только при заданных LEAD_IDS/LEADS_LIMIT
    leads_enabled = config.PROCESS_LEADS and (not deal_test or lead_test)
    if config.PROCESS_LEADS and not leads_enabled:
        log.info(
            "Тестовый режим сделок: лиды пропускаю "
            "(задайте LEAD_IDS/LEADS_LIMIT в config.py для теста лидов)"
        )

    with measure("stage.reference_books"):
        client = B24Client()
        me = client.ping()
        log.info("Подключено к Bitrix24: user_id={}", me.get("ID"))

        log.info("Загружаю справочники (пользователи, стадии, статусы)...")
        # LazyUserMap: неизвестные ID (уволенные/боты) дозапрашиваются точечно
        users_map = LazyUserMap(client, load_users(client))
        stages_map = load_stages(client)
        contacts = ContactBook(client)
        lead_statuses = {}
        if leads_enabled:
            try:
                lead_statuses = load_lead_statuses(client)
            except Exception as e:  # noqa: BLE001 — необязательный справочник не останавливает выгрузку
                log.warning("Статусы лидов не получены: {}", e)
        log.info(
            "Пользователей: {}, стадий сделок: {}, статусов лидов: {}",
            len(users_map),
            len(stages_map),
            len(lead_statuses),
        )

        # справочник источников CRM (SOURCE_ID -> название)
        try:
            source_map = load_sources(client)
            log.info("Источников в справочнике: {}", len(source_map))
        except Exception as e:  # noqa: BLE001 — необязательный справочник не останавливает выгрузку
            log.warning("Справочник источников не получен: {}", e)
            source_map = {}

        # метаданные полей (названия, enum-варианты)
        try:
            fields_meta = fetch_deals.load_deal_fields_meta(client)
            log.info(
                "Метаданные полей сделки: {} (UF: {})",
                len(fields_meta),
                sum(1 for k in fields_meta if k.startswith("UF_")),
            )
        except Exception as e:  # noqa: BLE001 — необязательные метаданные не останавливают выгрузку
            log.warning("Метаданные полей сделки не получены: {}", e)
            fields_meta = {}
        lead_fields_meta = {}
        if leads_enabled:
            try:
                lead_fields_meta = fetch_leads.load_lead_fields_meta(client)
                log.info(
                    "Метаданные полей лида: {} (UF: {})",
                    len(lead_fields_meta),
                    sum(1 for k in lead_fields_meta if k.startswith("UF_")),
                )
            except Exception as e:  # noqa: BLE001 — необязательные метаданные не останавливают выгрузку
                log.warning("Метаданные полей лида не получены: {}", e)

        # справочники для crm_status-полей (Тип, Стадия, Источник) и воронок
        status_maps = {"DEAL_STAGE": stages_map, "SOURCE": source_map}
        category_map = {}
        try:
            status_maps["DEAL_TYPE"] = load_status_book(client, "DEAL_TYPE")
            category_map = load_categories(client)
        except Exception as e:  # noqa: BLE001 — необязательные справочники не останавливают выгрузку
            log.warning("Справочники типов/воронок не получены: {}", e)

    # --- предыдущий снимок -> режим выгрузки (full / incremental)
    prev_state = {}
    state_path = os.path.join(config.DATA_DIR, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                prev_state = json.load(f)
        except Exception as e:  # noqa: BLE001 — повреждённый state допускает полный запуск
            log.warning("state.json не прочитан: {}", e)

    prev_snap = None
    if prev_state.get("last_run"):
        try:
            prev_snap = datetime.strptime(prev_state["last_run"], "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            log.warning(
                "last_run в state.json не распознан: {!r}", prev_state["last_run"]
            )

    export_type = config.EXPORT_TYPE
    if export_type == "auto":
        export_type = "incremental" if (prev_snap and not is_test_run) else "full"
    if export_type == "incremental" and (not prev_snap or is_test_run):
        if is_test_run:
            log.info(
                "Тестовый запуск (ID/лимит) -> полный режим, "
                "фильтр DATE_FROM/период применяется"
            )
        else:
            log.warning(
                "Инкрементальный режим невозможен (нет прошлого снимка) "
                "-> полный проход"
            )
        export_type = "full"

    changed_since = overlap_from = None
    if export_type == "incremental":
        changed_since = prev_snap
        overlap_from = prev_snap - timedelta(hours=config.INCREMENTAL_OVERLAP_HOURS)
        log.info(
            "Инкрементальный режим: ALL ACTIVE + изменения с {} (перекрытие от {})",
            changed_since.isoformat(timespec="seconds"),
            overlap_from.isoformat(timespec="seconds"),
        )

    # контекст снимка выгрузки (ID, дата, версия схемы) — единая TZ
    ctx = export_llm.ExportContext()
    log.info(
        "Снимок: {} (schema-v{}, режим {}, DATE_CREATE >= {}, TZ {})",
        ctx.export_id,
        ctx.schema_version,
        export_type,
        config.DATE_FROM or "без ограничения",
        config.SNAPSHOT_TZ,
    )

    # стейджинг ТЕКУЩЕГО снимка: чистим, чтобы файлы прошлых запусков
    # не попали в новый архив (история хранится в zip-архивах export/)
    snap_root = export_llm.snapshot_root()
    if os.path.isdir(snap_root):
        shutil.rmtree(snap_root)
    export_llm.ensure_snapshot_dirs(snap_root)

    books = {
        "users_map": users_map,
        "stages_map": stages_map,
        "lead_statuses": lead_statuses,
        "fields_meta": fields_meta,
        "lead_fields_meta": lead_fields_meta,
        "status_maps": status_maps,
        "category_map": category_map,
        "source_map": source_map,
        "contacts": contacts,
        "reg": registry.load(),
    }
    changed_since_iso = (
        overlap_from.isoformat(timespec="seconds") if overlap_from else None
    )

    results = {}
    with measure("stage.deals"):
        results["deals"] = run_entity(
            "deal",
            client,
            ctx,
            books,
            prev_state,
            export_type,
            is_test_run,
            changed_since_iso,
        )
    if leads_enabled:
        with measure("stage.leads"):
            results["leads"] = run_entity(
                "lead",
                client,
                ctx,
                books,
                prev_state,
                export_type,
                is_test_run,
                changed_since_iso,
            )
    else:
        results["leads"] = {
            "groups": {g: {"tps": [], "index": []} for g in export_llm.ALL_GROUPS},
            "errors": [],
            "skipped": [],
            "total": 0,
            "total_tps": 0,
            "filter_desc": {"enabled": False},
        }

    # --- воронки сделок (все из справочника; у лидов воронок нет)
    pipelines = [
        {
            "id": cid,
            "name": category_map.get(cid, cid),
            "code": export_llm.pipeline_slug(category_map.get(cid, cid)),
        }
        for cid in sorted(category_map.keys() or ["0"])
    ]

    filter_desc = {
        "date_from": config.DATE_FROM,
        "period_filter": (
            {
                ">=DATE_CREATE": config.EXPORT_PERIOD_FROM,
                "<=DATE_CREATE": config.EXPORT_PERIOD_TO,
            }
            if fetch_deals.period_filter_active()
            else None
        ),
        # все три группы описываются всегда (true/false = включена ли)
        "groups": {
            "active": config.PROCESS_ACTIVE,
            "completed": config.PROCESS_COMPLETED,
            "technical": config.PROCESS_TECHNICAL,
        },
        "technical_stage_ids": config.TECHNICAL_STAGE_IDS or None,
        "technical_stage_keywords": config.TECHNICAL_STAGE_KEYWORDS,
        "leads": {
            "enabled": leads_enabled,
            "technical_status_ids": config.LEAD_TECHNICAL_STATUS_IDS or None,
            "technical_status_keywords": config.LEAD_TECHNICAL_STATUS_KEYWORDS,
        },
        "per_entity": {
            "deals": results["deals"]["filter_desc"],
            "leads": results["leads"]["filter_desc"],
        },
        "include": {
            "comments": config.INCLUDE_COMMENTS,
            "tasks": config.INCLUDE_TASKS,
            "emails": config.INCLUDE_EMAILS,
            "meetings": config.INCLUDE_MEETINGS,
        },
    }

    incremental = {
        # update_mode — СПОСОБ получения данных из Bitrix;
        # сам архив всегда FULL_SNAPSHOT (см. write_manifest)
        "update_mode": "INCREMENTAL" if export_type == "incremental" else "FULL",
        "previous_export_id": prev_state.get("export_id"),
        "previous_snapshot_at": prev_state.get("last_run"),
        "changed_since": changed_since.isoformat(timespec="seconds")
        if changed_since
        else None,
        "overlap_from": (
            overlap_from.isoformat(timespec="seconds") if overlap_from else None
        ),
    }

    entities = {
        ent: {
            "groups": {g: data["groups"][g]["index"] for g in export_llm.ALL_GROUPS},
            "errors": data["errors"],
        }
        for ent, data in results.items()
    }
    errors = results["deals"]["errors"] + results["leads"]["errors"]
    skipped = results["deals"]["skipped"] + results["leads"]["skipped"]

    mpath = export_llm.write_manifest(
        ctx, pipelines, entities, errors, skipped, filter_desc, incremental
    )
    log.info("Manifest: {}", mpath)

    total_deals = results["deals"]["total"]
    total_leads = results["leads"]["total"]
    total_tps = results["deals"]["total_tps"] + results["leads"]["total_tps"]

    zpath = None
    if config.MAKE_SNAPSHOT_ZIP and (total_deals or total_leads):
        with measure("stage.zip"):
            zpath = export_llm.make_snapshot_zip(ctx)
        log.info("Архив снимка: {}", zpath)

    sqlite_result = {}
    if zpath and getattr(config, "SNAPSHOT_SQLITE_ENABLED", False):
        _progress(entity=None, stage="sqlite", deals=total_deals, leads=total_leads)
        try:
            with measure("stage.sqlite"):
                imported = snapshot_sqlite.import_snapshot(
                    Path(zpath), Path(config.SNAPSHOT_SQLITE_PATH)
                )
        except Exception as error:  # noqa: BLE001 — ZIP сохранён, состояние выгрузки нужно завершить
            sqlite_result = {
                "sqlite_status": "error",
                "sqlite_error": type(error).__name__,
            }
            log.error("Не удалось сохранить снимок в SQLite: {}", error)
        else:
            sqlite_result = {
                "sqlite_status": "added" if imported["added"] else "existing",
                "sqlite": os.path.basename(config.SNAPSHOT_SQLITE_PATH),
            }
            log.info(
                "SQLite: {} карточек; {}",
                imported["cards"],
                config.SNAPSHOT_SQLITE_PATH,
            )

    _progress(
        entity=None,
        stage="done",
        deals=total_deals,
        leads=total_leads,
        errors=len(errors),
        skipped=len(skipped),
        zip=os.path.basename(zpath) if zpath else None,
        **sqlite_result,
    )

    # state.json — якорь следующего инкрементального снимка,
    # registry.json — реестр выгруженных сущностей (защита от дублей закрытых).
    # Тестовые запуски (ID/лимиты в config.py) ни то ни другое
    # НЕ обновляют, иначе тесты сдвинули бы якорь и «засорили» реестр.
    if not is_test_run:
        with open(
            os.path.join(config.DATA_DIR, "state.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(
                {
                    "last_run": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "export_id": ctx.export_id,
                    "export_type": export_type,
                    "errors": {
                        "deals": [e.get("deal_id") for e in results["deals"]["errors"]],
                        "leads": [e.get("lead_id") for e in results["leads"]["errors"]],
                    },
                    "deals": total_deals,
                    "leads": total_leads,
                    "touchpoints": total_tps,
                },
                f,
                ensure_ascii=False,
                indent=1,
            )
        registry.save(books["reg"])
        log.info("Реестр: {} записей (registry.json)", len(books["reg"]))
    else:
        log.info("Тестовый запуск: state.json и registry.json не обновляю")

    log.info(
        "Готово за {:.1f} мин: сделок {}, лидов {}, касаний {} "
        "(пропущено: {}, ошибок: {})",
        (perf_counter() - t0) / 60,
        total_deals,
        total_leads,
        total_tps,
        len(skipped),
        len(errors),
    )
    log.info("Снимок: {}", snap_root)


if __name__ == "__main__":
    run_cli(main)
