"""Head office settings that used to be fixed in the code: the company's details, how long a step may wait before it
shows as overdue, and the purchase order amount each role may approve on its own when the person has no limit of their
own.

Each group is one `OfficeSetting` row (models/masters.py). Nothing is stored until someone saves, so a new install
behaves exactly as before. The alert timings and role limits are also kept in memory where alerts_service and
purchasing_service read them (the same module values they always read), so every existing caller, including ones
outside this file's reach such as sending a shipment without the branch's answer, follows the saved values without a
database read each time. `warm()` loads them again at most once a minute (so a restored backup is picked up too);
saving updates them straight away.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.models import Role, User
from app.models.masters import OfficeSetting
from app.services import alerts_service, purchasing_service


class OfficeSettingsError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


COMPANY_KEY = "company"
ALERTS_KEY = "alert-timings"
LIMITS_KEY = "approval-limits"

# (field, longest, label)
COMPANY_FIELDS: list[tuple[str, int, str]] = [
    ("name", 120, "Company name"), ("legalName", 160, "Legal name"), ("ntn", 40, "NTN"), ("strn", 40, "Sales tax registration number"),
    ("address", 255, "Address"), ("city", 80, "City"), ("phone", 60, "Phone"), ("email", 180, "Email"),
]
COMPANY_DEFAULTS: dict = {field: None for field, _, _ in COMPANY_FIELDS}


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)


# What the software did before these could be changed; taken before anything is applied.
ALERT_DEFAULTS: dict[str, int] = {
    "offlineAfterHours": _hours(alerts_service.OFFLINE_AFTER),
    "dispatchOverdueHours": _hours(alerts_service.DISPATCH_OVERDUE),
    "decisionOverdueHours": _hours(alerts_service.APPROVAL_OVERDUE),
    "unansweredEscalateHours": _hours(alerts_service.UNANSWERED_ESCALATE),
    "inTransitChaseHours": _hours(alerts_service.IN_TRANSIT_CHASE),
    "inTransitEscalateHours": _hours(alerts_service.IN_TRANSIT_ESCALATE),
    "noticeDays": alerts_service.NOTICE_DAYS,
}
# field -> (smallest, largest, what it is called on screen)
ALERT_RANGES: dict[str, tuple[int, int, str]] = {
    "offlineAfterHours": (1, 168, "A branch counts as offline after"),
    "dispatchOverdueHours": (1, 168, "An approved shipment is overdue for dispatch after"),
    "decisionOverdueHours": (1, 720, "A decision is overdue after"),
    "unansweredEscalateHours": (1, 720, "A branch that hasn't answered a shipment reaches the Executive after"),
    "inTransitChaseHours": (1, 720, "Stock not received is chased after"),
    "inTransitEscalateHours": (1, 720, "Stock not received reaches the Executive after"),
    "noticeDays": (1, 365, "Notices stay for"),
}

LIMIT_DEFAULTS: dict[str, Decimal | None] = dict(purchasing_service.ROLE_PO_LIMITS)

# When the saved values were last put in place (monotonic seconds); 0 = not yet.
_loaded_at = 0.0
RELOAD_AFTER_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _row(key: str) -> OfficeSetting | None:
    return await OfficeSetting.get_or_none(key=key)


async def _save(key: str, value: dict, user: User) -> OfficeSetting:
    row = await _row(key)
    if row is None:
        return await OfficeSetting.create(key=key, value=value, updated_at=_now(), updated_by_name=user.name)
    row.value, row.updated_at, row.updated_by_name = value, _now(), user.name
    await row.save()
    return row


def _stamp(row: OfficeSetting | None) -> dict:
    return {"updatedAt": row.updated_at if row else None, "updatedBy": row.updated_by_name if row else None}


# ── company details ─────────────────────────────────────────────────────────────────────────────

async def company() -> dict:
    row = await _row(COMPANY_KEY)
    value = dict(COMPANY_DEFAULTS)
    if row and isinstance(row.value, dict):
        value.update({k: v for k, v in row.value.items() if k in COMPANY_DEFAULTS})
    return {**value, **_stamp(row)}


async def save_company(data: dict, user: User) -> dict:
    current = await company()
    value = {field: current.get(field) for field, _, _ in COMPANY_FIELDS}
    for field, longest, label in COMPANY_FIELDS:
        if field not in data:
            continue
        text = re.sub(r"\s+", " ", str(data[field] or "").strip()) or None
        if text and len(text) > longest:
            raise OfficeSettingsError(f"{label} can be at most {longest} characters.")
        value[field] = text
    if not value["name"]:
        raise OfficeSettingsError("Type the company name.")
    if value["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value["email"]):
        raise OfficeSettingsError("That email address doesn't look right.")
    await _save(COMPANY_KEY, value, user)
    return await company()


# ── alert timings ───────────────────────────────────────────────────────────────────────────────

def _alert_values(stored: dict | None) -> dict[str, int]:
    value = dict(ALERT_DEFAULTS)
    for field, (low, high, _) in ALERT_RANGES.items():
        raw = (stored or {}).get(field)
        if isinstance(raw, int) and low <= raw <= high:
            value[field] = raw
    return value


def _apply_alerts(value: dict[str, int]) -> None:
    alerts_service.OFFLINE_AFTER = timedelta(hours=value["offlineAfterHours"])
    alerts_service.DISPATCH_OVERDUE = timedelta(hours=value["dispatchOverdueHours"])
    alerts_service.APPROVAL_OVERDUE = timedelta(hours=value["decisionOverdueHours"])
    alerts_service.UNANSWERED_ESCALATE = timedelta(hours=value["unansweredEscalateHours"])
    alerts_service.IN_TRANSIT_CHASE = timedelta(hours=value["inTransitChaseHours"])
    alerts_service.IN_TRANSIT_ESCALATE = timedelta(hours=value["inTransitEscalateHours"])
    alerts_service.NOTICE_DAYS = value["noticeDays"]


async def alert_timings() -> dict:
    row = await _row(ALERTS_KEY)
    return {**_alert_values(row.value if row else None), "defaults": dict(ALERT_DEFAULTS), **_stamp(row)}


async def save_alert_timings(data: dict, user: User) -> dict:
    row = await _row(ALERTS_KEY)
    value = _alert_values(row.value if row else None)
    for field, (low, high, label) in ALERT_RANGES.items():
        if data.get(field) is None:
            continue
        try:
            number = int(data[field])
        except (TypeError, ValueError):
            raise OfficeSettingsError(f"{label}: type a whole number.")
        if not low <= number <= high:
            unit = "days" if field == "noticeDays" else "hours"
            raise OfficeSettingsError(f"{label}: {low} to {high} {unit}.")
        value[field] = number
    if value["inTransitEscalateHours"] < value["inTransitChaseHours"]:
        raise OfficeSettingsError("Stock not received reaches the Executive after the Warehouse Manager chases it, not before.")
    await _save(ALERTS_KEY, value, user)
    _apply_alerts(value)
    return await alert_timings()


# ── usual purchase approval limit of each role ──────────────────────────────────────────────────

def _limit_values(stored: dict | None) -> dict[str, Decimal | None]:
    """role id -> the most a person in that role may approve with no limit of their own. None = no limit; a role not
    named can't approve on its own (0), as before."""
    value: dict[str, Decimal | None] = dict(LIMIT_DEFAULTS)
    if isinstance(stored, dict):
        value = {}
        for role_id, raw in stored.items():
            if raw is None:
                value[role_id] = None
                continue
            try:
                value[role_id] = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue
    return value


