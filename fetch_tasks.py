# -*- coding: utf-8 -*-
"""
Задачи, поставленные по сделке.

Связь задача <-> сделка: поле UF_CRM_TASK со значением "D_<deal_id>".
Источник: tasks.task.list (постранично).
"""

import logging

from api_client import BitrixError

log = logging.getLogger("b24")

TASK_SELECT = [
    "ID", "TITLE", "DESCRIPTION", "STATUS", "REAL_STATUS",
    "RESPONSIBLE_ID", "CREATED_BY", "CREATED_BY_ID",
    "CREATED_DATE", "DEADLINE", "CLOSED_DATE", "START_DATE_PLAN", "END_DATE_PLAN",
]


def get_deal_tasks(client, deal_id, prefix="D"):
    """
    Все задачи, привязанные к сущности (по возрастанию даты создания).
    prefix: "D" — сделка (D_<id>), "L" — лид (L_<id>).
    """
    try:
        return client.call_list("tasks.task.list", {
            "order": {"CREATED_DATE": "ASC"},
            "filter": {"UF_CRM_TASK": f"{prefix}_{int(deal_id)}"},
            "select": TASK_SELECT,
        })
    except BitrixError as e:
        log.warning("%s_%s: tasks.task.list: %s", prefix, deal_id, e)
        return []


def task_field(task, *names):
    """Поле задачи с учётом разного регистра имён (ID/id, CREATED_BY/createdBy)."""
    for n in names:
        for variant in (n, n.lower(), n.title().replace("_", "")):
            if variant in task and task[variant] not in (None, ""):
                return task[variant]
    return None
