"""
Диагностика метаданных полей сделки.

Запуск:  python diag_fields.py [UF_CRM_xxx UF_CRM_yyy ...]

Скрипт опрашивает оба источника метаданных и сохраняет результат
в data/diag_fields.json — пришлите этот файл (или вывод консоли),
если названия UF-полей не резолвятся.
"""

import json
import sys

import config
from api_client import B24Client
from logging_setup import run_cli

DEFAULT_TARGETS = [
    "UF_CRM_1760091836", "UF_CRM_1760432770",
    "UF_CRM_1693574738", "UF_CRM_1693574898",
    "UF_CRM_68306D2424CB2", "UF_CRM_66DABFA027C75",
]


def main():
    targets = sys.argv[1:] or DEFAULT_TARGETS
    client = B24Client()
    out = {"targets": targets}

    # --- 1) crm.deal.userfield.list (старый API)
    try:
        rows = client.call_list("crm.deal.userfield.list")
        out["deal_userfield_list"] = {
            "count": len(rows),
            "targets_found": [r for r in rows if r.get("FIELD_NAME") in targets],
            "sample": rows[:1],
        }
    except Exception as e:
        out["deal_userfield_list"] = {"error": str(e)}

    # --- 2) crm.item.fields (новый API, entityTypeId=2 = сделки)
    try:
        res = client.result("crm.item.fields",
                            {"entityTypeId": 2, "useOriginalUfNames": "Y"})
        fields = res.get("fields", res) if isinstance(res, dict) else {}
        out["item_fields"] = {
            "count": len(fields),
            "targets_found": {k: v for k, v in fields.items()
                              if any(k.startswith(t) for t in targets)},
            "uf_count": sum(1 for k in fields if k.startswith("UF_")),
        }
    except Exception as e:
        out["item_fields"] = {"error": str(e)}

    path = config.DATA_DIR + "/diag_fields.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(f"\nСохранено: {path}")


if __name__ == "__main__":
    run_cli(main)