def _apply_limits(value: dict[str, Decimal | None]) -> None:
    purchasing_service.ROLE_PO_LIMITS.clear()
    purchasing_service.ROLE_PO_LIMITS.update(value)


def _money(v: Decimal | None) -> str | None:
    return None if v is None else format(Decimal(v).quantize(Decimal("0.01")), "f")


async def approval_limits() -> dict:
    row = await _row(LIMITS_KEY)
    value = _limit_values(row.value if row else None)
    roles = await Role.all().order_by("name")
    return {
        "roles": [
            {"roleId": r.id, "roleName": r.name, "noLimit": r.id in value and value[r.id] is None,
             "limit": _money(value.get(r.id, Decimal("0"))) if value.get(r.id, Decimal("0")) is not None else None,
             "people": await User.filter(role_id=r.id, active=True).count(),
             "peopleWithOwnLimit": await User.filter(role_id=r.id, active=True, po_limit__isnull=False).count()}
            for r in roles
        ],
        **_stamp(row),
    }


async def save_approval_limits(rows: list[dict], user: User) -> dict:
    row = await _row(LIMITS_KEY)
    value = _limit_values(row.value if row else None)
    known = {r.id: r.name for r in await Role.all()}
    for item in rows:
        role_id = str(item.get("roleId") or "")
        if role_id not in known:
            raise OfficeSettingsError(f"There's no role {role_id}.")
        if item.get("noLimit"):
            value[role_id] = None
            continue
        try:
            amount = Decimal(str(item.get("limit") if item.get("limit") is not None else "0"))
        except (InvalidOperation, ValueError):
            raise OfficeSettingsError(f"{known[role_id]}: type an amount in rupees.")
        if amount < 0:
            raise OfficeSettingsError(f"{known[role_id]}: the limit can't be below zero.")
        value[role_id] = amount.quantize(Decimal("0.01"))
    await _save(LIMITS_KEY, {k: _money(v) for k, v in value.items()}, user)
    _apply_limits(value)
    return await approval_limits()


# ── loading ─────────────────────────────────────────────────────────────────────────────────────

async def warm(force: bool = False) -> None:
    """Puts saved alert timings and role limits in place. Reads the database at most once a minute; alerts_service and
    purchasing_service call it before they use the values."""
    global _loaded_at
    if not force and _loaded_at and time.monotonic() - _loaded_at < RELOAD_AFTER_SECONDS:
        return
    alerts = await _row(ALERTS_KEY)
    _apply_alerts(_alert_values(alerts.value if alerts else None))
    limits = await _row(LIMITS_KEY)
    _apply_limits(_limit_values(limits.value if limits else None))
    _loaded_at = time.monotonic()
