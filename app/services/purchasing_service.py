"""Head office purchase orders, approval limits and low stock. See models/purchasing.py for the lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tortoise.transactions import atomic

from app.models import Product, PurchaseOrder, PurchaseOrderLine, Supplier, User, next_value
from app.services import alerts_service

ZERO = Decimal("0")
OPEN = ("draft", "pending_approval", "approved", "partially_received")
RECEIVABLE = ("approved", "partially_received")

# What each starting role may approve on its own when no limit is set on the person. None = no limit. These are the
# limits until someone saves others in Admin > Company & Settings; call `use_saved_limits()` before reading them.
ROLE_PO_LIMITS: dict[str, Decimal | None] = {"executive": None, "warehouse-manager": Decimal("500000")}

LINK = "/warehouse/purchase-orders"
BUYERS = [("warehouse.purchase-orders", "W")]
APPROVERS = [("warehouse.purchase-orders.approve", "X")]
EXECUTIVE = [("executive.dashboard", "R")]


class PurchasingError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rs(v: Decimal) -> str:
    return f"Rs {v.quantize(Decimal('1')):,}"


def money(v: Decimal | None) -> str | None:
    """Plain rupees with paisa for the wire: 1500.00, never 1.5E+3."""
    return None if v is None else format(Decimal(v).quantize(Decimal("0.01")), "f")


async def use_saved_limits() -> None:
    from app.services import office_settings_service

    await office_settings_service.warm()


def po_limit_of(user: User) -> Decimal | None:
    """The largest order this person may approve. None means no limit."""
    if user.po_limit is not None:
        return user.po_limit
    return ROLE_PO_LIMITS.get(user.role_id, ZERO)


def covers(user: User, total: Decimal) -> bool:
    limit = po_limit_of(user)
    return limit is None or total <= limit


async def list_orders(status: str | None, q: str | None, limit: int, offset: int) -> tuple[list[PurchaseOrder], int]:
    qs = PurchaseOrder.all()
    if status == "open":
        qs = qs.filter(status__in=list(OPEN))
    elif status:
        qs = qs.filter(status=status)
    if q and q.strip():
        from tortoise.expressions import Q

        qs = qs.filter(Q(po_number__icontains=q.strip()) | Q(supplier__name__icontains=q.strip()))
    total = await qs.count()
    rows = await qs.order_by("-raised_at").offset(max(offset, 0)).limit(min(max(limit, 1), 200)).prefetch_related(
        "lines", "supplier", "raised_by", "approved_by",
    )
    return rows, total


async def get_order(order_id: str) -> PurchaseOrder:
    po = await PurchaseOrder.get_or_none(id=order_id).prefetch_related("lines", "supplier", "raised_by", "approved_by")
    if not po:
        raise PurchasingError("That purchase order doesn't exist.", 404)
    return po


async def _set_lines(po: PurchaseOrder, lines: list[tuple[str, Decimal, Decimal]]) -> None:
    if not lines:
        raise PurchasingError("Add at least one Item to order.")
    seen: set[str] = set()
    total = ZERO
    await PurchaseOrderLine.filter(purchase_order=po).delete()
    for position, (product_id, qty, unit_cost) in enumerate(lines):
        if product_id in seen:
            raise PurchasingError("An Item is on the order twice. Put the whole quantity on one line.")
        seen.add(product_id)
        product = await Product.get_or_none(id=product_id)
        if not product:
            raise PurchasingError(f"Unknown Item {product_id}")
        if qty <= ZERO:
            raise PurchasingError(f"{product.name}: order a quantity above zero.")
        if unit_cost < ZERO:
            raise PurchasingError(f"{product.name}: the cost can't be negative.")
        await PurchaseOrderLine.create(purchase_order=po, product=product, qty=qty, unit_cost=unit_cost, position=position)
        total += qty * unit_cost
    po.total = total.quantize(Decimal("0.01"))


@atomic()
async def create_order(user: User, supplier_id: str, lines, expected_at, notes: str | None, reason: str | None) -> PurchaseOrder:
    supplier = await Supplier.get_or_none(id=supplier_id)
    if not supplier:
        raise PurchasingError("Pick a supplier.")
    seq = await next_value("warehouse_po", 1)
    po = await PurchaseOrder.create(
        po_number=f"WPO-{seq:04d}", supplier=supplier, status="draft", expected_at=expected_at,
        notes=(notes or "").strip() or None, reason=(reason or "").strip()[:120] or None, raised_by=user,
    )
    await _set_lines(po, lines)
    await po.save()
    return await get_order(str(po.id))


@atomic()
async def update_order(user: User, order_id: str, supplier_id: str, lines, expected_at, notes: str | None, reason: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status not in ("draft", "rejected"):
        raise PurchasingError(f"{po.po_number} can't be changed now because it's {po.status.replace('_', ' ')}.")
    if po.raised_by_id != user.id:
        raise PurchasingError("Only the person who raised an order changes it.", 403)
    supplier = await Supplier.get_or_none(id=supplier_id)
    if not supplier:
        raise PurchasingError("Pick a supplier.")
    po.supplier = supplier
    po.expected_at = expected_at
    po.notes = (notes or "").strip() or None
    po.reason = (reason or "").strip()[:120] or None
    po.status = "draft"
    await _set_lines(po, lines)
    await po.save()
    return await get_order(order_id)


@atomic()
async def submit_order(user: User, order_id: str) -> PurchaseOrder:
    """Within the raiser's own limit it's approved there and then, and the Executive is told; above it, it waits
    for someone whose limit covers it."""
    from app.services.rbac_service import has_permission

    await use_saved_limits()
    po = await get_order(order_id)
    if po.status not in ("draft", "rejected"):
        raise PurchasingError(f"{po.po_number} was already submitted.")
    if po.raised_by_id != user.id:
        raise PurchasingError("Only the person who raised an order submits it.", 403)
    now = _now()
    po.submitted_at = now
    po.decision_note = None
    subject = ("purchase-order", str(po.id))
    if await has_permission(user, "warehouse.purchase-orders.approve", "X") and covers(user, po.total):
        po.status, po.approved_by, po.approved_at, po.auto_approved = "approved", user, now, True
        await po.save()
        await alerts_service.notify(
            "po.approved", f"{po.po_number} to {po.supplier.name} approved ({rs(po.total)})",
            body=f"Within {user.name}'s own approval limit.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE, subject=subject, tone="good",
        )
    else:
        po.status, po.auto_approved = "pending_approval", False
        await po.save()
        await alerts_service.notify(
            "po.submitted", f"{po.po_number} to {po.supplier.name} waits for approval ({rs(po.total)})",
            body=f"Raised by {user.name}.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE + APPROVERS, subject=subject,
        )
    return await get_order(order_id)


@atomic()
async def approve_order(user: User, order_id: str) -> PurchaseOrder:
    await use_saved_limits()
    po = await get_order(order_id)
    if po.status != "pending_approval":
        raise PurchasingError(f"{po.po_number} isn't waiting for approval.")
    if po.raised_by_id == user.id:
        raise PurchasingError("Nobody approves their own order. It's above your limit, so someone else decides.", 403)
    if not covers(user, po.total):
        raise PurchasingError(
            f"Your purchase approval limit is {rs(po_limit_of(user))}, but {po.po_number} is {rs(po.total)}. Someone with a higher limit approves it.", 403,
        )
    po.status, po.approved_by, po.approved_at = "approved", user, _now()
    await po.save()
    await alerts_service.notify(
        "po.approved", f"{po.po_number} to {po.supplier.name} approved by {user.name}", body=f"{rs(po.total)}. Send it to the supplier.",
        link=LINK, audience_any=BUYERS + EXECUTIVE, users=[str(po.raised_by_id)], subject=("purchase-order", str(po.id)), tone="good",
    )
    return await get_order(order_id)


@atomic()
async def reject_order(user: User, order_id: str, reason: str | None) -> PurchaseOrder:
    if not (reason or "").strip():
        raise PurchasingError("Say why it's rejected, so it can be changed.")
    po = await get_order(order_id)
    if po.status != "pending_approval":
        raise PurchasingError(f"{po.po_number} isn't waiting for approval.")
    if po.raised_by_id == user.id:
        raise PurchasingError("Cancel your own order instead of rejecting it.", 403)
    po.status, po.decision_note = "rejected", reason.strip()[:255]
    await po.save()
    await alerts_service.notify(
        "po.rejected", f"{po.po_number} to {po.supplier.name} rejected by {user.name}", body=po.decision_note,
        link=LINK, users=[str(po.raised_by_id)], audience_any=EXECUTIVE, subject=("purchase-order", str(po.id)), tone="bad",
    )
    return await get_order(order_id)


@atomic()
async def cancel_order(user: User, order_id: str, reason: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status not in ("draft", "pending_approval", "rejected", "approved"):
        raise PurchasingError(f"{po.po_number} can't be cancelled because it's {po.status.replace('_', ' ')}. Close it instead.")
    if any(l.received_qty > ZERO for l in po.lines):
        raise PurchasingError(f"Some of {po.po_number} has been received. Close it instead.")
    was = po.status
    po.status, po.decision_note, po.closed_at = "cancelled", (reason or "").strip()[:255] or None, _now()
    await po.save()
    if was in ("pending_approval", "approved"):
        await alerts_service.notify(
            "po.cancelled", f"{po.po_number} to {po.supplier.name} cancelled by {user.name}", body=po.decision_note,
            link=LINK, audience_any=BUYERS + EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning",
        )
    return await get_order(order_id)


@atomic()
async def close_order(user: User, order_id: str, note: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status != "partially_received":
        raise PurchasingError("Only a partly received order is closed. Cancel one that has nothing received.")
    po.status, po.decision_note, po.closed_at = "closed", (note or "").strip()[:255] or "The rest won't come.", _now()
    await po.save()
    await alerts_service.notify(
        "po.closed", f"{po.po_number} closed short by {user.name}", body=po.decision_note, link=LINK,
        audience_any=EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning",
    )
    return await get_order(order_id)


async def receive_against(po_id: str, supplier_id: str, received: list[tuple[str, Decimal, Decimal]], grn_number: str) -> list[str]:
    """Tick off what arrived on a GRN against its order. Returns what didn't match, in words, for the notice."""
    po = await get_order(po_id)
    if po.status not in RECEIVABLE:
        raise PurchasingError(f"{po.po_number} isn't approved, so nothing can be received against it.")
    if str(po.supplier_id) != str(supplier_id):
        raise PurchasingError(f"{po.po_number} is from {po.supplier.name}, so pick that supplier on the GRN.")
    by_product = {str(l.product_id): l for l in po.lines}
    names = {str(p.id): p.name for p in await Product.filter(id__in=[pid for pid, _, _ in received])}
    mismatches: list[str] = []
    for product_id, qty, unit_price in received:
        line = by_product.get(product_id)
        if line is None:
            raise PurchasingError(f"{names.get(product_id, product_id)} isn't on {po.po_number}. Receive it on a separate GRN.")
        remaining = line.qty - line.received_qty
        if qty > remaining:
            mismatches.append(f"{names.get(product_id)}: {qty.normalize():f} received, {max(remaining, ZERO).normalize():f} was still due")
        if unit_price != line.unit_cost:
            mismatches.append(f"{names.get(product_id)}: charged {rs(unit_price)} a unit, ordered at {rs(line.unit_cost)}")
        line.received_qty = line.received_qty + qty
        await line.save(update_fields=["received_qty"])
    all_in = all(l.received_qty >= l.qty for l in await PurchaseOrderLine.filter(purchase_order=po))
    po.status = "received" if all_in else "partially_received"
    if all_in:
        po.closed_at = _now()
    await po.save()
    await alerts_service.notify(
        "po.received", f"{grn_number} received against {po.po_number} from {po.supplier.name}"
        + (", but it doesn't match the order" if mismatches else ""),
        body="; ".join(mismatches) if mismatches else ("Everything ordered has arrived." if all_in else "Part of the order has arrived."),
        link=LINK, audience_any=BUYERS + EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning" if mismatches else "good",
    )
    return mismatches


