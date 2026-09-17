"""Admin > Company & Settings: the company's details, when waiting work counts as overdue, and the purchase order amount
each role may approve on its own. See services/office_settings_service.py.

The company details can be read by anyone signed in (a screen or a printout may need the company's name); changing them
and the alert timings needs the settings' own access. The role limits sit with the people who already set a person's
own limit on the Users tab.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middlewares.auth import get_current_user, require_any_permission, require_permission
from app.models import User
from app.services import office_settings_service as svc

router = APIRouter(prefix="/office", tags=["office-settings"])

OFFICE_SETTINGS_RESOURCE = "admin.office-settings"
_settings_read = require_permission(OFFICE_SETTINGS_RESOURCE, "R")
_settings_write = require_permission(OFFICE_SETTINGS_RESOURCE, "W")
_limits_read = require_any_permission(("admin.user-access", "R"), (OFFICE_SETTINGS_RESOURCE, "R"))
_limits_write = require_permission("admin.user-access", "W")


class Stamp(BaseModel):
    updatedAt: datetime | None = None
    updatedBy: str | None = None


class CompanyOut(Stamp):
    name: str | None = None
    legalName: str | None = None
    ntn: str | None = None
    strn: str | None = None
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    email: str | None = None


class CompanyIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    legalName: str | None = Field(default=None, max_length=160)
    ntn: str | None = Field(default=None, max_length=40)
    strn: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=60)
    email: str | None = Field(default=None, max_length=180)


class AlertTimings(BaseModel):
    offlineAfterHours: int
    dispatchOverdueHours: int
    decisionOverdueHours: int
    unansweredEscalateHours: int
    inTransitChaseHours: int
    inTransitEscalateHours: int
    noticeDays: int


class AlertTimingsOut(AlertTimings, Stamp):
    # What the software used before anyone changed them, for "Put back the usual timings".
    defaults: AlertTimings


class AlertTimingsIn(BaseModel):
    offlineAfterHours: int | None = None
    dispatchOverdueHours: int | None = None
    decisionOverdueHours: int | None = None
    unansweredEscalateHours: int | None = None
    inTransitChaseHours: int | None = None
    inTransitEscalateHours: int | None = None
    noticeDays: int | None = None


class RoleLimitOut(BaseModel):
    roleId: str
    roleName: str
    noLimit: bool
    # Rupees with paisa ("500000.00"); null when there is no limit.
    limit: str | None = None
    people: int
    # People in the role with a limit of their own, which wins over the role's.
    peopleWithOwnLimit: int


class ApprovalLimitsOut(Stamp):
    roles: list[RoleLimitOut]


class RoleLimitIn(BaseModel):
    roleId: str = Field(min_length=1, max_length=40)
    noLimit: bool = False
    limit: str | None = Field(default=None, max_length=20)


class ApprovalLimitsIn(BaseModel):
    roles: list[RoleLimitIn]


def _fail(exc: svc.OfficeSettingsError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/company", response_model=CompanyOut)
async def get_company(user: User = Depends(get_current_user)) -> CompanyOut:
    return CompanyOut(**await svc.company())


@router.put("/company", response_model=CompanyOut)
async def put_company(payload: CompanyIn, user: User = Depends(_settings_write)) -> CompanyOut:
    try:
        return CompanyOut(**await svc.save_company(payload.model_dump(exclude_unset=True), user))
    except svc.OfficeSettingsError as exc:
        raise _fail(exc)


@router.get("/alert-timings", response_model=AlertTimingsOut)
async def get_alert_timings(user: User = Depends(_settings_read)) -> AlertTimingsOut:
    return AlertTimingsOut(**await svc.alert_timings())


@router.put("/alert-timings", response_model=AlertTimingsOut)
async def put_alert_timings(payload: AlertTimingsIn, user: User = Depends(_settings_write)) -> AlertTimingsOut:
    """Takes effect straight away for everyone's task list and notices."""
    try:
        return AlertTimingsOut(**await svc.save_alert_timings(payload.model_dump(exclude_unset=True), user))
    except svc.OfficeSettingsError as exc:
        raise _fail(exc)


@router.get("/approval-limits", response_model=ApprovalLimitsOut)
async def get_approval_limits(user: User = Depends(_limits_read)) -> ApprovalLimitsOut:
    return ApprovalLimitsOut(**await svc.approval_limits())


@router.put("/approval-limits", response_model=ApprovalLimitsOut)
async def put_approval_limits(payload: ApprovalLimitsIn, user: User = Depends(_limits_write)) -> ApprovalLimitsOut:
    """The most a person in each role may approve when they have no limit of their own. A person's own limit on the
    Users tab still wins."""
    try:
        return ApprovalLimitsOut(**await svc.save_approval_limits([r.model_dump() for r in payload.roles], user))
    except svc.OfficeSettingsError as exc:
        raise _fail(exc)
