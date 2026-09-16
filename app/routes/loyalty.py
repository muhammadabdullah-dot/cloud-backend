"""Members and points rules in the Cloud App — every branch's members in one list, and the rules every
branch uses."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middlewares.auth import require_permission
from app.models import LoyaltyEntry, LoyaltySettings, Member, User
from app.schemas.types import Money
from app.services import loyalty_service

router = APIRouter(prefix="/loyalty", tags=["loyalty"])

_read = require_permission("admin.loyalty", "R")
_write = require_permission("admin.loyalty", "W")


class MemberOut(BaseModel):
    code: str
    name: str
    phone: str
    joinedVia: str
    joinedMethod: str | None = None
    homeBranchCode: str
    partyCode: str | None = None
    partyName: str | None = None
    points: int
    active: bool
    createdBy: str | None = None
    createdAt: datetime | None = None


class MemberListOut(BaseModel):
    items: list[MemberOut]
    total: int


class EntryOut(BaseModel):
    id: str
    kind: str
    points: int
    invoiceNumber: str | None = None
    branchCode: str
    note: str | None = None
    by: str | None = None
    at: datetime


class MemberDetailOut(MemberOut):
    entries: list[EntryOut]


class SettingsOut(BaseModel):
    enabled: bool
    rupeesPerPoint: Money
    pointValue: Money
    minRedeemPoints: int
    maxRedeemPercent: Money
    updatedAt: datetime | None = None
    updatedBy: str | None = None
    updatedFrom: str | None = None


class SettingsIn(BaseModel):
    enabled: bool | None = None
    rupeesPerPoint: Decimal | None = Field(default=None, gt=0)
    pointValue: Decimal | None = Field(default=None, gt=0)
    minRedeemPoints: int | None = Field(default=None, ge=0)
    maxRedeemPercent: Decimal | None = Field(default=None, gt=0, le=100)


def _member(m: Member) -> MemberOut:
    return MemberOut(
        code=m.code, name=m.name, phone=m.phone, joinedVia=m.joined_via, joinedMethod=m.joined_method,
        homeBranchCode=m.home_branch_code, partyCode=m.party_code, partyName=m.party_name, points=m.points_balance,
        active=m.active, createdBy=m.created_by_name, createdAt=m.created_at,
    )


def _settings(s: LoyaltySettings) -> SettingsOut:
    return SettingsOut(
        enabled=s.enabled, rupeesPerPoint=s.rupees_per_point, pointValue=s.point_value, minRedeemPoints=s.min_redeem_points,
        maxRedeemPercent=s.max_redeem_percent, updatedAt=s.updated_at, updatedBy=s.updated_by_name, updatedFrom=s.updated_from,
    )


@router.get("/members", response_model=MemberListOut)
async def members(q: str | None = None, branch: str | None = None, limit: int = 50, offset: int = 0, user: User = Depends(_read)) -> MemberListOut:
    """Every branch's members: search by code, name, mobile number or Party; filter by the branch that signed them up."""
    items, total = await loyalty_service.search(q, branch, min(max(limit, 1), 200), max(offset, 0))
    return MemberListOut(items=[_member(m) for m in items], total=total)


@router.get("/members/{code}", response_model=MemberDetailOut)
async def member(code: str, user: User = Depends(_read)) -> MemberDetailOut:
    try:
        m, entries = await loyalty_service.detail(code)
    except loyalty_service.LoyaltyError as exc:
        raise HTTPException(exc.status, exc.message)
    return MemberDetailOut(
        **_member(m).model_dump(),
        entries=[EntryOut(id=str(e.id), kind=e.kind, points=e.points, invoiceNumber=e.invoice_number, branchCode=e.branch_code,
                          note=e.note, by=e.by_name, at=e.at) for e in entries],
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings(user: User = Depends(_read)) -> SettingsOut:
    return _settings(await loyalty_service.settings())


@router.put("/settings", response_model=SettingsOut)
async def put_settings(payload: SettingsIn, user: User = Depends(_write)) -> SettingsOut:
    """New rules for every branch; each branch takes them at its next check with head office."""
    try:
        return _settings(await loyalty_service.update_settings(payload.model_dump(exclude_unset=True), user))
    except loyalty_service.LoyaltyError as exc:
        raise HTTPException(exc.status, exc.message)
