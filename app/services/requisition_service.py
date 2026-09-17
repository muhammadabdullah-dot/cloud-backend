"""Branch stock requests at head office: arriving, deciding, and telling the branch.

  arriving   a branch sends a request (event `requisition.sent`, carried with its transfer events), or head office
             records one for a branch that phoned it in. It lands as a pending `Requisition` with its lines.
  deciding   approve: head office sets what it will send of each Item (0 leaves one out). A transfer is made at once,
             from the central godown, or from the branch the request asked, which then dispatches it itself. The
             branch asked for it, so it isn't asked to agree again.
             decline: always with a reason the branch reads.
  withdrawn  the branch takes a pending request back (event `requisition.cancelled`).

Every change goes back down to the branch as the request now stands, on the transfer message kind the branch
already reads (branch software handles `kind: requisition` inside it), so its screen shows what became of it.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from tortoise.functions import Sum
from tortoise.transactions import atomic

from app.models import Branch, Product, Requisition, StockMovement, Transfer, TransferLine, User, next_value
from app.models.requisition_detail import RequisitionDetail, RequisitionLine
from app.services import alerts_service, downstream_service

ZERO = Decimal("0")
LINK = "/warehouse/requisitions"
DECIDERS = [("warehouse.requisitions.approve", "X")]
DISPATCHERS = [("warehouse.transfers.dispatch", "X")]
EXECUTIVE = [("executive.dashboard", "R")]


class RequisitionError(Exception):
    def __init__(self, message: str):
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value) -> datetime | None:
    if isinstance(value, datetime) or value is None:
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _qty(value: Decimal | None) -> str | None:
    return None if value is None else format(Decimal(value).normalize(), "f")


def _items(n: int) -> str:
    return f"{n} Item{'' if n == 1 else 's'}"


async def _parts(req: Requisition) -> tuple[RequisitionDetail | None, list[RequisitionLine]]:
    detail = await RequisitionDetail.get_or_none(requisition_id=req.id).prefetch_related("source_branch")
    lines = await RequisitionLine.filter(requisition_id=req.id).order_by("sort_order").prefetch_related("product")
    return detail, lines


# ── telling the branch ───────────────────────────────────────────────────────────────────────────

async def _state(req: Requisition) -> dict:
    await req.fetch_related("branch")
    detail, lines = await _parts(req)
    transfer = await Transfer.filter(requisition_id=req.id).order_by("-requested_at").first()
    source = detail.source_branch if detail else None
    if not lines:
        product = await Product.get(id=req.product_id)
        rows = [{"sku": product.sku, "name": product.name, "unit": product.unit, "price": str(product.price), "taxRate": str(product.tax_rate),
                 "isWeighed": product.is_weighed, "qtyRequested": _qty(req.qty_requested), "qtyApproved": None}]
    else:
        rows = [{
            "sku": l.product.sku, "name": l.product.name, "unit": l.product.unit, "price": str(l.product.price), "taxRate": str(l.product.tax_rate),
            "isWeighed": l.product.is_weighed, "qtyRequested": _qty(l.qty_requested), "qtyApproved": _qty(l.qty_approved),
        } for l in lines]
    return {
        "id": str(req.id), "headOfficeNumber": req.requisition_number, "branchNumber": detail.branch_number if detail else None,
        "status": req.status, "origin": detail.origin if detail else "head-office",
        "sourceCode": source.code if source else None, "sourceName": source.name if source else "Central Godown",
        "reason": detail.reason if detail else None, "neededBy": detail.needed_by.isoformat() if detail and detail.needed_by else None,
        "requestedBy": detail.requested_by_name if detail else None, "requestedAt": _iso(req.requested_at),
        "receivedAt": _iso(detail.received_at) if detail else None, "decidedAt": _iso(req.decided_at),
        "decidedBy": detail.decided_by_name if detail else None, "decisionNote": detail.decision_note if detail else None,
        "transferId": str(transfer.id) if transfer else None, "transferNumber": transfer.transfer_number if transfer else None,
        "lines": rows,
    }


async def publish(req: Requisition) -> None:
    await req.fetch_related("branch")
    if req.branch.verified_at:
        await downstream_service.enqueue(str(req.branch_id), "transfer.outbound", {"transfer": {"kind": "requisition", "requisition": await _state(req)}})


# ── arriving ─────────────────────────────────────────────────────────────────────────────────────

async def _source_for(requesting: Branch, code: str | None) -> Branch | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    source = await Branch.get_or_none(code=code)
    if not source:
        raise RequisitionError(f"No branch with code {code}.")
    if source.id == requesting.id:
        raise RequisitionError("A branch can't ask itself for stock.")
    return source


async def _create(
    branch: Branch, requisition_id: str | None, lines: list[tuple[Product, Decimal, Decimal | None, Decimal | None]], *,
    origin: str, source: Branch | None, reason: str | None, needed_by: date | None, requested_by: str | None,
    branch_number: str | None, requested_at: datetime | None,
) -> Requisition:
    if not lines:
        raise RequisitionError("A request needs at least one Item.")
    seen: set[str] = set()
    for product, qty, _, _ in lines:
        if product.id in seen:
            raise RequisitionError(f"{product.name} is on the request twice.")
        seen.add(product.id)
        if qty is None or qty <= ZERO:
            raise RequisitionError(f"{product.name}: the quantity must be above zero.")
    now = _now()
    seq = await next_value("requisition", 32)
    first_product, first_qty, _, _ = lines[0]
    extra = {"id": requisition_id} if requisition_id else {}
    req = await Requisition.create(
        requisition_number=f"REQ-{seq:04d}", branch=branch, product=first_product, qty_requested=first_qty,
        status="pending", requested_at=requested_at or now, **extra,
    )
    await RequisitionDetail.create(
        requisition=req, source_branch=source, origin=origin, branch_number=branch_number, reason=(reason or "").strip()[:255] or None,
        needed_by=needed_by, requested_by_name=requested_by, received_at=now,
    )
    for order, (product, qty, on_hand, daily) in enumerate(lines):
        await RequisitionLine.create(
            requisition=req, product=product, qty_requested=qty, branch_on_hand=on_hand, branch_daily_sales=daily, sort_order=order,
        )
    sender = source.name if source else "the godown"
    await alerts_service.notify(
        "requisition.received", f"{branch.name} asks for {_items(len(lines))} ({req.requisition_number})",
        body=f"{req.requisition_number}: from {sender}{f', needed by {needed_by:%d %b}' if needed_by else ''}. {(reason or '').strip()}".strip(),
        link=LINK, audience_any=DECIDERS + EXECUTIVE, subject=("requisition", str(req.id)),
    )
    return req


def _dec(value) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


async def apply_from_branch(branch: Branch, payload: dict) -> str:
    from app.services.transfer_sync_service import TransferSyncError, _product_for

    event = payload.get("event")
    try:
        if event == "requisition.sent":
            data = payload.get("requisition") or {}
            if not data.get("id"):
                raise RequisitionError("A stock request needs its id.")
            if await Requisition.exists(id=str(data["id"])):
                return "duplicate"
            lines = []
            for line in data.get("lines") or []:
                product = await _product_for(line)
                lines.append((product, _dec(line.get("qty")) or ZERO, _dec(line.get("onHand")), _dec(line.get("dailySales"))))
            needed = data.get("neededBy")
            req = await _create(
                branch, str(data["id"]), lines, origin="branch", source=await _source_for(branch, data.get("sourceCode")),
                reason=data.get("reason"), needed_by=date.fromisoformat(needed) if needed else None,
                requested_by=data.get("requestedBy") or data.get("sentBy"), branch_number=data.get("number"),
                requested_at=_dt(data.get("sentAt")),
            )
            await publish(req)
            return "requisition-received"
        if event == "requisition.cancelled":
            req = await Requisition.get_or_none(id=str(payload.get("requisitionId") or ""))
            if not req:
                raise RequisitionError("That stock request isn't known at head office.")
            if str(req.branch_id) != str(branch.id):
                raise RequisitionError("Only the branch that asked can withdraw a request.")
            if req.status != "pending":
                await publish(req)  # head office decided first; the branch gets that decision again
                return "already-decided"
            req.status, req.decided_at = "cancelled", _dt(payload.get("at")) or _now()
            await req.save(update_fields=["status", "decided_at"])
            detail = await RequisitionDetail.get_or_none(requisition_id=req.id)
            if detail:
                why = (payload.get("reason") or "").strip()
                detail.decision_note = (f"Withdrawn by {payload.get('by') or branch.name}" + (f": {why}" if why else ""))[:255]
                await detail.save(update_fields=["decision_note"])
            await publish(req)
            return "requisition-withdrawn"
    except RequisitionError as exc:
        raise TransferSyncError(exc.message) from exc
    return "ignored"


@atomic()
async def record_for_branch(
    user: User, branch_id: str, source_branch_id: str | None, reason: str | None, needed_by: date | None,
    lines: list[tuple[str, Decimal]],
) -> Requisition:
    """Head office writes down a request a branch phoned in (usually one without its own server)."""
    branch = await Branch.get_or_none(id=branch_id)
    if not branch or branch.status != "active":
        raise RequisitionError("Pick a branch that's in use.")
    source = await Branch.get_or_none(id=source_branch_id) if source_branch_id else None
    if source_branch_id and not source:
        raise RequisitionError("No such branch to send it.")
    if source and source.id == branch.id:
        raise RequisitionError("A branch can't ask itself for stock.")
    if not (reason or "").strip():
        raise RequisitionError("Say why the branch needs it.")
    rows = []
    for product_id, qty in lines:
        product = await Product.get_or_none(id=product_id)
        if not product:
            raise RequisitionError(f"Unknown Item {product_id}")
        rows.append((product, qty, None, None))
    req = await _create(
        branch, None, rows, origin="head-office", source=source, reason=reason, needed_by=needed_by,
        requested_by=f"{user.name} (head office)", branch_number=None, requested_at=None,
    )
    await publish(req)
    return req


# ── deciding ─────────────────────────────────────────────────────────────────────────────────────

async def _pending(requisition_id: str) -> Requisition:
    req = await Requisition.get_or_none(id=requisition_id).prefetch_related("branch")
    if not req:
        raise RequisitionError("Request not found")
    if req.status != "pending":
        word = {"rejected": "declined", "cancelled": "withdrawn by the branch"}.get(req.status, req.status)
        raise RequisitionError(f"{req.requisition_number} is already {word}.")
    return req


@atomic()
async def approve(user: User, requisition_id: str, quantities: dict[str, Decimal] | None = None, note: str | None = None) -> tuple[Requisition, Transfer]:
    """Approve with the quantities head office will send (left out: as asked; 0: not sent). Makes the transfer now."""
    from app.services import transfer_sync_service

    req = await _pending(requisition_id)
    detail, lines = await _parts(req)
    if not lines:
        # A request from before requests had lines: its one Item becomes its line.
        lines = [await RequisitionLine.create(requisition=req, product_id=req.product_id, qty_requested=req.qty_requested, sort_order=0)]
        await lines[0].fetch_related("product")
    quantities = {str(k): v for k, v in (quantities or {}).items()}
    on_request = {str(l.product_id) for l in lines}
    stray = set(quantities) - on_request
    if stray:
        raise RequisitionError("Only Items on the request can be approved. Ask the branch to request anything else.")
    changes, send = [], []
    for line in lines:
        qty = quantities.get(str(line.product_id), line.qty_requested)
        if qty is None or qty < ZERO:
            raise RequisitionError(f"{line.product.name}: the quantity can't be below zero.")
        line.qty_approved = qty
        await line.save(update_fields=["qty_approved"])
        if qty != line.qty_requested:
            changes.append(f"{line.product.name} {_qty(line.qty_requested)} to {_qty(qty)}" if qty > ZERO else f"{line.product.name} left out")
        if qty > ZERO:
            send.append((line.product_id, qty))
    if not send:
        raise RequisitionError("Approve at least one Item, or decline the request with a reason.")
    source = detail.source_branch if detail else None
    if source:
        if source.status != "active":
            raise RequisitionError(f"{source.name} is switched off, so it can't send stock. Approve it from the godown instead, or decline it.")
        if source.verified_at is None:
            raise RequisitionError(f"{source.name} has no branch server of its own, so nobody there can dispatch it. Decline it and send from the godown.")

    now = _now()
    written = (note or "").strip()
    summary = "; ".join(filter(None, [f"Changed: {', '.join(changes)}" if changes else "", written]))
    req.status, req.decided_by, req.decided_at = "approved", user, now
    await req.save()
    if detail:
        detail.decided_by_name, detail.decision_note = user.name, summary[:255] or None
        await detail.save(update_fields=["decided_by_name", "decision_note"])
    seq = await next_value("transfer", 45)
    transfer = await Transfer.create(
        transfer_number=f"TR-{seq:04d}", branch_id=req.branch_id, source_branch=source, requisition=req,
        status="approved", requested_at=req.requested_at, approved_at=now, dispute_open=False,
        # The branch asked for this stock, so it doesn't have to agree to it again.
        ack_status="skipped", ack_note=f"{req.branch.name} asked for it ({req.requisition_number})",
        notes=f"Requested: {detail.reason}"[:255] if detail and detail.reason else None,
    )
    for product_id, qty in send:
        await TransferLine.create(transfer=transfer, product_id=product_id, qty_sent=qty, qty_received=None)
    await transfer_sync_service.publish(transfer)
    await publish(req)
    if source:
        await alerts_service.notify(
            "requisition.approved", f"{req.requisition_number} approved: {source.name} sends {transfer.transfer_number} to {req.branch.name}",
            body=f"{_items(len(send))}. {source.name} dispatches it from its own stock.{f' {summary}' if summary else ''}",
            link="/warehouse/transfers", audience_any=[("warehouse.transfers.manage", "X")] + EXECUTIVE, subject=("transfer", str(transfer.id)),
        )
    else:
        await alerts_service.notify(
            "requisition.approved", f"{transfer.transfer_number} to pick for {req.branch.name} ({req.requisition_number})",
            body=f"{_items(len(send))} approved by {user.name}.{f' {summary}' if summary else ''}",
            link="/warehouse/transfers", audience_any=DISPATCHERS, subject=("transfer", str(transfer.id)),
        )
    await transfer.fetch_related("lines")
    return req, transfer


@atomic()
async def decline(user: User, requisition_id: str, reason: str | None) -> Requisition:
    why = (reason or "").strip()
    if len(why) < 3:
        raise RequisitionError("Say why it's declined. The branch reads it.")
    req = await _pending(requisition_id)
    req.status, req.decided_by, req.decided_at = "rejected", user, _now()
    await req.save()
    detail = await RequisitionDetail.get_or_none(requisition_id=req.id)
    if detail:
        detail.decided_by_name, detail.decision_note = user.name, why[:255]
        await detail.save(update_fields=["decided_by_name", "decision_note"])
    else:
        await RequisitionDetail.create(requisition=req, origin="head-office", decided_by_name=user.name, decision_note=why[:255])
    await publish(req)
    return req


# ── reading ──────────────────────────────────────────────────────────────────────────────────────

async def _godown_on_hand(product_ids: list[str]) -> dict[str, Decimal]:
    if not product_ids:
        return {}
    rows = await StockMovement.filter(product_id__in=product_ids).annotate(total=Sum("qty")).group_by("product_id").values("product_id", "total")
    return {str(r["product_id"]): Decimal(str(r["total"] or 0)) for r in rows}


async def _branch_on_hand(branch_id: str, skus: list[str]) -> dict[str, Decimal] | None:
    """What a branch last reported holding of these Items; None when it hasn't reported its stock."""
    from app.models import BranchProductStock, BranchSnapshotRun

    run = await BranchSnapshotRun.filter(branch_id=branch_id, status="complete").order_by("-completed_at").first()
    if not run or not skus:
        return None
    out: dict[str, Decimal] = {}
    for row in await BranchProductStock.filter(branch_id=branch_id, snapshot_id=run.snapshot_id, product_sku__in=skus).values("product_sku", "qty"):
        out[row["product_sku"]] = out.get(row["product_sku"], ZERO) + Decimal(str(row["qty"] or 0))
    return out