async def low_stock() -> list[dict]:
    """Godown Items at or below their reorder level with nothing already on order."""
    from app.services import warehouse_service

    watched = await Product.filter(reorder_level__gt=0, active=True)
    if not watched:
        return []
    on_hand: dict[str, Decimal] = {}
    for row in await warehouse_service.all_balances():
        on_hand[row["product_id"]] = on_hand.get(row["product_id"], ZERO) + Decimal(str(row["total"]))
    on_order: dict[str, Decimal] = {}
    for line in await PurchaseOrderLine.filter(purchase_order__status__in=list(OPEN)):
        on_order[str(line.product_id)] = on_order.get(str(line.product_id), ZERO) + max(line.qty - line.received_qty, ZERO)
    out = []
    for p in watched:
        held = on_hand.get(str(p.id), ZERO)
        if held <= p.reorder_level and on_order.get(str(p.id), ZERO) <= ZERO:
            out.append({"productId": str(p.id), "sku": p.sku, "name": p.name, "onHand": held, "reorderLevel": p.reorder_level, "avgCost": p.avg_cost})
    return sorted(out, key=lambda r: (r["onHand"] - r["reorderLevel"], r["name"]))


# ── suggestions: what to put on an order, by plain rules ─────────────────────────────────────────
#
# Quantity to order = what the branches sell in the cover days (from their last 30 days of sales), or the Item's reorder
# level if that is higher, less what the godown holds and what is already on open orders, rounded up to whole packs.
# Choosing a supplier suggests the Items bought from them before (earlier orders, GRNs and the Item's supplier list);
# without a supplier, the Items running low.

