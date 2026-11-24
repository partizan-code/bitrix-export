"""Подготовка, применение и откат обновления переписок существующего снимка."""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from workspace_lock import exclusive_workspace


class RefreshError(ValueError):
    """Остановка операции без вывода переписок или секретов в диагностике."""


class CallBudget:
    """Ограничить логические вызовы поверх тайм-аутов и HTTP-повторов B24Client."""

    def __init__(self, client, limit: int):
        if type(limit) is not int or limit < 1:
            raise RefreshError("INVALID_CALL_LIMIT")
        self.client, self.limit, self.calls = client, limit, 0

    def result(self, method, params=None):
        if self.calls >= self.limit:
            raise RefreshError("CALL_BUDGET_EXCEEDED")
        self.calls += 1
        return self.client.result(method, params)


def read_json(path: Path):
    """Прочитать UTF-8 JSON без загрузки настроек приложения."""
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    """Прочитать касания и индекс сохранённого снимка."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def atomic_bytes(path: Path, value: bytes) -> None:
    """Заменить один файл только после завершения записи."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path: Path, value) -> None:
    """Атомарно сохранить JSON с русским текстом."""
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


def save_jsonl(path: Path, values) -> None:
    """Атомарно сохранить ленту либо индекс."""
    atomic_bytes(
        path,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in values).encode(
            "utf-8"
        ),
    )


def stamp(value: str) -> datetime:
    """Разобрать дату с обязательным часовым поясом."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise RefreshError("DATE_WITHOUT_TIMEZONE")
    return result.astimezone(UTC)


def message_key(tp: dict) -> tuple[int, int] | None:
    """Выделить сообщения ОЛ, не затрагивая другие типы касаний."""
    source = tp.get("source") or {}
    if (
        tp.get("event_type") == "message"
        and source.get("chat_id")
        and source.get("message_id")
    ):
        return int(source["chat_id"]), int(source["message_id"])
    return None


def error_code(error: Exception) -> str:
    """Выводить только код, без API-описаний, ссылок и содержимого JSON."""
    code = (
        str(error) if isinstance(error, RefreshError) else getattr(error, "error", None)
    )
    return (
        code
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z_0-9]+", code)
        else type(error).__name__
    )


def safe_path(root: Path, relative: str) -> Path:
    """Пути операции ограничены рабочими данными текущего проекта, без симлинков."""
    value = Path(relative)
    if (
        value.is_absolute()
        or "\\" in relative
        or ".." in value.parts
        or not value.parts
        or value.parts[0] not in {"data", "raw", "export"}
    ):
        raise RefreshError("UNSAFE_PATH")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in (path, *path.parents) if p != root.parent
    ):
        raise RefreshError("UNSAFE_PATH")
    return path


def sha(path: Path) -> str | None:
    """Отпечаток файла либо явное отсутствие; каталоги не принимаются."""
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_copy(source: Path, target: Path) -> None:
    """Копировать большие БД/архивы без загрузки всего файла в память."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_baseline(root: Path) -> dict:
    """Зафиксировать существующий состав manifest и соответствующие данные store."""
    manifest = read_json(root / "export/snapshot/manifest.json")
    baseline = {"manifest": manifest, "entities": {}}
    stamp(manifest["snapshot_at"])
    for entity in ("deal", "lead"):
        block = manifest[f"{entity}s"]
        base = root / "data/store" / ("lead" if entity == "lead" else "")
        for group, info in block["groups"].items():
            if group not in {"active", "completed", "technical"}:
                raise RefreshError("INVALID_BASELINE_GROUP")
            ids = {int(i) for i in info["ids"]}
            if not ids:
                continue
            index = read_jsonl(root / "export" / f"index_{entity}_{group}.jsonl")
            grouped = defaultdict(list)
            for tp in read_jsonl(
                root / "export" / f"touchpoints_{entity}_{group}.jsonl"
            ):
                grouped[int(tp["deal_id"])].append(tp)
            for did in sorted(ids):
                key = (entity, did)
                if key in baseline["entities"]:
                    raise RefreshError("DUPLICATE_ENTITY")
                row = next(
                    r for r in index if int(r.get("lead_id", r.get("deal_id"))) == did
                )
                tps = read_jsonl(base / "tps" / f"{did}.jsonl")
                encode = lambda t: json.dumps(t, ensure_ascii=False, sort_keys=True)
                if (
                    Counter(map(encode, tps)) != Counter(map(encode, grouped[did]))
                    or len(tps) != row["touchpoints"]
                ):
                    raise RefreshError("BASELINE_STORE_MISMATCH")
                deal = read_json(base / "deal" / f"{did}.json")
                raw = read_json(root / "raw" / f"{entity}_{did}.json")
                if int(deal["ID"]) != did or int(raw[entity]["ID"]) != did:
                    raise RefreshError("BASELINE_ID_MISMATCH")
                chats = {
                    int(c["chat"]["chat_id"]): c["chat"]
                    for c in raw.get("ol_chats", [])
                }
                for tp in tps:
                    mk = message_key(tp)
                    if mk:
                        chats.setdefault(
                            mk[0], {"chat_id": mk[0], "channel": tp["channel"]}
                        )
                baseline["entities"][key] = {
                    "deal": deal,
                    "tps": tps,
                    "chats": chats,
                    "group": group,
                    "history": raw.get("stage_history", []),
                }
        if sum(k[0] == entity for k in baseline["entities"]) != block["total"]:
            raise RefreshError("BASELINE_COUNT_MISMATCH")
    if not baseline["entities"]:
        raise RefreshError("EMPTY_BASELINE")
    baseline["users"] = {
        int(k): v for k, v in read_json(root / "data/users_cache.json").items()
    }
    extra_path = root / "data/users_extra.json"
    if extra_path.is_file():
        baseline["users"].update(
            {int(k): v for k, v in read_json(extra_path).get("resolved", {}).items()}
        )
    baseline["stage_books"] = {
        entity: read_json(root / "data" / file_name)
        for entity, file_name in (
            ("deal", "stages.json"),
            ("lead", "lead_statuses.json"),
        )
        if any(key[0] == entity for key in baseline["entities"])
    }
    return baseline


