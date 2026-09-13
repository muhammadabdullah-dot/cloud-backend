"""The godown domain. Stock on hand is never stored — it is always folded from StockMovement.

Every action that changes stock does so by appending to that ledger inside one transaction, so a
balance and the rows explaining it can never disagree.
"""
from datetime import datetime, timezone
from decimal import Decimal

from tortoise.functions import Sum
from tortoise.transactions import atomic

from app.models import (
    GRN,
    Batch,
    Bin,
    Branch,
    CycleCount,
    GRNLine,
    Product,
    Requisition,
    StockMovement,
    Supplier,
    Transfer,
    TransferLine,
    User,
    next_value,
)
from app.schemas.warehouse import (
    CountSubmitRequest,
    DispatchRequest,
    GRNCreateRequest,
    ReceiveTransferRequest,
)

ZERO = Decimal("0")


class WarehouseError(Exception):
    def __init__(self, message: str):
        self.message = message


# ── balances ────────────────────────────────────────────────────────────────
async def balance(product_id: str, bin_id: str | None = None) -> Decimal:
    qs = StockMovement.filter(product_id=product_id)
    if bin_id:
        qs = qs.filter(bin_id=bin_id)
    row = await qs.annotate(total=Sum("qty")).values("total")
    return (row[0]["total"] if row and row[0]["total"] is not None else ZERO) or ZERO


async def all_balances() -> list[dict]:
    """Every non-zero (product, bin) balance in one grouped query rather than a fold per pair —
    what Stock Overview and the dashboard need, and the difference between one query and tens of
    thousands once the catalog is real."""
    rows = (
        await StockMovement.all()
        .annotate(total=Sum("qty"))
        .group_by("product_id", "bin_id")
        .values("product_id", "bin_id", "total")
    )
    return [r for r in rows if (r["total"] or ZERO) != ZERO]


# ── masters ─────────────────────────────────────────────────────────────────
async def list_products(q: str | None, limit: int, offset: int) -> tuple[list[Product], int]:
    qs = Product.all()
    term = (q or "").strip()
    if term:
        from tortoise.expressions import Q
        qs = qs.filter(Q(name__icontains=term) | Q(sku__icontains=term))
    total = await qs.count()
    return await qs.order_by("name").offset(offset).limit(limit), total


async def list_suppliers() -> list[Supplier]:
    return await Supplier.all().order_by("name")


async def list_bins() -> list[Bin]:
    return await Bin.all().order_by("rack", "bin")


# ── ledger ──────────────────────────────────────────────────────────────────
async def list_movements(
    product_id: str | None, bin_id: str | None, limit: int, offset: int
) -> tuple[list[StockMovement], int]:
    qs = StockMovement.all()
    if product_id:
        qs = qs.filter(product_id=product_id)
    if bin_id:
        qs = qs.filter(bin_id=bin_id)
    total = await qs.count()
    return await qs.order_by("-at").offset(offset).limit(limit), total


async def list_batches(product_id: str | None, limit: int, offset: int) -> tuple[list[Batch], int]:
    qs = Batch.all()
    if product_id:
        qs = qs.filter(product_id=product_id)
    total = await qs.count()
    return await qs.order_by("expiry").offset(offset).limit(limit), total


# ── receiving ───────────────────────────────────────────────────────────────
@atomic()
async def receive_grn(user: User, payload: GRNCreateRequest) -> GRN:
    if not payload.lines:
        raise WarehouseError("A GRN needs at least one line")
    if not await Supplier.exists(id=payload.supplierId):
        raise WarehouseError("No such supplier")
    if not await Bin.exists(id=payload.binId):
        raise WarehouseError("No such bin")
    if payload.gstMode not in ("normal", "normal-bonus"):
        raise WarehouseError("GST mode must be 'normal' or 'normal-bonus'")

    product_ids = {l.productId for l in payload.lines}
    found = set(await Product.filter(id__in=list(product_ids)).values_list("id", flat=True))
    missing = product_ids - found
    if missing:
        raise WarehouseError(f"No such product: {', '.join(sorted(missing))}")
    for line in payload.lines:
        if line.qty <= ZERO:
            raise WarehouseError("Every line needs a quantity above zero")
        if line.bonusQty < ZERO:
            raise WarehouseError("Bonus quantity can't be negative")

    seq = await next_value("warehouse_grn", 12)
    now = datetime.now(timezone.utc)
    grn = await GRN.create(
        grn_number=f"WGRN-{seq:04d}", supplier_id=payload.supplierId, party_inv_no=payload.partyInvNo,
        bin_id=payload.binId, gst_mode=payload.gstMode, advance_tax=payload.advanceTax,
        approved=payload.approved, received_by=user, at=now,
    )
    for line in payload.lines:
        await GRNLine.create(
            grn=grn, product_id=line.productId, qty=line.qty, bonus_qty=line.bonusQty,
            unit_price=line.unitPrice, disc_percent=line.discPercent, expiry=line.expiry, tax_rate=line.taxRate,
        )
        # Bonus units are real stock — they go onto the shelf even though they were not paid for.
        await StockMovement.create(
            product_id=line.productId, bin_id=payload.binId, kind="receive",
            qty=line.qty + line.bonusQty, reason=grn.grn_number, origin_user=user, at=now,
        )
        if line.expiry:
            await Batch.create(
                product_id=line.productId, lot_number=None, expiry=line.expiry,
                received_qty=line.qty + line.bonusQty,
            )
    await grn.fetch_related("lines")
    return grn