SALES_WINDOW_DAYS = 30
SHORT_DAYS = 7
MAX_SUGGESTIONS = 300


def _num(v: Decimal) -> str:
    """A quantity in words: 12, 1.5, never 12.000."""
    v = Decimal(v)
    if v == v.to_integral_value():
        return f"{int(v):,}"
    return f"{v.quantize(Decimal('0.1')).normalize():f}"


def rate_words(rate: Decimal, who: str = "sells") -> str:
    """How fast an Item goes, the way a person would say it."""
    if rate <= 0:
        return f"no sales in the last {SALES_WINDOW_DAYS} days"
    if rate >= 1:
        return f"{who} {_num(rate if rate >= 10 else rate.quantize(Decimal('0.1')))} a day"
    week = rate * 7
    if week >= 1:
        return f"{who} about {_num(week.quantize(Decimal('0.1')) if week < 10 else week.quantize(Decimal('1')))} a week"
    return f"{who} about {_num(max(Decimal('1'), (rate * 30).quantize(Decimal('1'))))} a month"


def round_up(need: Decimal, pack_size: int | None, weighed: bool = False) -> tuple[Decimal, str | None]:
    """Round what is needed up to what can be bought: whole packs when the pack size is known, whole units otherwise
    (half a unit for a weighed Item). Gives the quantity and, when packs changed it, how."""
    import math

    if need <= 0:
        return ZERO, None
    if pack_size and pack_size > 1:
        packs = math.ceil(need / Decimal(pack_size))
        qty = Decimal(packs * pack_size)
        return qty, (f"rounded up to {packs} full pack{'s' if packs != 1 else ''} of {pack_size}" if qty != need else None)
    if weighed:
        return Decimal(math.ceil(need * 2)) / 2, None
    return Decimal(math.ceil(need)), None


