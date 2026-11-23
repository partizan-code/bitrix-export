"""
refresh_names.py — обновление имён в уже сохранённых протоколах.

Зачем: если сотрудник уволен, в старых md ответственный/менеджер
остаётся «user_<id>». Этот скрипт перерендеривает markdown из
артефактов store (data/store/**/deal/<id>.json + tps/<id>.jsonl) с
АКТУАЛЬНОЙ картой пользователей и перепаковывает новый архив
FULL_SNAPSHOT — без переобработки сделок и лидов: ни активностей,
ни аудио, ни транскрибаций. С портала берутся только справочники.

Что обновляется:
  - «Ответственный» в шапке (ASSIGNED_BY_ID -> ФИО);
  - поля-сотрудники в «## Поля ...» (employee/user -> ФИО);
  - имена менеджеров в ленте касаний (actor.user_id -> ФИО);
  - «Текущая стадия/статус», если стадии/статусы переименовали.

Использование:
  python refresh_names.py            # справочники — свежие с портала
  python refresh_names.py --offline  # из кэшей data/*.json (без API)

Перед запуском убедитесь, что users.load_users возвращает ВСЕХ
пользователей, включая уволенных (без filter ACTIVE=true), — иначе
имена уволенных взять неоткуда.
"""

import argparse
import json
import logging
import os
import re
import shutil

import config
import export_llm
import fetch_deals
import fetch_leads
import store
from users import (load_users, load_stages, load_lead_statuses, load_sources,
                   load_status_book, load_categories, user_name, LazyUserMap)

log = logging.getLogger("b24")

_USER_PLACEHOLDER = re.compile(r"\buser_\d+\b")