async def list_grns(limit: int, offset: int) -> tuple[list[GRN], int]:
    qs = GRN.all()
    total = await qs.count()
    items = await qs.order_by("-at").offset(offset).limit(limit).prefetch_related("lines")
    return items, total


# ── requisitions ────────────────────────────────────────────────────────────
async def list_requisitions(status: str | None, limit: int, offset: int) -> tuple[list[Requisition], int]:
    qs = Requisition.all()
    if status:
        qs = qs.filter(status=status)
    total = await qs.count()
    return await qs.order_by("-requested_at").offset(offset).limit(limit), total


@atomic()
async def create_requisition(branch_id: str, product_id: str, qty: Decimal) -> Requisition:
    """A branch asking for stock. No UI reaches this yet on either side — branch-app has no
    requisition-authoring screen (frontend-baseline.md §4 point 2 flags it as a frontend gap) —
    but the endpoint is what a branch will call, and it lets the Cloud screens be tested with
    real data rather than only seeded rows."""
    if not await Branch.exists(id=branch_id):
        raise WarehouseError("No such branch")
    if not await Product.exists(id=product_id):
        raise WarehouseError("No such product")
    if qty <= ZERO:
        raise WarehouseError("Requested quantity must be above zero")
    seq = await next_value("requisition", 32)
    return await Requisition.create(
        requisition_number=f"REQ-{seq:04d}", branch_id=branch_id, product_id=product_id,
        qty_requested=qty, status="pending", requested_at=datetime.now(timezone.utc),
    )


@atomic()
async def approve_requisition(user: User, requisition_id: str) -> tuple[Requisition, Transfer]:
    """The requisition → transfer handoff: approving creates the Transfer, nothing re-typed.

    The Transfer keeps a foreign key back to the requisition, so "why does this transfer exist"
    has an answer in the data rather than only in the sequence of timestamps.
    """
    req = await Requisition.get_or_none(id=requisition_id)
    if not req:
        raise WarehouseError("Requisition not found")
    if req.status != "pending":
        raise WarehouseError(f"This requisition is already {req.status}")

    now = datetime.now(timezone.utc)
    req.status = "approved"
    req.decided_by = user
    req.decided_at = now
    await req.save()

    seq = await next_value("transfer", 45)
    transfer = await Transfer.create(
        transfer_number=f"TR-{seq:04d}", branch_id=req.branch_id, requisition=req,
        status="approved", requested_at=req.requested_at, approved_at=now, dispute_open=False,
    )
    await TransferLine.create(
        transfer=transfer, product_id=req.product_id, qty_sent=req.qty_requested, qty_received=None,
    )
    await transfer.fetch_related("lines")
    return req, transfer


@atomic()
async def reject_requisition(user: User, requisition_id: str) -> Requisition:
    req = await Requisition.get_or_none(id=requisition_id)
    if not req:
        raise WarehouseError("Requisition not found")
    if req.status != "pending":
        raise WarehouseError(f"This requisition is already {req.status}")
    req.status = "rejected"
    req.decided_by = user
    req.decided_at = datetime.now(timezone.utc)
    await req.save()
    return req


# ── transfers ───────────────────────────────────────────────────────────────
async def list_transfers(status: str | None, limit: int, offset: int) -> tuple[list[Transfer], int]:
    qs = Transfer.all()
    if status:
        qs = qs.filter(status=status)
    total = await qs.count()
    items = await qs.order_by("-requested_at").offset(offset).limit(limit).prefetch_related("lines")
    return items, total


async def _pick_bin_for(product_id: str, qty: Decimal) -> str:
    """Which bin the stock actually leaves from, by the bin's own dispatch priority, taking the
    first that can cover the line.

    The frontend hardcoded `loc-4` for every dispatch regardless of where the stock sat, which
    made the ledger say things that were not true about a real godown. Falls back to the highest-
    priority bin holding any of the item so a short pick still posts somewhere real.
    """
    rows = await all_balances()
    holding = {r["bin_id"]: Decimal(str(r["total"])) for r in rows if r["product_id"] == product_id}
    if not holding:
        raise WarehouseError("No stock of this item in any bin")
    bins = {b.id: b for b in await Bin.filter(id__in=list(holding))}
    ordered = sorted(holding.items(), key=lambda kv: (bins[kv[0]].priority if kv[0] in bins else 99, kv[0]))
    for bin_id, held in ordered:
        if held >= qty:
            return bin_id
    return ordered[0][0]