async def list_detailed(status: str | None, limit: int, offset: int, only_id: str | None = None) -> tuple[list[dict], int]:
    qs = Requisition.all()
    if only_id:
        qs = qs.filter(id=only_id)
    if status:
        qs = qs.filter(status=status)
    total = await qs.count()
    reqs = await qs.order_by("-requested_at").offset(max(offset, 0)).limit(min(max(limit, 1), 500)).prefetch_related("branch", "decided_by", "product")
    ids = [r.id for r in reqs]
    details = {str(d.requisition_id): d for d in await RequisitionDetail.filter(requisition_id__in=ids).prefetch_related("source_branch")} if ids else {}
    lines: dict[str, list[RequisitionLine]] = {}
    for line in (await RequisitionLine.filter(requisition_id__in=ids).order_by("sort_order").prefetch_related("product")) if ids else []:
        lines.setdefault(str(line.requisition_id), []).append(line)
    transfers: dict[str, Transfer] = {}
    for t in (await Transfer.filter(requisition_id__in=ids).order_by("requested_at")) if ids else []:
        transfers[str(t.requisition_id)] = t
    product_ids = list({str(l.product_id) for ls in lines.values() for l in ls} | {str(r.product_id) for r in reqs})
    godown = await _godown_on_hand(product_ids)
    source_stock: dict[str, dict[str, Decimal] | None] = {}
    rows = []
    for r in reqs:
        detail = details.get(str(r.id))
        source = detail.source_branch if detail else None
        req_lines = lines.get(str(r.id)) or []
        items = [(l.product, l.qty_requested, l.qty_approved, l.branch_on_hand, l.branch_daily_sales) for l in req_lines] or [
            (r.product, r.qty_requested, None, None, None)
        ]
        held_there = None
        if source and r.status == "pending":
            key = str(source.id)
            if key not in source_stock:
                all_skus = [p.sku for ls in lines.values() for p in [l.product for l in ls]]
                source_stock[key] = await _branch_on_hand(key, all_skus)
            held_there = source_stock[key]
        transfer = transfers.get(str(r.id))
        rows.append({
            "id": str(r.id), "requisitionNumber": r.requisition_number, "branchId": str(r.branch_id), "branchName": r.branch.name,
            "branchCode": r.branch.code, "branchHasServer": r.branch.verified_at is not None, "status": r.status,
            "origin": detail.origin if detail else "head-office", "branchNumber": detail.branch_number if detail else None,
            "sourceBranchId": str(source.id) if source else None, "sourceBranchName": source.name if source else None,
            "sourceBranchCode": source.code if source else None, "reason": detail.reason if detail else None,
            "neededBy": detail.needed_by if detail else None, "requestedBy": detail.requested_by_name if detail else None,
            "requestedAt": r.requested_at, "decidedAt": r.decided_at,
            "decidedBy": (detail.decided_by_name if detail and detail.decided_by_name else (r.decided_by.name if r.decided_by else None)),
            "decisionNote": detail.decision_note if detail else None,
            "transferId": str(transfer.id) if transfer else None, "transferNumber": transfer.transfer_number if transfer else None,
            "transferStatus": transfer.status if transfer else None,
            "lines": [
                {
                    "productId": str(p.id), "productName": p.name, "productSku": p.sku, "unit": p.unit, "qtyRequested": asked,
                    "qtyApproved": approved, "branchOnHand": on_hand, "branchDailySales": daily,
                    "godownOnHand": godown.get(str(p.id), ZERO),
                    "sourceOnHand": None if held_there is None else held_there.get(p.sku, ZERO),
                }
                for p, asked, approved, on_hand, daily in items
            ],
        })
    return rows, total
