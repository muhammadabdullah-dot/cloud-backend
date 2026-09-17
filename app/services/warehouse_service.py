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
MANAGERS = [("warehouse.transfers.manage", "X")]
EXECUTIVE = [("executive.dashboard", "R")]


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


async def list_bins(include_inactive: bool = False) -> list[Bin]:
    qs = Bin.all() if include_inactive else Bin.filter(active=True)
    return await qs.order_by("rack", "level", "position", "bin")


async def _active_bin(bin_id: str) -> Bin:
    found = await Bin.get_or_none(id=bin_id)
    if not found:
        raise WarehouseError("No such bin")
    if not found.active:
        raise WarehouseError(f"{found.label} is switched off. Pick another bin, or switch it back on under Racks & Bins.")
    return found


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
    supplier = await Supplier.get_or_none(id=payload.supplierId)
    if not supplier:
        raise WarehouseError("No such supplier")
    # Switched off anywhere is switched off everywhere (one company list), and the branch already refuses goods from one.
    if not getattr(supplier, "active", True):
        raise WarehouseError(f"{supplier.name} is switched off. Switch it back on under Warehouse, Suppliers to receive from them again.")
    await _active_bin(payload.binId)
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
        approved=payload.approved, received_by=user, at=now, purchase_order_id=payload.purchaseOrderId or None,
    )
    if payload.purchaseOrderId:
        from app.services import purchasing_service

        try:
            await purchasing_service.receive_against(
                payload.purchaseOrderId, payload.supplierId, [(l.productId, l.qty, l.unitPrice) for l in payload.lines], grn.grn_number,
            )
        except purchasing_service.PurchasingError as exc:
            raise WarehouseError(exc.message) from exc
    from app.services import putaway_service

    for line in payload.lines:
        product = await Product.get(id=line.productId)
        # Each line goes to its own bin, else the Item's home bin, else the GRN's bin — and the first bin an Item is
        # ever received into becomes its home.
        line_bin_id = await putaway_service.home_bin_for_receipt(product, line.binId, payload.binId)
        if line_bin_id != payload.binId:
            await _active_bin(line_bin_id)
        if not product.home_bin_id:
            product.home_bin_id = line_bin_id
            await product.save(update_fields=["home_bin_id"])
        await GRNLine.create(
            grn=grn, product_id=line.productId, qty=line.qty, bonus_qty=line.bonusQty,
            unit_price=line.unitPrice, disc_percent=line.discPercent, expiry=line.expiry, tax_rate=line.taxRate,
            bin_id=line_bin_id,
        )
        # Weighted-average cost of godown stock: what was paid for the line (less its discount) spread over
        # every unit that arrived, bonus included. Stock at or below zero has no cost left to average with.
        on_hand = await balance(line.productId)
        incoming = line.qty + line.bonusQty
        paid = line.qty * line.unitPrice * (Decimal("1") - line.discPercent / Decimal("100"))
        if incoming > ZERO:
            if on_hand > ZERO:
                product.avg_cost = (product.avg_cost * on_hand + paid) / (on_hand + incoming)
            else:
                product.avg_cost = paid / incoming
            await product.save(update_fields=["avg_cost"])
        # Bonus units are real stock — they go onto the shelf even though they were not paid for.
        await StockMovement.create(
            product_id=line.productId, bin_id=line_bin_id, kind="receive",
            qty=line.qty + line.bonusQty, reason=grn.grn_number, origin_user=user, at=now,
            unit_cost=(paid / incoming) if incoming > ZERO else None,
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
    """One Item asked for on a branch's behalf. Head office's Requisitions screen records whole requests (several
    Items, a reason, who sends it) through requisition_service.record_for_branch; this older one-Item form stays for
    anything still calling it, and makes the same request with one line."""
    from app.models.requisition_detail import RequisitionDetail, RequisitionLine

    if not await Branch.exists(id=branch_id):
        raise WarehouseError("No such branch")
    if not await Product.exists(id=product_id):
        raise WarehouseError("No such product")
    if qty <= ZERO:
        raise WarehouseError("Requested quantity must be above zero")
    seq = await next_value("requisition", 32)
    req = await Requisition.create(
        requisition_number=f"REQ-{seq:04d}", branch_id=branch_id, product_id=product_id,
        qty_requested=qty, status="pending", requested_at=datetime.now(timezone.utc),
    )
    await RequisitionDetail.create(requisition=req, origin="head-office", received_at=req.requested_at)
    await RequisitionLine.create(requisition=req, product_id=product_id, qty_requested=qty, sort_order=0)
    return req


async def approve_requisition(user: User, requisition_id: str) -> tuple[Requisition, Transfer]:
    """The requisition to transfer handoff: approving creates the Transfer, nothing re-typed. Everything asked for is
    approved as asked; the Requisitions screen approves with changed quantities (requisition_service.approve).

    The Transfer keeps a foreign key back to the requisition, so "why does this transfer exist"
    has an answer in the data rather than only in the sequence of timestamps.
    """
    from app.services import requisition_service

    try:
        return await requisition_service.approve(user, requisition_id)
    except requisition_service.RequisitionError as exc:
        raise WarehouseError(exc.message) from exc


@atomic()
async def create_transfer(user: User, branch_id: str, lines: list[tuple[str, Decimal]], notes: str | None) -> Transfer:
    """Head office decides to send stock to a branch without waiting for it to ask. Starts approved;
    dispatching it is the next step, exactly as for a transfer that came from a requisition."""
    branch = await Branch.get_or_none(id=branch_id)
    if not branch:
        raise WarehouseError("No such branch")
    if not lines:
        raise WarehouseError("A transfer needs at least one line")
    seen: set[str] = set()
    for product_id, qty in lines:
        if product_id in seen:
            raise WarehouseError("An Item is on the transfer twice. Put the whole quantity on one line.")
        seen.add(product_id)
        if qty <= ZERO:
            raise WarehouseError("Every line needs a quantity above zero")
        if not await Product.exists(id=product_id):
            raise WarehouseError(f"Unknown product {product_id}")
    now = datetime.now(timezone.utc)
    seq = await next_value("transfer", 45)
    # A branch with its own server says whether it can take the stock before anything is picked. One without a
    # server is handled here at head office, so there is nobody to ask.
    asks = branch.verified_at is not None
    transfer = await Transfer.create(
        transfer_number=f"TR-{seq:04d}", branch=branch, status="approved", requested_at=now, approved_at=now,
        dispute_open=False, notes=(notes or "").strip() or None,
        ack_status="awaiting" if asks else "skipped", ack_requested_at=now if asks else None,
        ack_note=None if asks else f"{branch.name} has no branch server; head office receives for it",
    )
    for product_id, qty in lines:
        await TransferLine.create(transfer=transfer, product_id=product_id, qty_sent=qty, qty_received=None)
    from app.services import alerts_service, transfer_sync_service
    await transfer_sync_service.publish(transfer)
    if asks:
        await alerts_service.notify(
            "transfer.created", f"{transfer.transfer_number}: {len(lines)} Item{'' if len(lines) == 1 else 's'} for {branch.name}",
            body=f"Created by {user.name}. Waiting for {branch.name} to agree before it's picked.", link="/warehouse/transfers",
            audience_any=EXECUTIVE, subject=("transfer", str(transfer.id)),
        )
    await transfer.fetch_related("lines")
    return transfer


async def reject_requisition(user: User, requisition_id: str, reason: str | None = None) -> Requisition:
    """Declining without writing a reason (the Decisions inbox) still tells the branch it was declined, and by whom;
    the Requisitions screen asks for the reason."""
    from app.services import requisition_service

    try:
        return await requisition_service.decline(user, requisition_id, reason or f"Declined by {user.name} at head office")
    except requisition_service.RequisitionError as exc:
        raise WarehouseError(exc.message) from exc


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
    if transfer.source_branch_id:
        raise WarehouseError("This transfer is between two branches, so the sending branch dispatches it.")
    if transfer.status != "approved":
        raise WarehouseError(f"Only an approved transfer can be dispatched. This one is {transfer.status}")
    if transfer.ack_status in ("awaiting", "declined"):
        await transfer.fetch_related("branch")
        if transfer.ack_status == "declined":
            raise WarehouseError(f"{transfer.branch.name} declined {transfer.transfer_number}: {transfer.ack_note or 'no reason given'}. Ask again or cancel it.")
        raise WarehouseError(
            f"{transfer.branch.name} hasn't agreed to receive {transfer.transfer_number} yet. It's dispatched once they have. "
            "Or, if they stay offline, send it without their answer with a written reason."
        )
    if not payload.vehicle.strip() or not payload.driver.strip():
        raise WarehouseError("A dispatch needs both a vehicle and a driver")

    from app.services import putaway_service

    now = datetime.now(timezone.utc)
    # Picked from the home bin first, then the other bins by priority, split across bins where one isn't enough.
    # Stock that isn't in the godown at all stops the dispatch rather than sending a bin below zero.
    plans = []
    for line in transfer.lines:
        plan, short = await putaway_service.pick_plan(str(line.product_id), line.qty_sent)
        if short > ZERO:
            product = await Product.get(id=line.product_id)
            have = line.qty_sent - short
            raise WarehouseError(f"The godown holds {have.normalize():f} of {product.name}; this transfer needs {line.qty_sent.normalize():f}. Receive or count the stock first.")
        plans.append((line, plan))
    for line, plan in plans:
        product = await Product.get(id=line.product_id)
        line.unit_cost = product.avg_cost
        await line.save(update_fields=["unit_cost"])
        for bin_id, qty in plan:
            await StockMovement.create(
                product_id=line.product_id, bin_id=bin_id, kind="dispatch",
                qty=-qty, reason=transfer.transfer_number, origin_user=user, at=now, unit_cost=product.avg_cost,
            )
    transfer.status = "dispatched"
    transfer.vehicle = payload.vehicle.strip()
    transfer.driver = payload.driver.strip()
    transfer.dispatched_at = now
    await transfer.save()
    # The branch sees it on its way at its next pull, and receives it from there.
    from app.services import transfer_sync_service
    await transfer_sync_service.publish(transfer)
    return transfer


@atomic()
async def receive_transfer(transfer_id: str, payload: ReceiveTransferRequest) -> Transfer:
    """The receiving half — what a branch confirms. Short receipts open a dispute with the sent
    quantity frozen, rather than overwriting it with whatever turned up."""
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines", "branch")
    if not transfer:
        raise WarehouseError("Transfer not found")
    if transfer.branch.verified_at:
        # A branch with its own server counts what arrived itself; recording it here would say stock
        # arrived that never went onto that branch's shelves.
        raise WarehouseError(f"{transfer.branch.name} receives its transfers in the Branch App. Its receipt comes back here by sync.")
    if transfer.status not in ("dispatched", "in_transit"):
        raise WarehouseError(f"Only a dispatched transfer can be received. This one is {transfer.status}")

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
        transfer.dispute_note = payload.note or "Short receipt: quantities received are below what was dispatched."
    await transfer.save()
    from app.services import alerts_service
    await alerts_service.notify(
        "transfer.received", f"{transfer.transfer_number} received for {transfer.branch.name}" + (" (short)" if short else ""),
        body=transfer.dispute_note if short else None, link="/warehouse/transfers", audience_any=MANAGERS + EXECUTIVE,
        subject=("transfer", str(transfer.id)), tone="bad" if short else "good",
    )
    return transfer


async def _open_transfer(transfer_id: str) -> Transfer:
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines", "branch", "source_branch")
    if not transfer:
        raise WarehouseError("Transfer not found")
    if transfer.status not in ("approved", "requested"):
        raise WarehouseError(f"{transfer.transfer_number} has already left. It's {transfer.status.replace('_', ' ')}.")
    return transfer


@atomic()
async def send_without_answer(user: User, transfer_id: str, reason: str | None) -> Transfer:
    """The branch has been offline for a long time: clear the transfer to go without its answer. Needs a written
    reason; the Executive and the branch are both told."""
    from app.services import alerts_service, transfer_sync_service

    transfer = await _open_transfer(transfer_id)
    if transfer.ack_status != "awaiting":
        raise WarehouseError(f"{transfer.transfer_number} isn't waiting for {transfer.branch.name}'s answer.")
    if not alerts_service.branch_offline(transfer.branch):
        hours = int(alerts_service.OFFLINE_AFTER.total_seconds() // 3600)
        raise WarehouseError(
            f"{transfer.branch.name} is online. It checked in within the last {hours} hours. Wait for their answer, or call them."
        )
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise WarehouseError("Write why it can't wait for the branch, in at least a sentence.")
    now = datetime.now(timezone.utc)
    transfer.ack_status, transfer.override_reason, transfer.override_by_name, transfer.override_at = "overridden", reason[:255], user.name, now
    if transfer.status == "requested":
        transfer.status = "approved"
    await transfer.save()
    await transfer_sync_service.publish(transfer)
    await alerts_service.notify(
        "transfer.sent_without_answer", f"{transfer.transfer_number} cleared to go without {transfer.branch.name}'s answer",
        body=f"{user.name}: “{reason}”", link="/warehouse/transfers", audience_any=EXECUTIVE + MANAGERS,
        subject=("transfer", str(transfer.id)), tone="warning",
    )
    return transfer


@atomic()
async def ask_again(user: User, transfer_id: str) -> Transfer:
    from app.services import transfer_sync_service

    transfer = await _open_transfer(transfer_id)
    if transfer.ack_status != "declined":
        raise WarehouseError(f"{transfer.branch.name} hasn't declined {transfer.transfer_number}.")
    transfer.ack_status, transfer.ack_requested_at = "awaiting", datetime.now(timezone.utc)
    transfer.ack_note = f"Declined earlier: {transfer.ack_note}"[:255] if transfer.ack_note else None
    await transfer.save()
    await transfer_sync_service.publish(transfer)
    return transfer


@atomic()
async def cancel_transfer(user: User, transfer_id: str, reason: str | None) -> Transfer:
    from app.services import alerts_service, transfer_sync_service

    transfer = await _open_transfer(transfer_id)
    transfer.status, transfer.cancelled_at = "cancelled", datetime.now(timezone.utc)
    transfer.notes = ((transfer.notes or "") + (f" (cancelled by {user.name}: {reason.strip()})" if reason and reason.strip() else f" (cancelled by {user.name})"))[:255]
    await transfer.save()
    await transfer_sync_service.publish(transfer)
    await alerts_service.notify(
        "transfer.cancelled", f"{transfer.transfer_number} to {transfer.branch.name} cancelled", body=transfer.notes,
        link="/warehouse/transfers", audience_any=EXECUTIVE, subject=("transfer", str(transfer.id)), tone="warning",
    )
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
    from app.services import transfer_sync_service
    await transfer_sync_service.publish(transfer)
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
    await _active_bin(payload.binId)
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
        product = await Product.get(id=count.product_id)
        held = await balance(str(count.product_id), str(count.bin_id))
        if held + delta < ZERO:
            # Picked or moved since the count was made: the difference would leave the bin below zero.
            raise WarehouseError(
                f"{product.name} in this bin has gone down to {held.normalize():f} since the count was made, so taking off its "
                f"difference of {(-delta).normalize():f} would leave less than nothing. Count the bin again."
            )
        await StockMovement.create(
            product_id=count.product_id, bin_id=count.bin_id, kind="count-correction",
            qty=delta, reason="Cycle count", origin_user=user, at=datetime.now(timezone.utc), unit_cost=product.avg_cost,
        )
    count.status = "approved"
    count.approved_by = user
    await count.save()
    return count