def suggest_line(rate: Decimal, held: Decimal, on_order: Decimal, reorder_level: Decimal | None, cover_days: int,
                 pack_size: int | None = None, weighed: bool = False, who: str = "sells", holder: str = "holds") -> dict:
    """The suggested quantity for one Item and the reasoning in plain words."""
    by_sales = rate * cover_days
    level = reorder_level if reorder_level and reorder_level > 0 else ZERO
    target = max(by_sales, level)
    need = target - held - on_order
    qty, packed = round_up(need, pack_size, weighed)
    parts = [rate_words(rate, who), f"{holder} {_num(held)}"]
    if on_order > 0:
        parts.append(f"{_num(on_order)} already on order")
    if rate > 0:
        parts.append(f"{cover_days} days of cover needs {_num(by_sales.quantize(Decimal('1')) if by_sales >= 10 else by_sales.quantize(Decimal('0.1')))}")
    if level > by_sales:
        parts.append(f"keeps at least its reorder level of {_num(level)}")
    if qty > 0:
        parts.append(f"so order {_num(qty)}" + (f" ({packed})" if packed else ""))
    elif target <= 0:
        parts.append("so nothing is suggested")
    else:
        parts.append("enough for now")
    return {"rate": rate, "target": target, "qty": qty, "reason": ", ".join(parts)}


