"""Отдельная пересборка чатов существующего снимка по всем доступным сессиям.

Без --execute только показывает план. Рабочие raw/store/snapshot не изменяет.
Порядок запуска и ограничения: docs/operations.md#сравнение-всех-сессий-чатов.
"""

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from chat_refresh import load_baseline


class ComparisonError(Exception):
    """Неполнота, которую нельзя трактовать как отсутствие сессий."""


def stamp(value: str) -> datetime:
    """Разобрать дату API, сохраняя однозначность часового пояса."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ComparisonError("DATE_WITHOUT_TIMEZONE")
    return result.astimezone(UTC)


def read_json(path: Path):
    """Прочитать существующий JSON без импорта настроек приложения."""
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    """Прочитать касания или индекс снимка."""
    return [
        json.loads(s)
        for s in path.read_text(encoding="utf-8").splitlines()
        if s.strip()
    ]


def atomic_write(path: Path, text: str) -> None:
    """Опубликовать только завершённый файл в новом каталоге сравнения."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path: Path, value) -> None:
    """Атомарно сохранить JSON без экранирования русского текста."""
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def save_summary(output: Path, report: dict) -> None:
    """Сформировать обзор без текстов переписки, имён и авторизованных ссылок."""
    lines = [
        "# Сравнение сессий чатов",
        "",
        f"Статус: {report['status']}.",
        f"Исходный снимок: {report['baseline_export_id']}.",
        f"Сущностей в исходном снимке: {report['target_entities']}.",
        "",
    ]
    if report["status"] != "complete":
        lines += [
            "Сравнение неполное; нулевые счётчики не подтверждают отсутствие новых диалогов.",
            "",
        ]
    lines += [
        f"Итоги: `{json.dumps(report['summary'], ensure_ascii=False)}`.",
        "",
        "Сообщения до исходного снимка могут также отсутствовать из-за переноса старых карточек из store; причина добавления не определяется автоматически.",
        "",
        "| Тип | ID | Сессий | Добавлено до снимка | Добавлено после снимка | Новых чатов | Статус |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in report["entities"]:
        lines.append(
            f"| {item['entity']} | {item['id']} | {sum(item['chat_sessions'].values())} | {item['added_before_baseline']} | {item['added_after_baseline']} | {len(item['new_chat_ids'])} | {item['status']} |"
        )
    if report["errors"]:
        lines += ["", "Ошибки: " + ", ".join(report["errors"]) + "."]
    lines += [
        "",
        "Подробные ID и ошибки сущностей: report.json. Карточки и JSONL находятся в подкаталогах deal/lead.",
        "",
    ]
    atomic_write(output / "comparison.md", "\n".join(lines))


def error_code(error: Exception) -> str:
    """Вернуть только код: описание API может содержать секреты или переписку."""
    code = getattr(error, "error", None)
    if isinstance(error, ComparisonError):
        code = str(error)
    return (
        code
        if isinstance(code, str) and re.fullmatch(r"[A-Z_0-9]+", code)
        else type(error).__name__
    )


def message_key(tp: dict) -> tuple[int, int] | None:
    """Отделить сообщения открытых линий от остальных касаний."""
    source = tp.get("source") or {}
    if (
        tp.get("event_type") == "message"
        and source.get("chat_id")
        and source.get("message_id")
    ):
        return int(source["chat_id"]), int(source["message_id"])
    return None


class BudgetClient:
    """Ограничить общее число логических вызовов поверх повторов B24Client."""

    def __init__(self, client, limit: int):
        self.client, self.limit, self.calls = client, limit, 0

    def result(self, method, params=None):
        """Передать запрос только через общий REST-клиент."""
        if self.calls >= self.limit:
            raise ComparisonError("CALL_BUDGET_EXCEEDED")
        self.calls += 1
        return self.client.result(method, params)


def rebuild(client, baseline: dict, output: Path, *, max_calls: int = 5000) -> dict:
    """Пересобрать только слой чатов и записать независимые карточки и сравнение."""
    import export_llm
    import fetch_openlines
    import normalize
    from logging_setup import log

    client = BudgetClient(client, max_calls)
    cutoff = datetime.now(UTC)
    report = {
        "baseline_export_id": baseline["manifest"]["export_id"],
        "baseline_snapshot_at": baseline["manifest"]["snapshot_at"],
        "scope": "accessible_sessions",
        "target_entities": len(baseline["entities"]),
        "started_at": cutoff.isoformat(),
        "status": "running",
        "entities": [],
        "errors": [],
    }
    report_path = output / "report.json"
    save_json(report_path, report)
    rows = baseline["entities"]
    chats = {key: copy.deepcopy(value["chats"]) for key, value in rows.items()}
    entity_errors = defaultdict(list)
    cache = {}
    try:
        # Сначала проверить новый источник: при отказе тарифа не опрашивать все CRM-сущности.
        sessions = fetch_openlines.collect_sessions(client, cutoff)
        report["available_sessions"] = len(sessions)
        for key in rows:
            entity, did = key
            try:
                bindings = client.result(
                    "imopenlines.crm.chat.get",
                    {"CRM_ENTITY_TYPE": entity, "CRM_ENTITY": did, "ACTIVE_ONLY": "N"},
                )
                if not isinstance(bindings, list):
                    raise ComparisonError("INVALID_CHAT_BINDINGS")
                for item in bindings:
                    if not item.get("CHAT_ID"):
                        continue
                    cid = int(item["CHAT_ID"])
                    conn = (item.get("CONNECTOR_ID") or "").lower()
                    chats[key][cid] = {
                        "chat_id": cid,
                        "channel": fetch_openlines.CONNECTOR_CHANNEL.get(
                            conn, conn or "unknown"
                        ),
                        "connector_title": item.get("CONNECTOR_TITLE") or conn,
                    }
            except Exception as error:  # noqa: BLE001 — сохранить сравнение остальных сущностей
                entity_errors[key].append(error_code(error))

        by_chat = defaultdict(list)
        for sid, session in sessions.items():
            cid = int(session["chatId"])
            by_chat[cid].append(sid)
            if session.get("crmEntityType") in ("deal", "lead") and session.get(
                "crmEntityId"
            ):
                key = session["crmEntityType"], int(session["crmEntityId"])
                if key in rows:
                    conn = session.get("source") or "unknown"
                    chats[key].setdefault(
                        cid,
                        {
                            "chat_id": cid,
                            "channel": fetch_openlines.CONNECTOR_CHANNEL.get(
                                conn, conn
                            ),
                        },
                    )

        users, stage_books = baseline["users"], baseline["stage_books"]
        baseline_time = stamp(report["baseline_snapshot_at"])
        for key, data in rows.items():
            entity, did = key
            before = {message_key(tp): tp for tp in data["tps"] if message_key(tp)}
            fresh, times, session_counts = {}, {}, {}
            failures = entity_errors[key]
            for cid, chat in sorted(chats[key].items()):
                sids = sorted(by_chat.get(cid, []))
                session_counts[str(cid)] = len(sids)
                if not sids:
                    failures.append("CHAT_WITHOUT_VISIBLE_SESSIONS")
                for sid in sids:
                    if sid not in cache:
                        try:
                            messages, info = fetch_openlines.get_chat_history(
                                client, cid, session_id=sid
                            )
                            cache[sid] = (messages, info)
                            save_json(
                                output / "sessions" / f"{sid}.json",
                                {
                                    "session_id": sid,
                                    "chat_id": cid,
                                    "messages": messages,
                                },
                            )
                        except Exception as error:  # noqa: BLE001 — следующий диалог обрабатывается независимо
                            cache[sid] = error_code(error)
                    history = cache[sid]
                    if isinstance(history, str):
                        failures.append(history)
                        continue
                    messages, info = history
                    for msg in messages:
                        if not msg.get("text") or fetch_openlines.is_system_message(
                            msg
                        ):
                            continue
                        mk = cid, int(msg["id"])
                        sender = fetch_openlines.classify_sender(
                            msg["sender_id"],
                            users,
                            info,
                            data["deal"].get("_contact_name"),
                        )
                        fresh[mk] = normalize.tp_from_ol_message(
                            data["deal"],
                            chat,
                            msg,
                            sender,
                            data["history"],
                            stage_books[entity],
                        )
                        times[mk] = msg.get("time")
            added, missing = (
                sorted(fresh.keys() - before.keys()),
                sorted(before.keys() - fresh.keys()),
            )
            older = newer = unknown = 0
            for mk in added:
                try:
                    if stamp(times[mk]) <= baseline_time:
                        older += 1
                    else:
                        newer += 1
                except (TypeError, ValueError, ComparisonError):
                    unknown += 1
            if missing:
                failures.append("BASELINE_MESSAGES_NOT_RETURNED")
            # Существующие касания и их авторы сохраняются; добавляются только новые ID.
            rebuilt = copy.deepcopy(data["tps"]) + [fresh[mk] for mk in added]
            rebuilt.sort(key=lambda tp: (tp["timestamp"] or "", tp["touchpoint_id"]))
            target = output / entity / data["group"] / str(did)
            atomic_write(
                target.with_suffix(".jsonl"),
                "".join(json.dumps(tp, ensure_ascii=False) + "\n" for tp in rebuilt),
            )
            atomic_write(
                target.with_suffix(".md"),
                export_llm.deal_markdown(
                    data["deal"], rebuilt, users, stage_books[entity], entity=entity
                ),
            )
            item = {
                "entity": entity,
                "id": did,
                "status": "partial" if failures else "complete",
                "chat_sessions": session_counts,
                "new_chat_ids": sorted(chats[key].keys() - data["chats"].keys()),
                "baseline_messages": len(before),
                "added_message_ids": added,
                "added_before_baseline": older,
                "added_after_baseline": newer,
                "added_unknown_date": unknown,
                "not_returned_message_ids": missing,
                "errors": sorted(set(failures)),
            }
            report["entities"].append(item)
            log.info(
                "Сравнение {} {}: сессий {}, добавлено сообщений {}, статус {}",
                entity,
                did,
                sum(session_counts.values()),
                len(added),
                item["status"],
            )
    except Exception as error:  # noqa: BLE001 — даже при глобальном сбое нужен отчёт неполноты
        report["errors"].append(error_code(error))
    finally:
        report["calls"] = client.calls
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["status"] = (
            "complete"
            if len(report["entities"]) == len(rows)
            and not report["errors"]
            and all(e["status"] == "complete" for e in report["entities"])
            else "partial"
        )
        report["summary"] = {
            "entities": len(report["entities"]),
            "changed_entities": sum(
                bool(e["added_message_ids"] or e["new_chat_ids"])
                for e in report["entities"]
            ),
            "added_messages": sum(
                len(e["added_message_ids"]) for e in report["entities"]
            ),
            "added_before_baseline": sum(
                e["added_before_baseline"] for e in report["entities"]
            ),
            "added_after_baseline": sum(
                e["added_after_baseline"] for e in report["entities"]
            ),
            "added_unknown_date": sum(
                e["added_unknown_date"] for e in report["entities"]
            ),
            "not_returned_messages": sum(
                len(e["not_returned_message_ids"]) for e in report["entities"]
            ),
            "partial_entities": sum(
                e["status"] == "partial" for e in report["entities"]
            ),
            "compared_sessions": len(cache),
            "failed_sessions": sum(isinstance(value, str) for value in cache.values()),
            "chats_with_multiple_sessions": len(
                {
                    cid
                    for item in report["entities"]
                    for cid, count in item["chat_sessions"].items()
                    if count > 1
                }
            ),
            "new_chats": len(
                {cid for item in report["entities"] for cid in item["new_chat_ids"]}
            ),
        }
        save_json(report_path, report)
        save_summary(output, report)
    return report


def main(argv=None) -> int:
    """Показать план либо явно выполнить чтение рабочего REST API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Прочитать рабочий Bitrix24 и создать отдельное сравнение",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=5000,
        help="Бюджет логических API-вызовов (по умолчанию 5000)",
    )
    args = parser.parse_args(argv)
    if args.max_calls < 1:
        parser.error("--max-calls должен быть положительным")
    try:
        baseline = load_baseline(ROOT)
    except (OSError, ValueError, KeyError, StopIteration, ComparisonError):
        print(
            "Исходный снимок отсутствует или не согласован со store/raw.",
            file=sys.stderr,
        )
        return 2
    counts = Counter(entity for entity, _ in baseline["entities"])
    print(
        f"Исходный снимок: {baseline['manifest']['export_id']}; сделки: {counts['deal']}, лиды: {counts['lead']}"
    )
    if not args.execute:
        print(
            "План: прочитать привязки чатов и все доступные сессии, пересобрать только чаты в отдельном каталоге data/session_comparisons/."
        )
        print(
            "Для запуска рабочего API добавьте --execute. Текущие raw/store/snapshot/state/registry не перезаписываются."
        )
        return 0

    import config
    from api_client import B24Client
    from logging_setup import configure_logging, log

    base = Path(config.DATA_DIR) / "session_comparisons"
    base.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="sessions_", dir=base))
    configure_logging(diagnostic_path=str(output / "run.log"))
    print(f"Результат сравнения: {output}", flush=True)
    try:
        client = B24Client()
        try:
            report = rebuild(client, baseline, output, max_calls=args.max_calls)
        finally:
            client.session.close()
        print(
            json.dumps(
                {"status": report["status"], **report["summary"]}, ensure_ascii=False
            )
        )
        return 0 if report["status"] == "complete" else 1
    except Exception as error:  # noqa: BLE001 — не печатать секретные описания API в консоль
        print(f"Сравнение не завершено: {error_code(error)}", file=sys.stderr)
        return 2
    finally:
        log.remove()


if __name__ == "__main__":
    raise SystemExit(main())
