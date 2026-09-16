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

# What each starting role may approve on its own when no limit is set on the person. None = no limit.
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
        raise PurchasingError(f"{po.po_number} can't be changed now — it's {po.status.replace('_', ' ')}.")
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
            "po.approved", f"{po.po_number} to {po.supplier.name} approved — {rs(po.total)}",
            body=f"Within {user.name}'s own approval limit.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE, subject=subject, tone="good",
        )
    else:
        po.status, po.auto_approved = "pending_approval", False
        await po.save()
        await alerts_service.notify(
            "po.submitted", f"{po.po_number} to {po.supplier.name} waits for approval — {rs(po.total)}",
            body=f"Raised by {user.name}.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE + APPROVERS, subject=subject,
        )
    return await get_order(order_id)


@atomic()
async def approve_order(user: User, order_id: str) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status != "pending_approval":
        raise PurchasingError(f"{po.po_number} isn't waiting for approval.")
    if po.raised_by_id == user.id:
        raise PurchasingError("Nobody approves their own order — it's above your limit, so someone else decides.", 403)
    if not covers(user, po.total):
        raise PurchasingError(
            f"Your purchase approval limit is {rs(po_limit_of(user))} — {po.po_number} is {rs(po.total)}. Someone with a higher limit approves it.", 403,
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
        raise PurchasingError(f"{po.po_number} can't be cancelled — it's {po.status.replace('_', ' ')}. Close it instead.")
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
        raise PurchasingError(f"{po.po_number} is from {po.supplier.name} — pick that supplier on the GRN.")
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
        + (" — doesn't match the order" if mismatches else ""),
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