def snapshot_files(root: Path) -> list[str]:
    """Зафиксировать полный состав текущего snapshot, включая прежние имена карточек."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in (root / "export/snapshot").rglob("*")
        if p.is_file()
    )


def source_guard(root: Path, baseline: dict, databases: list[str]) -> dict:
    """Не применять подготовку к уже изменившимся источникам."""
    names = set(snapshot_files(root))
    for entity, did in baseline["entities"]:
        names.add(f"raw/{entity}_{did}.json")
        base = "data/store/" + ("lead/" if entity == "lead" else "")
        names.update(
            f"{base}{kind}/{did}{suffix}"
            for kind, suffix in (
                ("deal", ".json"),
                ("index", ".json"),
                ("tps", ".jsonl"),
                ("deals", ".md"),
            )
        )
    names.update(
        f"export/{prefix}_{entity}_{group}.jsonl"
        for entity in ("deal", "lead")
        for group in ("active", "completed", "technical")
        for prefix in ("index", "touchpoints")
    )
    names.update(
        "data/" + name
        for name in (
            "registry.json",
            "state.json",
            "users_cache.json",
            "users_extra.json",
            "stages.json",
            "lead_statuses.json",
            "deal_fields_meta.json",
            "lead_fields_meta.json",
        )
    )
    names.update(databases)
    return {name: sha(safe_path(root, name)) for name in sorted(names)}


def check_sources(root: Path, report: dict) -> None:
    """Проверить не только файлы, но и добавление новых карточек в snapshot."""
    if snapshot_files(root) != report["snapshot_files"] or any(
        sha(safe_path(root, name)) != digest for name, digest in report["guard"].items()
    ):
        raise RefreshError("SOURCES_CHANGED")


def check_databases(root: Path, databases: list[str]) -> None:
    """Не заменять SQLite при открытом журнале/WAL другой программы."""
    for name in databases:
        path = safe_path(root, name)
        if any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        ):
            raise RefreshError("SQLITE_BUSY")


def timeline_only(original: str, rendered: str, entity: str) -> str:
    """Сохранить шапку и раздел полей исходной карточки буквально."""
    newline = "\r\n" if "\r\n" in original else "\n"
    rendered = rendered.replace("\r\n", "\n").replace("\n", newline)
    marker = f"{newline}## Лента касаний{newline}"
    field = f"{newline}## Поля {'лида' if entity == 'lead' else 'сделки'}{newline}"
    if marker not in original or marker not in rendered:
        raise RefreshError("CARD_LAYOUT_MISMATCH")
    header, old_body = original.split(marker, 1)
    body = rendered.split(marker, 1)[1].partition(field)[0]
    _, separator, tail = old_body.partition(field)
    return header + marker + body + separator + tail


def prepare_entity(
    history, root: Path, baseline: dict, key: tuple
) -> tuple[dict, dict, list, str]:
    """Заменить только слой сообщений; необъяснённое исчезновение запрещает применение."""
    import export_llm
    import fetch_openlines
    import normalize

    entity, did = key
    data = baseline["entities"][key]
    raw = read_json(root / f"raw/{entity}_{did}.json")
    old_raw = {
        (int(c["chat"]["chat_id"]), int(m["id"])): m
        for c in raw.get("ol_chats", [])
        for m in c.get("messages", [])
    }
    chats = dict(data["chats"])
    for chat in history.get_entity_chats(did, entity):
        chats[chat["chat_id"]] = chat
    fresh, filtered, new_chats = {}, set(), []
    for cid, chat in sorted(chats.items()):
        messages, users, sids = history.get_chat_history(cid)
        new_chats.append({"chat": chat, "session_ids": sids, "messages": messages})
        for msg in messages:
            mk = cid, int(msg["id"])
            if fetch_openlines.is_system_message(msg):
                filtered.add(mk)
                continue
            if not msg.get("text"):
                continue
            sender = fetch_openlines.classify_sender(
                msg["sender_id"],
                baseline["users"],
                users,
                data["deal"].get("_contact_name"),
            )
            fresh[mk] = normalize.tp_from_ol_message(
                data["deal"],
                chat,
                msg,
                sender,
                data["history"],
                baseline["stage_books"][entity],
            )
    old = {message_key(tp): tp for tp in data["tps"] if message_key(tp)}
    # Служебный текст подтверждается сырым сообщением, а не actor.type=client.
    allowed_removed = {
        mk
        for mk in old
        if mk in filtered
        or (mk in old_raw and fetch_openlines.is_system_message(old_raw[mk]))
    }
    unexplained = sorted(old.keys() - fresh.keys() - allowed_removed)
    info = {
        "entity": entity,
        "id": did,
        "status": "blocked" if unexplained else "complete",
        "before": len(old),
        "after": len(fresh),
        "added": sorted(fresh.keys() - old.keys()),
        "changed": sorted(
            mk for mk in fresh.keys() & old.keys() if fresh[mk] != old[mk]
        ),
        "removed_service": sorted((old.keys() - fresh.keys()) & allowed_removed),
        "missing": unexplained,
        "sessions": sum(len(c["session_ids"]) for c in new_chats),
    }
    if unexplained:
        return info, raw, [], ""
    tps = copy.deepcopy([t for t in data["tps"] if not message_key(t)]) + sorted(
        fresh.values(), key=lambda tp: (tp["timestamp"] or "", tp["touchpoint_id"])
    )
    if len({t["touchpoint_id"] for t in tps}) != len(tps):
        raise RefreshError("DUPLICATE_TOUCHPOINT")
    raw["ol_chats"] = new_chats
    md = export_llm.deal_markdown(
        data["deal"],
        tps,
        baseline["users"],
        baseline["stage_books"][entity],
        entity=entity,
    )
    return info, raw, tps, md


def prepare(root: Path, output: Path, client, *, databases=(), max_calls=5000) -> dict:
    """Прочитать только ОЛ и создать изолированный комплект; рабочие файлы не писать."""
    import config
    import export_llm
    import fetch_openlines
    from logging_setup import log

    root, output = root.resolve(), output.resolve()
    baseline = load_baseline(root)
    if not baseline["manifest"]["is_complete"]:
        raise RefreshError("PARTIAL_BASELINE")
    # Защита от обновления общей папки данными другого портала, например .env.dev.
    expected_cards = {"export/snapshot/manifest.json"}
    portal = urlsplit(config.PORTAL_URL).netloc.casefold()
    for (entity, did), data in baseline["entities"].items():
        folder = (
            root
            / "export/snapshot"
            / export_llm.ENTITY_DIR[entity]
            / export_llm.GROUP_DIR[data["group"]]
        )
        cards = list(folder.glob(f"*_{did}_SNAP_*.md"))
        if len(cards) != 1:
            raise RefreshError("BASELINE_CARD_MISMATCH")
        text = cards[0].read_text(encoding="utf-8")
        crm = re.search(r"^\*\*CRM:\*\* (https?://\S+)", text, re.MULTILINE)
        if not crm or not portal or urlsplit(crm[1]).netloc.casefold() != portal:
            raise RefreshError("PORTAL_MISMATCH")
        expected_cards.add(cards[0].relative_to(root).as_posix())
    observed = {
        name
        for name in snapshot_files(root)
        if Path(name).name != ".DS_Store" and not Path(name).name.startswith("._")
    }
    if observed != expected_cards:
        raise RefreshError("BASELINE_CARD_MISMATCH")
    if (
        export_llm.ExportContext().schema_version
        != baseline["manifest"]["schema_version"]
    ):
        raise RefreshError("SCHEMA_MISMATCH")
    client = CallBudget(client, max_calls)
    databases = list(databases)
    check_databases(root, databases)
    report = {
        "version": 1,
        "root": str(root),
        "status": "preparing",
        "baseline_export_id": baseline["manifest"]["export_id"],
        "guard": source_guard(root, baseline, databases),
        "snapshot_files": snapshot_files(root),
        "databases": databases,
        "entities": [],
        "errors": [],
        "operations": [],
    }
    save_json(output / "report.json", report)
    candidate = output / "candidate"
    history = fetch_openlines.OpenLinesHistory(client)
    ctx = export_llm.ExportContext()
    if stamp(ctx.started.isoformat()) <= stamp(baseline["manifest"]["snapshot_at"]):
        ctx.started = (
            stamp(baseline["manifest"]["snapshot_at"]) + timedelta(seconds=1)
        ).astimezone(ctx.started.tzinfo)
    groups = defaultdict(list)
    grouped_tps = defaultdict(list)
    try:
        for key, data in baseline["entities"].items():
            entity, did = key
            try:
                info, raw, tps, rendered = prepare_entity(history, root, baseline, key)
                report["entities"].append(info)
                log.info(
                    "Обновление {} {}: {} -> {} сообщений, статус {}",
                    entity,
                    did,
                    info["before"],
                    info["after"],
                    info["status"],
                )
                if info["status"] != "complete":
                    continue
                base = f"data/store/{'lead/' if entity == 'lead' else ''}"
                stats = read_json(root / f"{base}index/{did}.json")
                stats.update(
                    touchpoints=len(tps),
                    ol_chats=len(raw["ol_chats"]),
                    group=data["group"],
                )
                stats.pop("reused", None)
                folder = (
                    Path("export/snapshot")
                    / export_llm.ENTITY_DIR[entity]
                    / export_llm.GROUP_DIR[data["group"]]
                )
                originals = list((root / folder).glob(f"*_{did}_SNAP_*.md"))
                if len(originals) != 1:
                    raise RefreshError("BASELINE_CARD_MISMATCH")
                md = timeline_only(
                    originals[0].read_bytes().decode("utf-8"), rendered, entity
                )
                name = re.sub(
                    r"_SNAP_[^_]+_schema-v\d+\.md$",
                    f"_SNAP_{ctx.snap}_schema-v{ctx.schema_version}.md",
                    originals[0].name,
                )
                atomic_bytes(candidate / folder / name, md.encode("utf-8"))
                atomic_bytes(candidate / f"{base}deals/{did}.md", md.encode("utf-8"))
                save_jsonl(candidate / f"{base}tps/{did}.jsonl", tps)
                save_json(candidate / f"{base}index/{did}.json", stats)
                save_json(candidate / f"raw/{entity}_{did}.json", raw)
                # Поля сущности не обновлялись: нужны только для подготовки source-SQLite.
                atomic_bytes(
                    candidate / f"{base}deal/{did}.json",
                    (root / f"{base}deal/{did}.json").read_bytes(),
                )
                groups[entity, data["group"]].append(stats)
                grouped_tps[entity, data["group"]].extend(tps)
            except Exception as error:  # noqa: BLE001 — подготовить отчёт и продолжить остальные сущности
                report["errors"].append(
                    {"entity": entity, "id": did, "code": error_code(error)}
                )
        if report["errors"] or any(
            item["status"] != "complete" for item in report["entities"]
        ):
            report["status"] = "blocked"
        else:
            finalize_candidate(
                root, output, candidate, baseline, ctx, groups, grouped_tps, report
            )
            check_sources(root, report)
            report["status"] = "ready"
    except Exception as error:  # noqa: BLE001 — сохранить диагностируемый отказ без частичного применения
        report["status"] = "blocked"
        report["errors"].append({"code": error_code(error)})
    finally:
        report["calls"] = client.calls
        report["summary"] = {
            name: sum(len(item[name]) for item in report["entities"])
            for name in ("added", "changed", "removed_service", "missing")
        }
        report["summary"]["entities"] = len(baseline["entities"])
        save_json(output / "report.json", report)
        rows = [
            "# Обновление переписок",
            "",
            f"Статус: {report['status']}",
            f"Исходный снимок: {report['baseline_export_id']}",
            "",
            json.dumps(report["summary"], ensure_ascii=False),
            "",
            "| Тип | ID | Было | Стало | Добавлено | Изменено | Удалено служебных | Не объяснено |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        rows.extend(
            f"| {e['entity']} | {e['id']} | {e['before']} | {e['after']} | {len(e['added'])} | {len(e['changed'])} | {len(e['removed_service'])} | {len(e['missing'])} |"
            for e in report["entities"]
        )
        atomic_bytes(output / "summary.md", ("\n".join(rows) + "\n").encode("utf-8"))
    return report


def finalize_candidate(
    root, output, candidate, baseline, ctx, groups, grouped_tps, report
):
    """Собрать индексы, новый ZIP и копии SQLite до изменения рабочего набора."""
    import export_llm
    import registry
    import snapshot_sqlite

    for entity in ("deal", "lead"):
        for group in export_llm.ALL_GROUPS:
            save_jsonl(
                candidate / f"export/index_{entity}_{group}.jsonl",
                groups[entity, group],
            )
            tps = sorted(
                grouped_tps[entity, group],
                key=lambda t: (
                    int(t["deal_id"]),
                    t["timestamp"] or "",
                    t["touchpoint_id"],
                ),
            )
            save_jsonl(candidate / f"export/touchpoints_{entity}_{group}.jsonl", tps)
            (
                candidate
                / "export/snapshot"
                / export_llm.ENTITY_DIR[entity]
                / export_llm.GROUP_DIR[group]
            ).mkdir(parents=True, exist_ok=True)
    manifest = copy.deepcopy(baseline["manifest"])
    manifest.update(
        export_id=ctx.export_id,
        snapshot_at=ctx.started.isoformat(timespec="seconds"),
        update_mode="CHAT_REFRESH",
        previous_export_id=manifest["export_id"],
        previous_snapshot_at=manifest["snapshot_at"],
    )
    manifest["chat_refresh"] = {
        "crm_snapshot_at": baseline["manifest"]["snapshot_at"],
        "scope": "accessible_sessions",
        "source_export_id": baseline["manifest"]["export_id"],
    }
    for block in ("deals", "leads"):
        for group in manifest[block]["groups"].values():
            group["reused"] = 0
    save_json(candidate / "export/snapshot/manifest.json", manifest)
    reg_path = root / "data/registry.json"
    reg = read_json(reg_path) if reg_path.exists() else {}
    for (entity, _), data in baseline["entities"].items():
        registry.mark(reg, data["deal"], data["group"], ctx, entity=entity)
    save_json(candidate / "data/registry.json", reg)
    report["export_id"] = ctx.export_id
    # Существующий архив никогда не перезаписывается, в том числе в ту же минуту.
    archive_name = f"Обновление переписок {ctx.snap}_{output.name}.zip"
    archive = candidate / "export" / archive_name
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as zip_file:
            snap = candidate / "export/snapshot"
            for directory in sorted(p for p in snap.rglob("*") if p.is_dir()):
                if not any(directory.iterdir()):
                    zip_file.writestr(directory.relative_to(snap).as_posix() + "/", "")
            for path in sorted(p for p in snap.rglob("*") if p.is_file()):
                zip_file.write(path, path.relative_to(snap).as_posix())
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    # Валидация всех карточек и восстановления текста выполняется даже без рабочей БД.
    snapshot_sqlite.import_snapshot(archive, output / "validation.db")
    for name in report["databases"]:
        destination = safe_path(candidate, name)
        source = safe_path(root, name)
        if source.exists():
            atomic_copy(source, destination)
        if name.endswith("sqlite3_structured.db"):
            import source_sqlite

            metadata = {
                entity: read_json(root / f"data/{entity}_fields_meta.json")
                for entity in ("deal", "lead")
                if manifest[entity + "s"]["total"]
            }
            source_sqlite.write_snapshot(
                destination,
                manifest,
                lambda: source_sqlite.store_records(
                    candidate / "data", candidate / "export", manifest
                ),
                metadata,
            )
        elif name == "data/sqlite3.db":
            from import_snapshot import import_snapshot

            import_snapshot(archive, destination)
        else:
            snapshot_sqlite.import_snapshot(archive, destination)
    report["archive"] = archive.relative_to(candidate).as_posix()
    paths = {
        p.relative_to(candidate).as_posix() for p in candidate.rglob("*") if p.is_file()
    }
    paths.update(report["snapshot_files"])
    for name in sorted(paths):
        before = report["guard"].get(name)
        if name not in report["guard"]:
            before = sha(safe_path(root, name))
            if before is not None:
                raise RefreshError("NEW_TARGET_ALREADY_EXISTS")
            report["guard"][name] = None
        after = sha(safe_path(candidate, name))
        if before != after:
            report["operations"].append(
                {"path": name, "before": before, "after": after}
            )


def load_report(root: Path, output: Path) -> dict:
    """Не разрешать применять чужой проект, произвольные пути или неполную подготовку."""
    if not output.resolve().is_relative_to((root / "data/chat_refresh").resolve()):
        raise RefreshError("RUN_OUTSIDE_PROJECT")
    report = read_json(output / "report.json")
    if (
        report.get("version") != 1
        or report.get("root") != str(root.resolve())
        or report.get("status") != "ready"
    ):
        raise RefreshError("PREPARATION_NOT_READY")
    paths = set()
    for operation in report["operations"]:
        name = operation["path"]
        safe_path(root, name)
        safe_path(output / "candidate", name)
        if (
            name in paths
            or name not in report["guard"]
            or operation["before"] != report["guard"][name]
        ):
            raise RefreshError("INVALID_OPERATION")
        paths.add(name)
    return report


def rollback(root: Path, output: Path) -> dict:
    """Восстановить весь набор после применения или обрыва, проверяя последующие правки."""
    report = load_report(root, output)
    journal = read_json(output / "journal.json")
    if journal["status"] == "rolled_back":
        return journal
    # Изменение якоря CRM после применения означает, что уже был другой прогон.
    operation_paths = {op["path"] for op in report["operations"]}
    allowed_snapshots = set(report["snapshot_files"]) | {
        name for name in operation_paths if name.startswith("export/snapshot/")
    }
    if not set(snapshot_files(root)).issubset(allowed_snapshots):
        raise RefreshError("SOURCES_CHANGED_AFTER_APPLY")
    for name, expected in report["guard"].items():
        if name not in operation_paths and sha(safe_path(root, name)) != expected:
            raise RefreshError("SOURCES_CHANGED_AFTER_APPLY")
    check_databases(root, report["databases"])
    for op in report["operations"]:
        if sha(safe_path(root, op["path"])) not in (op["before"], op["after"]):
            raise RefreshError("TARGET_CHANGED_AFTER_APPLY")
        if (
            op["before"] is not None
            and sha(safe_path(output / "backup", op["path"])) != op["before"]
        ):
            raise RefreshError("BACKUP_CHANGED")
    for op in reversed(report["operations"]):
        target = safe_path(root, op["path"])
        current = sha(target)
        if current not in (op["before"], op["after"]):
            raise RefreshError("TARGET_CHANGED_AFTER_APPLY")
        if current == op["before"]:
            continue
        if op["before"] is None:
            target.unlink(missing_ok=True)
        else:
            atomic_copy(safe_path(output / "backup", op["path"]), target)
    journal["status"] = "rolled_back"
    save_json(output / "journal.json", journal)
    return journal


def apply(root: Path, output: Path) -> dict:
    """Резервировать файлы до первой записи, применить комплект либо откатить его."""
    report = load_report(root, output)
    journal_path = output / "journal.json"
    if journal_path.exists():
        journal = read_json(journal_path)
        if journal["status"] == "applied":
            if any(
                sha(safe_path(root, op["path"])) != op["after"]
                for op in report["operations"]
            ):
                raise RefreshError("TARGET_CHANGED_AFTER_APPLY")
            return journal
        if journal["status"] != "rolled_back":
            raise RefreshError("ROLLBACK_REQUIRED")
    check_sources(root, report)
    check_databases(root, report["databases"])
    check_databases(output / "candidate", report["databases"])
    for op in report["operations"]:
        if sha(safe_path(output / "candidate", op["path"])) != op["after"]:
            raise RefreshError("CANDIDATE_CHANGED")
    for op in report["operations"]:
        if op["before"] is not None:
            atomic_copy(
                safe_path(root, op["path"]), safe_path(output / "backup", op["path"])
            )
            if sha(safe_path(output / "backup", op["path"])) != op["before"]:
                raise RefreshError("BACKUP_CHANGED")
    # Повторная сверка после резервирования: внешние инструменты могут не брать lock.
    check_sources(root, report)
    journal = {"status": "applying", "export_id": report["export_id"]}
    save_json(journal_path, journal)
    try:
        operations = sorted(
            report["operations"], key=lambda op: op["path"].endswith("manifest.json")
        )
        for op in operations:
            target = safe_path(root, op["path"])
            if sha(target) != op["before"]:
                raise RefreshError("TARGET_CHANGED_DURING_APPLY")
            if op["after"] is None:
                target.unlink(missing_ok=True)
            else:
                atomic_copy(safe_path(output / "candidate", op["path"]), target)
        if any(
            sha(safe_path(root, op["path"])) != op["after"]
            for op in report["operations"]
        ):
            raise RefreshError("APPLY_VERIFICATION_FAILED")
        journal["status"] = "applied"
        save_json(journal_path, journal)
    except BaseException:
        rollback(root, output)
        raise
    return journal


def main(argv=None, *, root=None) -> int:
    """По умолчанию показать план; сеть и применение требуют отдельных явных команд."""
    root = (Path(root) if root else Path(__file__).resolve().parent).resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Прочитать рабочий API и подготовить отдельный комплект",
    )
    mode.add_argument(
        "--apply",
        type=Path,
        metavar="RUN",
        help="Применить подготовленный каталог без API",
    )
    mode.add_argument(
        "--rollback",
        type=Path,
        metavar="RUN",
        help="Восстановить файлы из резервной копии без API",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=5000,
        help="Предел логических API-вызовов подготовки (5000)",
    )
    args = parser.parse_args(argv)
    if args.max_calls < 1:
        parser.error("--max-calls должен быть положительным")
    try:
        if args.apply or args.rollback:
            with exclusive_workspace(root / "data"):
                result = (apply if args.apply else rollback)(
                    root, (args.apply or args.rollback).resolve()
                )
            print("Статус:", result["status"])
            return 0
        baseline = load_baseline(root)
        print(
            "Исходный снимок:",
            baseline["manifest"]["export_id"],
            "; карточек:",
            len(baseline["entities"]),
        )
        if not args.prepare:
            print(
                "План без API и записи. --prepare: отдельный комплект; --apply RUN: применение; --rollback RUN: откат."
            )
            return 0
        import config
        from api_client import B24Client
        from logging_setup import configure_logging, log

        for attr, expected in (
            ("DATA_DIR", "data"),
            ("RAW_DIR", "raw"),
            ("EXPORT_DIR", "export"),
        ):
            if Path(getattr(config, attr)).resolve() != root / expected:
                raise RefreshError("NONSTANDARD_WORKSPACE_PATHS")
        databases = []
        if config.SNAPSHOT_SQLITE_ENABLED or Path(config.SNAPSHOT_SQLITE_PATH).exists():
            databases.append(
                Path(config.SNAPSHOT_SQLITE_PATH).resolve().relative_to(root).as_posix()
            )
        databases.extend(
            name
            for name in ("data/sqlite3_structured.db", "data/sqlite3.db")
            if (root / name).exists()
        )
        with exclusive_workspace(root / "data"):
            base = root / "data/chat_refresh"
            base.mkdir(parents=True, exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix="refresh_", dir=base))
            print("Подготовка:", output, flush=True)
            configure_logging(diagnostic_path=str(output / "run.log"))
            try:
                client = B24Client()
                try:
                    report = prepare(
                        root,
                        output,
                        client,
                        databases=databases,
                        max_calls=args.max_calls,
                    )
                finally:
                    client.session.close()
            finally:
                log.remove()
        print("Статус:", report["status"], report["summary"])
        return 0 if report["status"] == "ready" else 1
    except Exception as error:  # noqa: BLE001 — не выводить авторизованные URL и содержимое источников
        print("Обновление не выполнено:", error_code(error), file=sys.stderr)
        return 1