@atomic()
async def dispatch_transfer(user: User, transfer_id: str, payload: DispatchRequest) -> Transfer:
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines")
    if not transfer:
        raise WarehouseError("Transfer not found")
    if transfer.status != "approved":
        raise WarehouseError(f"Only an approved transfer can be dispatched — this one is {transfer.status}")
    if not payload.vehicle.strip() or not payload.driver.strip():
        raise WarehouseError("A dispatch needs both a vehicle and a driver")

    now = datetime.now(timezone.utc)
    for line in transfer.lines:
        bin_id = await _pick_bin_for(line.product_id, line.qty_sent)
        await StockMovement.create(
            product_id=line.product_id, bin_id=bin_id, kind="dispatch",
            qty=-line.qty_sent, reason=transfer.transfer_number, origin_user=user, at=now,
        )
    transfer.status = "dispatched"
    transfer.vehicle = payload.vehicle.strip()
    transfer.driver = payload.driver.strip()
    transfer.dispatched_at = now
    await transfer.save()
    return transfer


@atomic()
async def receive_transfer(transfer_id: str, payload: ReceiveTransferRequest) -> Transfer:
    """The receiving half — what a branch confirms. Short receipts open a dispute with the sent
    quantity frozen, rather than overwriting it with whatever turned up."""
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines")
    if not transfer:
        raise WarehouseError("Transfer not found")
    if transfer.status not in ("dispatched", "in_transit"):
        raise WarehouseError(f"Only a dispatched transfer can be received — this one is {transfer.status}")

    received = {l.productId: l.qtyReceived for l in payload.lines}
    short = False
    for line in transfer.lines:
        qty = received.get(line.product_id)
        if qty is None:
            raise WarehouseError(f"No received quantity given for product {line.product_id}")
        if qty < ZERO or qty > line.qty_sent:
            raise WarehouseError("Received quantity must be between zero and the quantity sent")
        line.qty_received = qty
        await line.save()
        if qty < line.qty_sent:
            short = True

    now = datetime.now(timezone.utc)
    transfer.received_at = now
    transfer.status = "received_short" if short else "received"
    if short:
        transfer.dispute_open = True
        transfer.dispute_note = payload.note or "Short receipt — quantities received are below what was dispatched."
    await transfer.save()
    return transfer


@atomic()
async def resolve_dispute(transfer_id: str, note: str | None) -> Transfer:
    """Closing a short-receipt dispute. The status stays `received_short` — the history of what
    happened is not rewritten; only the "still needs attention" flag clears."""
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines")
    if not transfer:
        raise WarehouseError("Transfer not found")
    if not transfer.dispute_open:
        raise WarehouseError("This transfer has no open dispute")
    transfer.dispute_open = False
    if note:
        transfer.dispute_note = f"{transfer.dispute_note or ''} · Resolved: {note}".strip(" ·")
    await transfer.save()
    return transfer


# ── cycle counts ────────────────────────────────────────────────────────────
async def list_counts(limit: int, offset: int) -> tuple[list[CycleCount], int]:
    qs = CycleCount.all()
    total = await qs.count()
    return await qs.order_by("-at").offset(offset).limit(limit), total


@atomic()
async def submit_count(user: User, payload: CountSubmitRequest) -> CycleCount:
    if not await Product.exists(id=payload.productId):
        raise WarehouseError("No such product")
    if not await Bin.exists(id=payload.binId):
        raise WarehouseError("No such bin")
    if payload.countedQty < ZERO:
        raise WarehouseError("A counted quantity can't be negative")
    system_qty = await balance(payload.productId, payload.binId)
    return await CycleCount.create(
        product_id=payload.productId, bin_id=payload.binId, system_qty=system_qty,
        counted_qty=payload.countedQty, status="pending", counted_by=user, at=datetime.now(timezone.utc),
    )


@atomic()
async def approve_count(user: User, count_id: str) -> CycleCount:
    count = await CycleCount.get_or_none(id=count_id)
    if not count:
        raise WarehouseError("Count not found")
    if count.status != "pending":
        raise WarehouseError("This count has already been approved")
    delta = count.counted_qty - count.system_qty
    if delta != ZERO:
        # Post only the difference, not the counted quantity — the ledger records the correction,
        # not a restatement of the whole balance.
        await StockMovement.create(
            product_id=count.product_id, bin_id=count.bin_id, kind="count-correction",
            qty=delta, reason="Cycle count", origin_user=user, at=datetime.now(timezone.utc),
        )
    count.status = "approved"
    count.approved_by = user
    await count.save()
    return count
