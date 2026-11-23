# -*- coding: utf-8 -*-
"""
Восстановление data/registry.json из store после ПРЕРВАННОГО прогона.

Registry штатно пишется в конце выгрузки; если прогон умер посередине,
артефакты обработанных сделок уже лежат в data/store/, а реестр пуст/стар.
Скрипт просканирует store и допишет недостающие записи — тогда следующий
запуск переиспользует готовые сделки вместо повторной обработки.

Запуск: python recover_registry.py
"""

import time

import config
import registry
import store


def main():
    reg = registry.load()
    before = len(reg)
    added = skipped = 0
    for entity in ("deal", "lead"):
        for did in store.stored_ids(entity=entity):
            k = registry.key(entity, did)
            if k in reg:
                skipped += 1
                continue
            if not store.has_deal(did, entity=entity):
                continue
            try:
                stats = store.load_stats(did, entity=entity)
            except Exception:
                continue
            group = stats.get("group") or (
                "completed" if stats.get("closed") else "active")
            reg[k] = {
                "group": group,
                "entity": entity,
                "stage_id": "",
                "date_modify": stats.get("date_modify") or "",
                "exported_at": "RECOVERED_FROM_STORE",
                "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "schema_version": int(config.SCHEMA_VERSION),
            }
            added += 1
    registry.save(reg)
    print(f"registry.json: было {before}, добавлено {added} из store "
          f"(уже были: {skipped}), итого {len(reg)}")


if __name__ == "__main__":
    main()
