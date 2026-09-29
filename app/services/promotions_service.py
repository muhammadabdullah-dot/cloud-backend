"""Writing a campaign at head office, and sending it to every branch.

Why head office and not a branch. The old software's campaign tables carry no branch at all: one list, written by
seven people, and every one of its five shops sold under it. Its shops share one database, so one decision reaches
every till by itself. Ours cannot, so the decision is taken here and travels down as a `promotion.upsert` message,
the same road a supplier already takes.

Three states, and the order between them is the whole point:

    draft  ->  approved  ->  live  ->  stopped

A draft is being written and reaches nobody. Approving it is a second person's act, and their own software has the
same step: 10,893 of their 11,364 campaigns are approved, 441 are not. Sending it makes it live at every branch.
Stopping it sends the same campaign down switched off, which is what a branch can act on, rather than deleting it and
leaving a till selling under something head office no longer has.

**A campaign is never edited once it is live.** A discount that changes underneath a bill already rung is a bill
nobody can explain. Stop it and write another; both are then on the record, which is what a shop needs when a
customer asks why the price was different yesterday.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from tortoise.transactions import atomic

from app.models import BranchPromotionStat, Product, Promotion
from app.models.promotion import KINDS
from app.services import downstream_service

ZERO = Decimal("0")


class PromotionError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def payload(promo: Promotion) -> dict:
    """What a branch is told. Its own `Promotion` takes these names, so nothing is translated on the way in."""
    return {
        "id": str(promo.id), "code": promo.code, "name": promo.name, "productSku": promo.product_sku,
        "startsOn": promo.starts_on.isoformat(), "endsOn": promo.ends_on.isoformat(), "kind": promo.kind,
        "discPercent": str(promo.disc_percent), "discFlat": str(promo.disc_flat),
        "promoPrice": str(promo.promo_price) if promo.promo_price is not None else None,
        "minQty": str(promo.min_qty),
        # A stopped campaign travels switched off rather than being deleted, so a branch stops selling under it and
        # can still say what it gave while it ran.
        "active": promo.state == "live",
        "remarks": promo.remarks, "rev": promo.rev,
    }


def _money(value, what: str) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception as exc:  # noqa: BLE001
        raise PromotionError(f"{what} has to be a number.") from exc


async def _check(data: dict, code: str | None = None) -> dict:
    kind = (data.get("kind") or "percent").strip().lower()
    if kind not in KINDS:
        raise PromotionError(f"A campaign is a percentage, a flat amount or a price, not {kind!r}.")
    sku = (data.get("productSku") or "").strip()
    product = await Product.get_or_none(sku=sku) if sku else None
    if product is None:
        raise PromotionError("Pick the Item the campaign is on.")
    starts = data.get("startsOn")
    ends = data.get("endsOn")
    if not starts or not ends:
        raise PromotionError("A campaign runs between two days. Give both.")
    starts = date.fromisoformat(str(starts)) if not isinstance(starts, date) else starts
    ends = date.fromisoformat(str(ends)) if not isinstance(ends, date) else ends
    if ends < starts:
        raise PromotionError("A campaign cannot end before it starts.")

    percent, flat = _money(data.get("discPercent"), "The percentage"), _money(data.get("discFlat"), "The flat amount")
    price = data.get("promoPrice")
    price = _money(price, "The price") if price not in (None, "") else None
    if kind == "percent" and not (ZERO < percent <= 100):
        raise PromotionError("A percentage campaign takes off between nothing and 100%.")
    if kind == "flat" and flat <= ZERO:
        raise PromotionError("A flat campaign has to take something off.")
    if kind == "price":
        if price is None or price <= ZERO:
            raise PromotionError("A price campaign needs the price the Item sells at while it runs.")
        if product.price is not None and price >= product.price:
            raise PromotionError(
                f"{product.name} already sells at {product.price}. A campaign at {price} would not take anything off.")

    name = (data.get("name") or "").strip()[:160]
    if not name:
        raise PromotionError("Give the campaign a name. It is what the till shows on the line.")
    new_code = (code or data.get("code") or "").strip().upper()[:20]
    if not new_code:
        raise PromotionError("Give the campaign a short code.")

    return {
        "code": new_code, "name": name, "product_sku": product.sku, "product_name": product.name,
        "starts_on": starts, "ends_on": ends, "kind": kind,
        "disc_percent": percent if kind == "percent" else ZERO,
        "disc_flat": flat if kind == "flat" else ZERO,
        "promo_price": price if kind == "price" else None,
        "min_qty": _money(data.get("minQty") or 1, "The smallest quantity"),
        "remarks": (data.get("remarks") or "").strip()[:255] or None,
    }


@atomic()
async def create(data: dict, user_name: str) -> Promotion:
    columns = await _check(data)
    if await Promotion.exists(code=columns["code"]):
        raise PromotionError(f"{columns['code']} is already a campaign. Give this one a different code.")
    return await Promotion.create(**columns, state="draft", created_by_name=user_name)


@atomic()
async def update(promo_id: str, data: dict, user_name: str) -> Promotion:
    promo = await _get(promo_id)
    if promo.state in ("live", "stopped"):
        raise PromotionError(
            f"{promo.code} has already gone to the branches, so it cannot be changed: a discount that moves under a "
            f"bill already rung is one nobody can explain. Stop it and write another.")
    columns = await _check(data, code=promo.code)
    for column, value in columns.items():
        setattr(promo, column, value)
    # Anything changed after an approval needs approving again.
    if promo.state == "approved":
        promo.state, promo.approved_by_name, promo.approved_at = "draft", None, None
        promo.remarks = ((promo.remarks + " · ") if promo.remarks else "") + f"Changed by {user_name} after approval"
    await promo.save()
    return promo


async def _get(promo_id: str) -> Promotion:
    promo = await Promotion.get_or_none(id=promo_id)
    if promo is None:
        raise PromotionError("That campaign doesn't exist.")
    return promo


@atomic()
async def approve(promo_id: str, user_name: str) -> Promotion:
    promo = await _get(promo_id)
    if promo.state != "draft":
        raise PromotionError(f"{promo.code} is {promo.state}, not a draft.")
    if promo.created_by_name and promo.created_by_name == user_name:
        raise PromotionError(
            f"{promo.code} was written by {user_name}. Somebody else approves a campaign, which is the whole point of "
            f"the step.")
    promo.state, promo.approved_by_name, promo.approved_at = "approved", user_name, datetime.now(timezone.utc)
    await promo.save()
    return promo


@atomic()
async def publish(promo_id: str, user_name: str) -> tuple[Promotion, int]:
    """Send an approved campaign to every branch that has joined."""
    promo = await _get(promo_id)
    if promo.state != "approved":
        raise PromotionError(
            f"{promo.code} is {promo.state}. Only an approved campaign goes to the tills."
            if promo.state != "live" else f"{promo.code} is already at the branches.")
    promo.rev += 1
    promo.state, promo.published_at = "live", datetime.now(timezone.utc)
    await promo.save()
    return promo, await _send(promo)


@atomic()
async def stop(promo_id: str, user_name: str) -> tuple[Promotion, int]:
    """Withdraw a live campaign. It goes down again switched off rather than being deleted."""
    promo = await _get(promo_id)
    if promo.state != "live":
        raise PromotionError(f"{promo.code} is not running, so there is nothing to stop.")
    promo.rev += 1
    promo.state, promo.stopped_by_name, promo.stopped_at = "stopped", user_name, datetime.now(timezone.utc)
    await promo.save()
    return promo, await _send(promo)


async def _send(promo: Promotion) -> int:
    """To every branch that has joined. A branch that joins later is caught up by `catch_up`."""
    from app.models import Branch

    sent = 0
    for branch in await Branch.filter(verified_at__isnull=False):
        await downstream_service.enqueue(str(branch.id), "promotion.upsert", {"promotion": payload(promo)})
        sent += 1
    return sent


async def catch_up(branch_id: str) -> int:
    """Every campaign a branch should know about, for one that has just joined or been away.

    Live and stopped ones, not drafts: a branch is told what is running and what has been withdrawn, and never about
    a campaign somebody is still writing."""
    sent = 0
    for promo in await Promotion.filter(state__in=("live", "stopped")).order_by("starts_on"):
        await downstream_service.enqueue(branch_id, "promotion.upsert", {"promotion": payload(promo)})
        sent += 1
    return sent


# ── what they gave away ──────────────────────────────────────────────────────────────────────────
async def given_away(promo: Promotion) -> dict:
    """What this campaign has cost, and where, from the figures the branches send."""
    rows = await BranchPromotionStat.filter(promotion_code=promo.code).prefetch_related("branch")
    by_branch: dict[str, dict] = {}
    for row in rows:
        entry = by_branch.setdefault(row.branch.code, {
            "branchCode": row.branch.code, "branchName": row.branch.name,
            "lines": 0, "qty": ZERO, "given": ZERO, "netSales": ZERO,
        })
        entry["lines"] += row.lines
        entry["qty"] += Decimal(str(row.qty))
        entry["given"] += Decimal(str(row.given))
        entry["netSales"] += Decimal(str(row.net_sales))
    branches = sorted(by_branch.values(), key=lambda b: -b["given"])
    for entry in branches:
        entry["qty"], entry["given"], entry["netSales"] = str(entry["qty"]), str(entry["given"]), str(entry["netSales"])
    return {
        "branches": branches,
        "lines": sum(r.lines for r in rows),
        "qty": str(sum((Decimal(str(r.qty)) for r in rows), ZERO)),
        "given": str(sum((Decimal(str(r.given)) for r in rows), ZERO)),
        "netSales": str(sum((Decimal(str(r.net_sales)) for r in rows), ZERO)),
        "days": len({r.day for r in rows}),
    }


def out(promo: Promotion) -> dict:
    return {
        "id": str(promo.id), "code": promo.code, "name": promo.name,
        "productSku": promo.product_sku, "productName": promo.product_name,
        "startsOn": promo.starts_on.isoformat(), "endsOn": promo.ends_on.isoformat(),
        "kind": promo.kind, "discPercent": str(promo.disc_percent), "discFlat": str(promo.disc_flat),
        "promoPrice": str(promo.promo_price) if promo.promo_price is not None else None,
        "minQty": str(promo.min_qty), "state": promo.state, "remarks": promo.remarks, "rev": promo.rev,
        "createdBy": promo.created_by_name, "approvedBy": promo.approved_by_name,
        "approvedAt": promo.approved_at.isoformat() if promo.approved_at else None,
        "publishedAt": promo.published_at.isoformat() if promo.published_at else None,
        "stoppedBy": promo.stopped_by_name,
        "stoppedAt": promo.stopped_at.isoformat() if promo.stopped_at else None,
    }


async def listing(state: str | None = None) -> list[dict]:
    qs = Promotion.all()
    if state:
        qs = qs.filter(state=state)
    return [out(p) for p in await qs.order_by("-starts_on", "code")]