async def _branch_sales_rates(skus: list[str]) -> dict[str, Decimal]:
    """Units a day the branches sell of each code, together: each branch's sales over the last 30 days it reported,
    divided by the days it reported."""
    from datetime import timedelta

    from tortoise import Tortoise

    from app.core.pk_time import today_pk

    if not skus:
        return {}
    since = str(today_pk() - timedelta(days=SALES_WINDOW_DAYS))
    conn = Tortoise.get_connection("default")
    days = {str(r["branch_id"]): int(r["days"] or 0) for r in await conn.execute_query_dict(
        "SELECT branch_id, COUNT(DISTINCT day) AS days FROM branch_daily_stats WHERE day > ? GROUP BY branch_id", [since])}
    rates: dict[str, Decimal] = {}
    for i in range(0, len(skus), 500):
        chunk = skus[i:i + 500]
        for r in await conn.execute_query_dict(
            f"SELECT branch_id, product_sku, SUM(CAST(qty AS REAL)) AS qty FROM branch_product_stats "
            f"WHERE day > ? AND product_sku IN ({','.join('?' * len(chunk))}) GROUP BY branch_id, product_sku", [since, *chunk],
        ):
            d = days.get(str(r["branch_id"]), 0)
            if d > 0 and (r["qty"] or 0) > 0:
                rates[r["product_sku"]] = rates.get(r["product_sku"], ZERO) + Decimal(str(r["qty"])) / d
    return rates


async def _dispatch_rates(product_ids: list[str]) -> dict[str, Decimal]:
    """Units a day the godown sends out to branches of each Item: its last 30 days of dispatches over those days (fewer
    for a godown that started keeping stock more recently)."""
    from datetime import timedelta

    from tortoise import Tortoise

    from app.core.pk_time import day_start, today_pk

    if not product_ids:
        return {}
    conn = Tortoise.get_connection("default")
    first = await conn.execute_query_dict("SELECT MIN(at) AS first FROM warehouse_stock_movements", [])
    if not first or not first[0]["first"]:
        return {}
    try:
        from app.core.pk_time import pk_day

        began = pk_day(datetime.fromisoformat(str(first[0]["first"]).replace("Z", "+00:00")))
    except ValueError:
        began = today_pk() - timedelta(days=SALES_WINDOW_DAYS)
    start = max(today_pk() - timedelta(days=SALES_WINDOW_DAYS - 1), began)
    days = max((today_pk() - start).days + 1, 1)
    since = day_start(start).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, Decimal] = {}
    for i in range(0, len(product_ids), 500):
        chunk = product_ids[i:i + 500]
        for r in await conn.execute_query_dict(
            "SELECT product_id, -SUM(CAST(qty AS REAL)) AS sent FROM warehouse_stock_movements "
            f"WHERE kind = 'dispatch' AND at >= ? AND product_id IN ({','.join('?' * len(chunk))}) GROUP BY product_id", [since, *chunk],
        ):
            if (r["sent"] or 0) > 0:
                out[str(r["product_id"])] = Decimal(str(r["sent"])) / days
    return out


async def _branches_hold(skus: list[str]) -> dict[str, Decimal]:
    from app.services import items_service

    out: dict[str, Decimal] = {}
    lists = await items_service.latest_branch_lists()
    for i in range(0, len(skus), 500):
        for r in await items_service.branch_rows(lists, skus=skus[i:i + 500], limit=50000):
            out[r["product_sku"]] = out.get(r["product_sku"], ZERO) + max(Decimal(str(r["qty"] or 0)), ZERO)
    return out


async def _on_order(product_ids: list[str], exclude_order_id: str | None) -> dict[str, Decimal]:
    qs = PurchaseOrderLine.filter(purchase_order__status__in=list(OPEN), product_id__in=product_ids)
    if exclude_order_id:
        qs = qs.exclude(purchase_order_id=exclude_order_id)
    out: dict[str, Decimal] = {}
    for pid, qty, got in await qs.values_list("product_id", "qty", "received_qty"):
        out[str(pid)] = out.get(str(pid), ZERO) + max(Decimal(str(qty)) - Decimal(str(got)), ZERO)
    return out