# ------------------------------------------------------------- справочники
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _int_keys(d):
    out = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def load_books(offline):
    """users_map, stages по сущностям, fields_meta, status_maps, category_map."""
    if offline:
        users_map = _int_keys(_load_json(
            os.path.join(config.DATA_DIR, "users_cache.json"), {}))
        if not users_map:
            raise SystemExit("data/users_cache.json пуст — в offline-режиме "
                             "имена взять неоткуда. Запустите без --offline.")
        stages = {
            "deal": _load_json(os.path.join(config.DATA_DIR, "stages.json"), {}),
            "lead": _load_json(os.path.join(config.DATA_DIR, "lead_statuses.json"), {}),
        }
        fields_meta = {
            "deal": _load_json(os.path.join(config.DATA_DIR, "deal_fields_meta.json"), {}),
            "lead": _load_json(os.path.join(config.DATA_DIR, "lead_fields_meta.json"), {}),
        }
        category_map = _load_json(
            os.path.join(config.DATA_DIR, "categories.json"),
            {"0": "Основная воронка"})
        source_map = _load_json(os.path.join(config.DATA_DIR, "sources.json"), {})
        log.info("Offline: пользователей %d, стадий сделок %d, статусов лидов %d",
                 len(users_map), len(stages["deal"]), len(stages["lead"]))
    else:
        from api_client import B24Client
        client = B24Client()
        # LazyUserMap: неизвестные ID (уволенные/боты) дозапрашиваются точечно
        users_map = LazyUserMap(client, load_users(client))
        stages = {"deal": load_stages(client), "lead": {}}
        try:
            stages["lead"] = load_lead_statuses(client)
        except Exception as e:
            log.warning("Статусы лидов не получены: %s", e)
        try:
            source_map = load_sources(client)
        except Exception as e:
            log.warning("Источники не получены: %s", e)
            source_map = {}
        fields_meta = {"deal": {}, "lead": {}}
        try:
            fields_meta["deal"] = fetch_deals.load_deal_fields_meta(client)
        except Exception as e:
            log.warning("Метаданные полей сделки не получены: %s", e)
        try:
            fields_meta["lead"] = fetch_leads.load_lead_fields_meta(client)
        except Exception as e:
            log.warning("Метаданные полей лида не получены: %s", e)
        try:
            category_map = load_categories(client)
        except Exception as e:
            log.warning("Воронки не получены: %s", e)
            category_map = _load_json(
                os.path.join(config.DATA_DIR, "categories.json"),
                {"0": "Основная воронка"})
        if category_map:
            path = os.path.join(config.DATA_DIR, "categories.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(category_map, f, ensure_ascii=False, indent=1)
        log.info("Пользователей: %d, стадий сделок: %d, статусов лидов: %d",
                 len(users_map), len(stages["deal"]), len(stages["lead"]))

    status_maps = {
        "deal": {"DEAL_STAGE": stages["deal"], "SOURCE": source_map},
        "lead": {"STATUS": stages["lead"], "SOURCE": source_map},
    }
    return users_map, stages, fields_meta, status_maps, category_map


# ------------------------------------------------------------- перерезолв
def patch_fields_offline(deal, users_map):
    """
    Замена без метаданных: в уже готовых _fields ищет значения user_<id>
    и подставляет ФИО из карты. Тип поля не проверяем (нет метаданных),
    но плейсхолдер user_<id> однозначен.
    """
    fields = deal.get("_fields") or {}
    patched = 0
    for code, f in fields.items():
        value = str(f.get("value") or "")
        if not _USER_PLACEHOLDER.search(value):
            continue
        raw = deal.get(code)
        ids = raw if isinstance(raw, list) else [raw]
        names = []
        for v in ids:
            if str(v).lstrip("-").isdigit():
                n = user_name(users_map, int(v))
                if n:
                    names.append(n)
        if names:
            f["value"] = ", ".join(names)
            patched += 1
    return patched


def refresh_touchpoint_names(tps, users_map):
    """Имена менеджеров в ленте касаний — по actor.user_id."""
    n = 0
    for tp in tps:
        actor = tp.get("actor") or {}
        uid = actor.get("user_id")
        if uid and actor.get("type") in ("manager", "bot"):
            new = user_name(users_map, uid)
            if new and new != actor.get("name"):
                actor["name"] = new
                n += 1
        c = tp.get("content") or {}
        v = c.get("created_by")
        if v and _USER_PLACEHOLDER.fullmatch(str(v)):
            new = user_name(users_map, str(v)[5:])
            if new:
                c["created_by"] = new
                n += 1
    return n


# ------------------------------------------------------------- main
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Обновить имена в сохранённых протоколах и перепаковать")
    ap.add_argument("--offline", action="store_true",
                    help="не ходить на портал: справочники из data/*.json")
    args = ap.parse_args()

    (users_map, stages, fields_meta,
     status_maps, category_map) = load_books(args.offline)

    ctx = export_llm.ExportContext()
    entities = {ent: {"groups": {g: [] for g in export_llm.ALL_GROUPS},
                      "errors": []}
                for ent in export_llm.ENTITIES}
    all_tps = {ent: {g: [] for g in export_llm.ALL_GROUPS}
               for ent in export_llm.ENTITIES}
    changed = 0

    # стейджинг нового снимка — с нуля, все папки создаются всегда
    snap_root = export_llm.snapshot_root()
    shutil.rmtree(snap_root, ignore_errors=True)
    export_llm.ensure_snapshot_dirs(snap_root)

    for entity, ent_block in (("deal", "deals"), ("lead", "leads")):
        ids = store.stored_ids(entity=entity)
        if not ids:
            continue
        log.info("%s: сущностей в store: %d", entity, len(ids))
        stages_map = stages[entity]
        fmeta = fields_meta[entity]
        smap = status_maps[entity]
        cats = category_map if entity == "deal" else {}
        id_key = "lead_id" if entity == "lead" else "deal_id"

        for i, did in enumerate(ids, 1):
            deal = store.load_deal(did, entity=entity)
            tps = store.load_tps(did, entity=entity)
            stats = store.load_stats(did, entity=entity)
            if entity == "lead" and not deal.get("_contact_name"):
                deal["_contact_name"] = fetch_leads.display_name(deal)
            group = stats.get("group")
            if group not in entities[ent_block]["groups"]:
                if entity == "lead":
                    group = fetch_leads.lead_group(deal, stages_map)
                else:
                    group = fetch_deals.deal_group(deal, stages_map)

            # 1) поля-сотрудники
            if fmeta:
                deal["_fields"] = fetch_deals.resolve_deal_fields(
                    deal, fmeta, users_map, smap, cats)
            else:
                patch_fields_offline(deal, users_map)
            stats["fields"] = deal.get("_fields") or {}

            # 2) имена в ленте касаний
            refresh_touchpoint_names(tps, users_map)

            # 3) перерендер markdown с актуальными именами
            md_text = export_llm.deal_markdown(deal, tps, users_map,
                                               stages_map, entity=entity)

            # обратно в store (теперь store всегда с актуальными именами)
            store.save_artifacts(deal, md_text, tps, stats, entity=entity)

            # в стейджинг нового снимка — с именем ЭТОГО снимка
            export_llm.write_deal_markdown_copy(md_text, deal, group, ctx,
                                                cats, entity=entity)
            stats["reused"] = True
            stats["group"] = group
            stats.setdefault(id_key, did)
            if not stats.get("country"):
                stats["country"] = deal.get("_country") or config.FILE_COUNTRY
            entities[ent_block]["groups"][group].append(stats)
            all_tps[ent_block][group].extend(tps)
            changed += 1
            if i % 500 == 0:
                log.info("%s [%d/%d] обновлено", entity, i, len(ids))

    # сопутствующие файлы выгрузки (в export/, в архив не попадают)
    for ent, ent_block in (("deal", "deals"), ("lead", "leads")):
        for group in export_llm.ALL_GROUPS:
            idx = entities[ent_block]["groups"][group]
            if not idx:
                continue
            tps = all_tps[ent_block][group]
            tps.sort(key=lambda t: (t["deal_id"], t["timestamp"] or ""))
            export_llm.write_touchpoints_jsonl(
                tps, os.path.join(config.EXPORT_DIR,
                                  f"touchpoints_{ent}_{group}.jsonl"))
            export_llm.write_deals_index(
                idx, users_map,
                os.path.join(config.EXPORT_DIR,
                             f"index_{ent}_{group}.jsonl"))

    pipelines = [{"id": cid, "name": category_map.get(cid, cid),
                  "code": export_llm.pipeline_slug(category_map.get(cid, cid))}
                 for cid in sorted(category_map.keys() or ["0"])]
    filter_desc = {"note": "refresh_names: обновлены имена "
                           "(ответственный/менеджеры/поля-сотрудники) "
                           "без переобработки сделок и лидов"}
    export_llm.write_manifest(ctx, pipelines, entities,
                              errors=[], skipped=[],
                              filter_desc=filter_desc,
                              incremental={"update_mode": "FULL"})
    archive = export_llm.make_snapshot_zip(ctx)
    log.info("Готово: обновлено %d протоколов -> %s", changed, archive)


if __name__ == "__main__":
    main()