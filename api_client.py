# -*- coding: utf-8 -*-
"""
REST-клиент Bitrix24 для входящего вебхука.

- POST JSON на {webhook}/{method}
- пауза между запросами (rate limit облака ~2 запроса/сек)
- ретраи с экспоненциальной задержкой на QUERY_LIMIT_EXCEEDED / 429 / сетевых сбоях
- пагинация списочных методов через параметр start / поле next
"""

import time
import logging
import requests

import config

log = logging.getLogger("b24")


class BitrixError(Exception):
    def __init__(self, method, error, description=""):
        self.method = method
        self.error = error
        self.description = description
        super().__init__(f"{method}: {error} — {description}")


class B24Client:
    def __init__(self, webhook_url=None):
        raw = webhook_url or config.WEBHOOK_URL
        if not raw or "ВАШ_ПОРТАЛ" in raw or "USER_ID" in raw:
            raise SystemExit(
                "Не настроен WEBHOOK_URL. Задайте переменную окружения "
                "B24_WEBHOOK (или строку B24_WEBHOOK=... в файле .env "
                "рядом с config.py)."
            )
        url = raw.rstrip("/") + "/"
        self.base = url
        self.session = requests.Session()

    # ------------------------------------------------------------ низкий уровень
    def call(self, method, params=None):
        """Один вызов метода. Возвращает весь ответ (result, next, total, ...)."""
        url = self.base + method
        last_err = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.post(
                    url, json=params or {}, timeout=config.REQUEST_TIMEOUT
                )
            except requests.RequestException as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("Сеть: %s (%s), повтор через %ss", method, e, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("429 rate limit (%s), пауза %ss", method, wait)
                time.sleep(wait)
                continue

            try:
                data = resp.json()
            except ValueError:
                last_err = BitrixError(method, f"HTTP {resp.status_code}", resp.text[:300])
                time.sleep(2 ** attempt)
                continue

            if "error" in data:
                err = data.get("error", "")
                desc = data.get("error_description", "")
                if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
                    wait = 2 ** attempt
                    log.warning("%s (%s), пауза %ss", err, method, wait)
                    time.sleep(wait)
                    last_err = BitrixError(method, err, desc)
                    continue
                raise BitrixError(method, err, desc)

            time.sleep(config.REQUEST_PAUSE)
            return data

        raise BitrixError(method, "RETRIES_EXCEEDED", str(last_err))

    def result(self, method, params=None):
        """Только поле result."""
        return self.call(method, params).get("result")

    # ------------------------------------------------------------ пагинация
    def call_list(self, method, params=None):
        """
        Собирает все страницы списочного метода.
        Работает для методов, где result — это массив элементов
        (crm.deal.list, crm.activity.list, crm.stagehistory.list,
         voximplant.statistic.get, crm.status.list, user.get и т.п.).
        """
        items = []
        params = dict(params or {})
        start = 0
        while True:
            params["start"] = start
            data = self.call(method, params)
            res = data.get("result") or []
            if isinstance(res, dict):
                # crm.stagehistory.list -> {"items": [...]}, tasks.task.list -> {"tasks": [...]}
                for key in ("items", "tasks"):
                    if key in res:
                        res = res[key] or []
                        break
            if isinstance(res, list):
                items.extend(res)
            else:
                items.append(res)
            if "next" in data:
                start = data["next"]
            else:
                break
        return items

    def ping(self):
        """Проверка доступа: возвращает профиль пользователя вебхука."""
        return self.result("profile")

    @property
    def auth_key(self):
        """Ключ входящего вебхука из URL (.../rest/<user>/<key>/).

        Legacy: в коде не используется. DOWNLOAD_URL, который disk.file.get
        отдаёт вебхуку, уже содержит авторизацию в пути, поэтому
        transcribe.resolve_file_url скачивает его как есть, без ?auth=.
        Оставлено как запасной вариант для порталов, где URL придёт
        без авторизации (тогда понадобится ?auth=<ключ>).
        """
        return self.base.rstrip("/").rsplit("/", 1)[-1]
