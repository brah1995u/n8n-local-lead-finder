import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import SETTINGS, secrets


class ServiceError(Exception):
    def __init__(self, message, stage="service", ambiguous=False):
        self.stage, self.ambiguous = stage, ambiguous
        super().__init__(message)


class Apify:
    def __init__(self):
        token = secrets().get("apify_token")
        if not token:
            raise ServiceError("Потрібен Apify API-токен у Підключеннях", "apify")
        self.client = httpx.Client(base_url="https://api.apify.com/v2/", headers={"Authorization": "Bearer " + token}, timeout=45)

    def get(self, path, **params):
        for attempt in range(3):
            try:
                r = self.client.get(path, params=params)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise ServiceError(f"Apify HTTP {r.status_code}: перевір доступ і ліміти", "apify")
                data = r.json()
                return data.get("data", data) if isinstance(data, dict) else data
            except httpx.HTTPError:
                if attempt == 2:
                    raise ServiceError("Apify недоступний; результати збережено для продовження", "apify") from None
                time.sleep(2 ** attempt)

    def budget(self):
        user = self.get("users/me")
        limits = self.get("users/me/limits")
        plan = user.get("plan", {})
        if plan.get("tier", "").upper() != "FREE":
            raise ServiceError("Безкоштовний тариф Apify не підтверджений. Збір призупинено", "apify")
        credit = float(plan.get("monthlyUsageCreditsUsd", 0))
        used = limits.get("current", {}).get("monthlyUsageUsd")
        if not credit or used is None:
            raise ServiceError("Apify не повернув кредит/використання; витрати заблоковані", "apify")
        cycle = limits["monthlyUsageCycle"]
        remaining = max(0.0, min(SETTINGS["apify_monthly_budget_usd"] - float(used), credit - SETTINGS["apify_reserve_usd"] - float(used)))
        info = {"cycle": cycle, "credit_usd": credit, "used_usd": float(used), "available_usd": round(remaining, 4), "checked_at": datetime.now().isoformat()}
        db.put("apify_budget", info)
        return info

    def start(self, actor, payload, cap):
        # Never automatically retry this POST: a lost response can still mean a paid run started.
        try:
            r = self.client.post(f"acts/{actor}/runs", params={"maxTotalChargeUsd": round(cap, 4), "timeout": 900, "memory": 1024}, json=payload, timeout=60)
        except httpx.HTTPError:
            raise ServiceError("Відповідь запуску втрачено: шукаємо вже створений runId", "apify", ambiguous=True) from None
        if r.status_code >= 500:
            raise ServiceError("Невизначена відповідь запуску: потрібна звірка runId", "apify", ambiguous=True)
        if r.status_code >= 400:
            raise ServiceError(f"Apify відхилив запуск (HTTP {r.status_code}); перевір вхідні параметри/квоту", "apify")
        return r.json()["data"]

    def minimum_cap(self, actor):
        metadata = self.get('acts/' + actor)
        now = datetime.now(timezone.utc)
        prices = [p for p in metadata.get('pricingInfos', [])
                  if not p.get('startedAt') or datetime.fromisoformat(p['startedAt'].replace('Z', '+00:00')) <= now]
        if not prices:
            raise ServiceError('Apify не повернув чинні умови запуску; витрати заблоковані', 'apify')
        current = max(prices, key=lambda p: p.get('startedAt', ''))
        minimum = max(0.05, float(current.get('minimalMaxTotalChargeUsd') or 0))
        db.put('apify_actor_minimum:' + actor, {'usd': minimum, 'effective_at': current.get('startedAt')})
        return minimum


class Airtable:
    def __init__(self):
        token = secrets().get("airtable_token")
        if not token:
            raise ServiceError("Потрібен Airtable PAT у Підключеннях", "airtable")
        self.client = httpx.Client(base_url="https://api.airtable.com/v0/", headers={"Authorization": "Bearer " + token}, timeout=40)

    def request(self, method, path, **kwargs):
        cycle = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m")
        for attempt in range(3):
            with db.connect() as c:
                c.execute("INSERT INTO budgets(service,cycle) VALUES('airtable',%s) ON CONFLICT DO NOTHING", (cycle,))
                row = c.execute("UPDATE budgets SET calls=calls+1 WHERE service='airtable' AND cycle=%s AND calls < %s RETURNING calls", (cycle, SETTINGS["airtable_monthly_calls"])).fetchone()
                if not row:
                    raise ServiceError("Місячний бюджет Airtable API вичерпано; черга збережена", "airtable")
            time.sleep(0.55)
            try:
                r = self.client.request(method, path, **kwargs)
            except httpx.HTTPError:
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ServiceError("Airtable недоступний; синхронізацію буде повторено через upsert", "airtable") from None
            if r.status_code == 429:
                error_type = r.json().get("error", {}).get("type", "")
                if error_type == "PUBLIC_API_BILLING_LIMIT_EXCEEDED":
                    raise ServiceError("Спільний місячний ліміт Airtable workspace вичерпано", "airtable")
                if attempt < 2:
                    time.sleep(30)
                    continue
            if r.status_code >= 500 and method == "GET" and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise ServiceError(f"Airtable HTTP {r.status_code}; перевір доступ PAT до потрібних баз", "airtable")
            return r.json()

    def records(self, base=None, table=None):
        path = f"{base or SETTINGS['airtable_base']}/{table or SETTINGS['airtable_table']}"
        rows, params = [], {"pageSize": 100}
        while True:
            result = self.request("GET", path, params=params)
            rows.extend(result.get("records", []))
            if not result.get("offset"):
                return rows
            params["offset"] = result["offset"]

    def upsert(self, rows):
        return self.request("PATCH", f"{SETTINGS['airtable_base']}/{SETTINGS['airtable_table']}", json={"performUpsert": {"fieldsToMergeOn": ["Cortex ID"]}, "records": rows, "typecast": False})