async def suggestions(supplier_id: str | None, cover_days: int = 30, exclude_order_id: str | None = None) -> dict:
    """With a supplier: the Items bought from them before, each with a suggested quantity and why. Without one: the
    godown Items running low (at or below their reorder level, or lasting under a week at the branches' rate)."""
    from tortoise.functions import Sum

    from app.models import GRNLine, ProductSupplier, StockMovement

    cover_days = min(max(int(cover_days or 30), 1), 365)
    supplier = None
    last_cost: dict[str, tuple[datetime, Decimal]] = {}
    source: dict[str, str] = {}
    if supplier_id:
        supplier = await Supplier.get_or_none(id=supplier_id)
        if not supplier:
            raise PurchasingError("That supplier doesn't exist.", 404)
        for pid, cost, at in await GRNLine.filter(grn__supplier_id=supplier_id).values_list("product_id", "unit_price", "grn__at"):
            source[str(pid)] = "received from them"
            if at and (str(pid) not in last_cost or at > last_cost[str(pid)][0]):
                last_cost[str(pid)] = (at, Decimal(str(cost)))
        ordered_at: dict[str, tuple[datetime, Decimal]] = {}
        for pid, cost, at in await PurchaseOrderLine.filter(purchase_order__supplier_id=supplier_id).exclude(
            purchase_order__status="cancelled",
        ).values_list("product_id", "unit_cost", "purchase_order__raised_at"):
            source.setdefault(str(pid), "ordered from them")
            if at and (str(pid) not in ordered_at or at > ordered_at[str(pid)][0]):
                ordered_at[str(pid)] = (at, Decimal(str(cost)))
        # What was last paid them on a GRN, or else the price last agreed on an order.
        for pid, seen in ordered_at.items():
            last_cost.setdefault(pid, seen)
        for pid in await ProductSupplier.filter(supplier_id=supplier_id).values_list("product_id", flat=True):
            source.setdefault(str(pid), "on the Item's supplier list")
        products = await Product.filter(id__in=list(source), active=True) if source else []
    else:
        products = await Product.filter(active=True)
    ids = [p.id for p in products]
    held: dict[str, Decimal] = {}
    for i in range(0, len(ids), 500):
        for row in await StockMovement.filter(product_id__in=ids[i:i + 500]).annotate(total=Sum("qty")).group_by("product_id").values("product_id", "total"):
            held[str(row["product_id"])] = Decimal(str(row["total"] or 0))
    on_order = await _on_order(ids, exclude_order_id) if ids else {}
    rates = await _branch_sales_rates([p.sku for p in products])
    sent = await _dispatch_rates(ids)
    branches = await _branches_hold([p.sku for p in products]) if products else {}
    if not supplier_id and ids:
        # Without a supplier, the cost is the last price paid anyone.
        for pid, cost, at in await GRNLine.filter(product_id__in=ids).values_list("product_id", "unit_price", "grn__at"):
            if at and (str(pid) not in last_cost or at > last_cost[str(pid)][0]):
                last_cost[str(pid)] = (at, Decimal(str(cost)))

    lines = []
    for p in products:
        h, o = held.get(p.id, ZERO), on_order.get(p.id, ZERO)
        # The faster of the two: what the branches sell of it, or what the godown sends out to them.
        r, who = rates.get(p.sku, ZERO), "branches sell"
        if sent.get(p.id, ZERO) > r:
            r, who = sent[p.id], "the godown sends branches"
        s = suggest_line(r, h, o, p.reorder_level, cover_days, p.pack_size, p.is_weighed, who=who, holder="godown holds")
        if not supplier_id:
            low = (p.reorder_level is not None and p.reorder_level > 0 and h + o <= p.reorder_level) or (r > 0 and h + o < r * SHORT_DAYS)
            if not low or s["qty"] <= 0:
                continue
        cost = last_cost.get(p.id, (None, p.avg_cost))[1]
        lines.append({
            "productId": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "packSize": p.pack_size,
            "ratePerDay": s["rate"], "held": h, "onOrder": o, "reorderLevel": p.reorder_level, "branchesHold": branches.get(p.sku, ZERO),
            "suggestedQty": s["qty"], "unitCost": cost or ZERO, "reason": s["reason"], "why": source.get(p.id, "running low"),
            "needsDetails": p.needs_details,
        })
    lines.sort(key=lambda l: (l["suggestedQty"] <= 0, -(l["ratePerDay"] or ZERO), l["name"]))
    return {
        "supplierId": supplier_id, "supplierName": supplier.name if supplier else None, "coverDays": cover_days,
        "salesDays": SALES_WINDOW_DAYS, "count": len(lines), "lines": lines[:MAX_SUGGESTIONS],
        "rule": (f"Suggested quantity is what the branches sell in {cover_days} days (from their last {SALES_WINDOW_DAYS} days of sales, "
                 "or what the godown sent them if that is more), "
                 "or the Item's reorder level if that is higher, less what the godown holds and what is already on open orders, "
                 "rounded up to whole packs where the pack size is known."),
    }
