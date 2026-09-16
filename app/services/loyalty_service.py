"""Members and points at head office: taking what branches report, passing it on to the other branches,
and the points rules set from the Cloud App.

Every branch keeps its own copy of every member and every points entry, so points follow a member to any
branch, even one that's offline when they walk in (it knows what it last heard). Rules are the same
everywhere; whichever change was made last — at head office or at a branch — wins.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal

from tortoise.expressions import Q

from app.models import Branch, LoyaltyEntry, LoyaltySettings, Member, User
from app.services import downstream_service

HEAD_OFFICE = "HO"


class LoyaltyError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def member_payload(m: Member) -> dict:
    return {
        "code": m.code, "name": m.name, "phone": m.phone, "joinedVia": m.joined_via, "joinedMethod": m.joined_method,
        "homeBranchCode": m.home_branch_code, "active": m.active, "partyCode": m.party_code, "partyName": m.party_name,
        "createdBy": m.created_by_name, "createdAt": _iso(m.created_at), "updatedAt": _iso(m.updated_at),
    }


def entry_payload(e: LoyaltyEntry, member_code: str) -> dict:
    return {
        "id": str(e.id), "memberCode": member_code, "kind": e.kind, "points": e.points,
        "invoiceNumber": e.invoice_number, "branchCode": e.branch_code, "note": e.note, "by": e.by_name, "at": _iso(e.at),
    }


def settings_payload(s: LoyaltySettings) -> dict:
    return {
        "enabled": s.enabled, "rupeesPerPoint": str(s.rupees_per_point), "pointValue": str(s.point_value),
        "minRedeemPoints": s.min_redeem_points, "maxRedeemPercent": str(s.max_redeem_percent),
        "updatedAt": _iso(s.updated_at), "updatedBy": s.updated_by_name, "updatedFrom": s.updated_from,
    }


async def _other_branches(source: Branch | None) -> list[Branch]:
    qs = Branch.filter(verified_at__not_isnull=True)
    if source is not None:
        qs = qs.exclude(id=source.id)
    return await qs


async def _refresh_balance(member: Member) -> None:
    total = sum(await LoyaltyEntry.filter(member=member).values_list("points", flat=True))
    member.points_balance = total
    await Member.filter(id=member.id).update(points_balance=total)


# ── from a branch ────────────────────────────────────────────────────────────────────────────────

async def apply_member(branch: Branch, data: dict) -> str:
    code = (data.get("code") or "").strip().upper()
    if not code:
        raise LoyaltyError("A member event without a member code.")
    incoming_at = _dt(data.get("updatedAt"))
    member = await Member.get_or_none(code=code)
    home = await Branch.get_or_none(code=(data.get("homeBranchCode") or branch.code))
    fields = dict(
        name=(data.get("name") or code)[:120], phone=re.sub(r"\D", "", data.get("phone") or "")[:20],
        joined_via=data.get("joinedVia") or "till", joined_method=data.get("joinedMethod"),
        home_branch=home, home_branch_code=(data.get("homeBranchCode") or branch.code)[:10],
        party_code=data.get("partyCode"), party_name=data.get("partyName"), active=bool(data.get("active", True)),
        created_by_name=data.get("createdBy"), created_at=_dt(data.get("createdAt")), updated_at=incoming_at,
    )
    if member is None:
        member = await Member.create(code=code, **fields)
        outcome = "created"
    elif member.updated_at and incoming_at and incoming_at <= member.updated_at:
        return "older"
    else:
        for key, value in fields.items():
            setattr(member, key, value)
        await member.save()
        outcome = "updated"
    for other in await _other_branches(branch):
        await downstream_service.enqueue(str(other.id), "member.upsert", {"member": member_payload(member)})
    return outcome


async def apply_entry(branch: Branch, data: dict) -> str:
    entry_id = data.get("id")
    if not entry_id:
        raise LoyaltyError("A points event without an id.")
    if await LoyaltyEntry.exists(id=entry_id):
        return "duplicate"
    member = await Member.get_or_none(code=(data.get("memberCode") or "").upper())
    if member is None:
        raise LoyaltyError(f"Points for member {data.get('memberCode')}, whom head office hasn't heard of yet.")
    entry = await LoyaltyEntry.create(
        id=entry_id, member=member, kind=data.get("kind") or "adjust", points=int(data.get("points") or 0),
        invoice_number=data.get("invoiceNumber"), branch_code=(data.get("branchCode") or branch.code)[:10],
        note=data.get("note"), by_name=data.get("by"), at=_dt(data.get("at")) or datetime.now(timezone.utc),
    )
    await _refresh_balance(member)
    for other in await _other_branches(branch):
        # The member rides along, so a branch that joined after the member did can still take the points.
        await downstream_service.enqueue(
            str(other.id), "loyalty.entry", {"entry": entry_payload(entry, member.code), "member": member_payload(member)},
        )
    return "created"


async def settings() -> LoyaltySettings:
    current = await LoyaltySettings.get_or_none(id=1)
    return current or await LoyaltySettings.create(id=1)


def _apply_rules(s: LoyaltySettings, data: dict) -> None:
    if data.get("enabled") is not None:
        s.enabled = bool(data["enabled"])
    if data.get("rupeesPerPoint") is not None:
        s.rupees_per_point = Decimal(str(data["rupeesPerPoint"]))
    if data.get("pointValue") is not None:
        s.point_value = Decimal(str(data["pointValue"]))
    if data.get("minRedeemPoints") is not None:
        s.min_redeem_points = int(data["minRedeemPoints"])
    if data.get("maxRedeemPercent") is not None:
        s.max_redeem_percent = Decimal(str(data["maxRedeemPercent"]))


async def apply_settings(branch: Branch, data: dict) -> str:
    s = await settings()
    incoming_at = _dt(data.get("updatedAt"))
    if s.updated_at and incoming_at and incoming_at <= s.updated_at:
        return "older"
    _apply_rules(s, data)
    s.updated_at = incoming_at or datetime.now(timezone.utc)
    s.updated_by_name = data.get("updatedBy")
    s.updated_from = (data.get("updatedFrom") or branch.code)[:10]
    await s.save()
    for other in await _other_branches(branch):
        await downstream_service.enqueue(str(other.id), "loyalty.settings", {"settings": settings_payload(s)})
    return "updated"


async def seed_branch(branch: Branch) -> int:
    """Everything a newly set-up branch needs to honour members from anywhere: the rules, every member and
    every points entry. Applying any of it twice changes nothing, so a branch set up again is safe."""
    queued = 0
    s = await LoyaltySettings.get_or_none(id=1)
    if s and s.updated_at:
        await downstream_service.enqueue(str(branch.id), "loyalty.settings", {"settings": settings_payload(s)})
        queued += 1
    members = {m.id: m for m in await Member.all().order_by("created_at")}
    for member in members.values():
        await downstream_service.enqueue(str(branch.id), "member.upsert", {"member": member_payload(member)})
        queued += 1
    for entry in await LoyaltyEntry.all().order_by("at"):
        member = members.get(entry.member_id)
        if member:
            await downstream_service.enqueue(
                str(branch.id), "loyalty.entry", {"entry": entry_payload(entry, member.code), "member": member_payload(member)},
            )
            queued += 1
    return queued


# ── from the Cloud App ───────────────────────────────────────────────────────────────────────────

async def update_settings(data: dict, user: User) -> LoyaltySettings:
    s = await settings()
    if data.get("rupeesPerPoint") is not None and Decimal(str(data["rupeesPerPoint"])) <= 0:
        raise LoyaltyError("Rupees for one point must be more than zero.")
    if data.get("pointValue") is not None and Decimal(str(data["pointValue"])) <= 0:
        raise LoyaltyError("What a point is worth must be more than zero.")
    if data.get("maxRedeemPercent") is not None and not 0 < Decimal(str(data["maxRedeemPercent"])) <= 100:
        raise LoyaltyError("The share of a bill points may pay is 1 to 100%.")
    _apply_rules(s, data)
    s.updated_at = datetime.now(timezone.utc)
    s.updated_by_name = user.name
    s.updated_from = HEAD_OFFICE
    await s.save()
    for branch in await _other_branches(None):
        await downstream_service.enqueue(str(branch.id), "loyalty.settings", {"settings": settings_payload(s)})
    return s


async def search(q: str | None, branch_code: str | None, limit: int, offset: int) -> tuple[list[Member], int]:
    qs = Member.all()
    if branch_code:
        qs = qs.filter(home_branch_code=branch_code.upper())
    text = (q or "").strip()
    if text:
        predicate = Q(code__iexact=text) | Q(name__icontains=text) | Q(party_name__icontains=text)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 4:
            predicate |= Q(phone__contains=digits[-10:])
        qs = qs.filter(predicate)
    total = await qs.count()
    return await qs.order_by("-created_at").offset(offset).limit(limit), total


async def detail(code: str) -> tuple[Member, list[LoyaltyEntry]]:
    member = await Member.get_or_none(code=(code or "").upper())
    if not member:
        raise LoyaltyError("No member with that code.", 404)
    return member, await LoyaltyEntry.filter(member=member).order_by("-at").limit(200)
