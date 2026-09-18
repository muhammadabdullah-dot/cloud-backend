"""Transfers between head office and branches, both ways.

Down: whenever a transfer changes here (created, answered, dispatched, cancelled, dispute resolved), the
receiving branch gets `transfer.inbound` with the transfer as it now stands, and — for a branch-to-branch
transfer — the sending branch gets `transfer.outbound`. Lines travel by Item code (sku), because each
branch has its own Item ids.

Up, step by step (a branch's stock request comes the same way: `requisition.sent` and `requisition.cancelled`, handled
by requisition_service):
  requested     a branch asks to send stock to another branch (created here, relayed to the other branch)
  acknowledged  the receiving branch agrees to take it — only then is it picked or dispatched
  declined      the receiving branch says no, and why
  cancelled     the sending branch drops a branch-to-branch transfer that hasn't left
  dispatched    the sending branch's stock left
  received      the receiving branch counted it in — accepted, or with a dispute
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.models import Branch, Product, Transfer, TransferLine
from app.services import alerts_service, downstream_service

ZERO = Decimal("0")
MANAGERS = [("warehouse.transfers.manage", "X")]
EXECUTIVE = [("executive.dashboard", "R")]
LINK = "/warehouse/transfers"


def _dt(value) -> datetime | None:
    """A branch sends times as ISO text; the records here hold real datetimes."""
    if isinstance(value, datetime) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _iso(value) -> str | None:
    value = _dt(value)
    return value.isoformat() if value else None


class TransferSyncError(Exception):
    def __init__(self, message: str):
        self.message = message


async def _state(transfer: Transfer) -> dict:
    await transfer.fetch_related("lines__product", "branch", "source_branch")
    source = transfer.source_branch
    return {
        "id": str(transfer.id),
        "number": transfer.transfer_number,
        "status": transfer.status,
        "source": {"kind": "branch", "code": source.code, "name": source.name} if source else {"kind": "godown", "code": "HO", "name": "Central Godown"},
        "destination": {"code": transfer.branch.code, "name": transfer.branch.name},
        "vehicle": transfer.vehicle,
        "driver": transfer.driver,
        "notes": transfer.notes,
        "requestedAt": _iso(transfer.requested_at),
        "dispatchedAt": _iso(transfer.dispatched_at),
        "receivedAt": _iso(transfer.received_at),
        "receivedBy": transfer.received_by_name,
        "disputeOpen": transfer.dispute_open,
        "disputeNote": transfer.dispute_note,
        "ackStatus": transfer.ack_status,
        "ackRequestedAt": _iso(transfer.ack_requested_at),
        "ackAt": _iso(transfer.ack_at),
        "ackBy": transfer.ack_by_name,
        "ackNote": transfer.ack_note,
        "overrideReason": transfer.override_reason,
        "overrideBy": transfer.override_by_name,
        "overrideAt": _iso(transfer.override_at),
        "lines": [
            {
                "sku": line.product.sku, "name": line.product.name, "unit": line.product.unit,
                "price": str(line.product.price), "taxRate": str(line.product.tax_rate), "isWeighed": line.product.is_weighed,
                "qtySent": str(line.qty_sent), "qtyReceived": None if line.qty_received is None else str(line.qty_received),
                "unitCost": None if line.unit_cost is None else str(line.unit_cost),
            }
            for line in transfer.lines
        ],
    }


def _unit_cost(line: dict):
    value = line.get("unitCost")
    return Decimal(str(value)) if value not in (None, "") else None


async def publish(transfer: Transfer, reinstated: bool = False) -> None:
    """Send the transfer as it now stands to the branch receiving it, and to the branch that sent it.

    `reinstated` says head office took back its own cancel because the sending branch had already dispatched it; only
    then may the receiving branch put a shipment it has as cancelled back on its way."""
    state = await _state(transfer)
    if reinstated:
        state["reinstatedByHeadOffice"] = True
    destination = transfer.branch
    if destination.verified_at:
        await downstream_service.enqueue(str(destination.id), "transfer.inbound", {"transfer": state})
    if transfer.source_branch_id and transfer.source_branch and transfer.source_branch.verified_at:
        await downstream_service.enqueue(str(transfer.source_branch_id), "transfer.outbound", {"transfer": state})


async def _product_for(line: dict) -> Product:
    """The Cloud's own Item for a line reported by a branch, created from the line when head office
    doesn't have that code yet — a branch-to-branch transfer can carry anything a branch sells."""
    sku = str(line.get("sku") or "").strip()
    if not sku:
        raise TransferSyncError("A transfer line has no Item code.")
    product = await Product.get_or_none(sku=sku)
    if product:
        return product
    product_id = sku if not await Product.exists(id=sku) else f"br-{sku}"
    product = await Product.create(
        id=product_id[:60], sku=sku, name=line.get("name") or sku, price=Decimal(str(line.get("price") or "0")),
        tax_rate=Decimal(str(line.get("taxRate") or "0")), unit=line.get("unit") or "pc", is_weighed=bool(line.get("isWeighed")),
    )
    from app.services import price_history_service

    await price_history_service.save_rows([price_history_service.first_price(product, "transfer-new")])
    return product


async def project(branch: Branch, payload: dict) -> str:
    event = payload.get("event")
    if str(event or "").startswith("requisition."):
        # A branch's stock request travels with its transfer events. See requisition_service.
        from app.services import requisition_service

        return await requisition_service.apply_from_branch(branch, payload)
    if event == "requested":
        return await _branch_requested(branch, payload.get("transfer") or {})
    if event in ("acknowledged", "declined"):
        return await _branch_answered(branch, payload, event)
    if event == "cancelled":
        return await _branch_cancelled(branch, payload)
    if event == "dispatched":
        return await _branch_dispatched(branch, payload.get("transfer") or {})
    if event == "received":
        return await _branch_received(branch, payload)
    if event == "dispute_opened":
        return await _branch_dispute(branch, payload)
    if event in ("held", "released"):
        return await _branch_hold(branch, payload, event == "held")
    return "ignored"


async def _create_from_branch(branch: Branch, data: dict, status: str) -> Transfer:
    destination = await Branch.get_or_none(code=str(data.get("destinationCode") or "").upper())
    if not destination:
        raise TransferSyncError(f"No branch with code {data.get('destinationCode')}.")
    if destination.id == branch.id:
        raise TransferSyncError("A branch can't send a transfer to itself.")
    at = _dt(data.get("requestedAt")) or _dt(data.get("dispatchedAt")) or datetime.now(timezone.utc)
    transfer = await Transfer.create(
        id=str(data["id"]), transfer_number=str(data.get("number") or str(data["id"])[:8])[:30], branch=destination,
        source_branch=branch, status=status, vehicle=data.get("vehicle"), driver=data.get("driver"), notes=data.get("notes"),
        requested_at=at, approved_at=at, dispute_open=False,
        ack_status="awaiting" if status == "requested" else None, ack_requested_at=at if status == "requested" else None,
    )
    for line in data.get("lines") or []:
        product = await _product_for(line)
        await TransferLine.create(transfer=transfer, product=product, qty_sent=Decimal(str(line.get("qtySent") or "0")), unit_cost=_unit_cost(line))
    return transfer


async def _branch_requested(branch: Branch, data: dict) -> str:
    """A branch asks to send stock to another branch. Nothing has left; the other branch is asked first."""
    if not data.get("id"):
        raise TransferSyncError("A requested transfer needs its id.")
    if await Transfer.exists(id=str(data["id"])):
        return "duplicate"
    transfer = await _create_from_branch(branch, data, "requested")
    await transfer.fetch_related("branch")
    await publish(transfer)
    await alerts_service.notify(
        "transfer.requested", f"{branch.name} wants to send {transfer.transfer_number} to {transfer.branch.name}",
        body=f"Asked by {data.get('requestedBy') or branch.name}. Waiting for {transfer.branch.name} to agree.", link=LINK,
        audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)),
    )
    return "requested"


async def _branch_answered(branch: Branch, payload: dict, event: str) -> str:
    """The receiving branch agreed to take a transfer, or declined it."""
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or "")).prefetch_related("branch", "source_branch")
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    if str(transfer.branch_id) != str(branch.id):
        raise TransferSyncError("Only the branch a transfer is going to can answer for it.")
    answered_at = _aware(_dt(payload.get("at"))) or datetime.now(timezone.utc)
    if transfer.ack_status == "awaiting" and transfer.ack_requested_at and answered_at < _aware(transfer.ack_requested_at):
        return "older-than-question"  # an answer to a question head office has asked again since
    note, by = (payload.get("note") or None), payload.get("by")
    if transfer.status in ("cancelled",):
        return "cancelled"
    late = transfer.status not in ("approved", "requested")
    if late:
        # Sent without waiting (the branch was offline) and it has left already: keep the answer on record.
        transfer.ack_at, transfer.ack_by_name, transfer.ack_note = answered_at, by, note
        await transfer.save(update_fields=["ack_at", "ack_by_name", "ack_note"])
        if event == "declined":
            await alerts_service.notify(
                "transfer.declined", f"{branch.name} declined {transfer.transfer_number} after it was sent", body=note,
                link=LINK, audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)), tone="bad",
            )
        return "answer-after-dispatch"
    transfer.ack_status = "acknowledged" if event == "acknowledged" else "declined"
    transfer.ack_at, transfer.ack_by_name, transfer.ack_note = answered_at, by, note
    if event == "acknowledged" and transfer.status == "requested":
        transfer.status = "approved"
    await transfer.save()
    await publish(transfer)
    sender = transfer.source_branch.name if transfer.source_branch_id else "the godown"
    if event == "acknowledged":
        await alerts_service.notify(
            "transfer.agreed", f"{branch.name} agreed to {transfer.transfer_number}",
            body=f"{by or branch.name}{f': “{note}”' if note else ''}. {'Pick and dispatch it.' if not transfer.source_branch_id else f'{sender} dispatches it.'}",
            link=LINK, audience_any=MANAGERS + [("warehouse.transfers.dispatch", "X")], subject=("transfer", str(transfer.id)), tone="good",
        )
    else:
        await alerts_service.notify(
            "transfer.declined", f"{branch.name} declined {transfer.transfer_number}", body=f"{by or branch.name}: “{note or 'no reason given'}”",
            link=LINK, audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)), tone="bad",
        )
    return transfer.ack_status


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _branch_cancelled(branch: Branch, payload: dict) -> str:
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or "")).prefetch_related("branch")
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    if str(transfer.source_branch_id) != str(branch.id):
        raise TransferSyncError("Only the branch sending a transfer can cancel it.")
    if transfer.status not in ("requested", "approved"):
        return "already-left"
    transfer.status, transfer.cancelled_at = "cancelled", datetime.now(timezone.utc)
    reason = payload.get("reason")
    # Worded the same as the branch writes it on its own copy of the transfer.
    earlier = (transfer.notes or "").rstrip(". ")
    cancelled = f"Cancelled: {reason.strip()}" if reason and reason.strip() else "Cancelled"
    transfer.notes = (f"{earlier}. {cancelled}" if earlier else cancelled)[:255]
    await transfer.save()
    await publish(transfer)
    return "cancelled"


async def _branch_dispatched(branch: Branch, data: dict) -> str:
    transfer_id = str(data.get("id") or "")
    if not transfer_id:
        raise TransferSyncError("A dispatched transfer needs its id.")
    existing = await Transfer.get_or_none(id=transfer_id)
    if existing:
        if existing.status in ("dispatched", "in_transit", "received", "received_short"):
            return "duplicate"
        if str(existing.source_branch_id) != str(branch.id):
            raise TransferSyncError("Only the branch sending a transfer can dispatch it.")
        # Head office cancelled it while the sending branch was already dispatching it. The stock has left that branch,
        # so the cancel came too late: put the shipment back on its way instead of leaving the stock nowhere.
        too_late_cancel = existing.status == "cancelled"
        existing.status = "dispatched"
        if too_late_cancel:
            existing.cancelled_at = None
            earlier = (existing.notes or "").rstrip(". ")
            undone = f"Cancel undone: {branch.name} had already dispatched it"
            existing.notes = (f"{earlier}. {undone}" if earlier else undone)[:255]
        # The sending branch's cost when the stock actually left.
        costs = {str(l.get("sku")): _unit_cost(l) for l in data.get("lines") or []}
        for line in await TransferLine.filter(transfer=existing).prefetch_related("product"):
            if costs.get(line.product.sku) is not None:
                line.unit_cost = costs[line.product.sku]
                await line.save(update_fields=["unit_cost"])
        existing.vehicle, existing.driver = data.get("vehicle") or existing.vehicle, data.get("driver") or existing.driver
        existing.dispatched_at = _dt(data.get("dispatchedAt")) or datetime.now(timezone.utc)
        await existing.save()
        await publish(existing, reinstated=too_late_cancel)
        if too_late_cancel:
            await existing.fetch_related("branch")
            await alerts_service.notify(
                "transfer.cancel_too_late", f"{existing.transfer_number} had already left {branch.name}, so the cancel was undone",
                body=f"{branch.name} dispatched it before the cancel reached them. It is on its way to {existing.branch.name} after all.",
                link=LINK, audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(existing.id)), tone="bad",
            )
            return "dispatched-after-cancel"
        return "dispatched"
    destination = await Branch.get_or_none(code=str(data.get("destinationCode") or "").upper())
    if not destination:
        raise TransferSyncError(f"No branch with code {data.get('destinationCode')}.")
    if destination.id == branch.id:
        raise TransferSyncError("A branch can't send a transfer to itself.")
    now = datetime.now(timezone.utc)
    dispatched_at = _dt(data.get("dispatchedAt")) or now
    transfer = await Transfer.create(
        id=transfer_id, transfer_number=str(data.get("number") or transfer_id[:8])[:30], branch=destination,
        source_branch=branch, status="dispatched", vehicle=data.get("vehicle"), driver=data.get("driver"),
        notes=data.get("notes"), requested_at=dispatched_at, approved_at=dispatched_at, dispatched_at=dispatched_at,
        dispute_open=False,
    )
    for line in data.get("lines") or []:
        product = await _product_for(line)
        await TransferLine.create(transfer=transfer, product=product, qty_sent=Decimal(str(line.get("qtySent") or "0")), unit_cost=_unit_cost(line))
    await publish(transfer)
    return "created"


async def _branch_received(branch: Branch, payload: dict) -> str:
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or "")).prefetch_related("lines__product")
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    if str(transfer.branch_id) != str(branch.id):
        raise TransferSyncError("Only the branch a transfer was sent to can receive it.")
    if transfer.status in ("received", "received_short"):
        return "duplicate"
    received = {str(l.get("sku")): Decimal(str(l.get("qtyReceived") or "0")) for l in payload.get("lines") or []}
    short = False
    for line in transfer.lines:
        qty = received.get(line.product.sku, ZERO)
        line.qty_received = qty
        await line.save(update_fields=["qty_received"])
        if qty < line.qty_sent:
            short = True
    transfer.status = "received_short" if short else "received"
    transfer.received_at = _dt(payload.get("receivedAt")) or datetime.now(timezone.utc)
    transfer.received_by_name = payload.get("receivedBy")
    disputed = short or bool(payload.get("dispute"))
    if disputed:
        transfer.dispute_open = True
        transfer.dispute_note = payload.get("note") or "Short receipt: the branch received less than was sent."
    await transfer.save()
    await publish(transfer)
    await alerts_service.notify(
        "transfer.received", f"{branch.name} received {transfer.transfer_number}" + (" and opened a dispute" if disputed else " and accepted it"),
        body=transfer.dispute_note if disputed else f"Signed for by {transfer.received_by_name or branch.name}.", link=LINK,
        audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)), tone="bad" if disputed else "good",
    )
    return "received-short" if short else ("received-disputed" if disputed else "received")


async def _branch_hold(branch: Branch, payload: dict, held: bool) -> str:
    """The receiving branch held a shipment back (damaged, wrong goods) or released it again."""
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or ""))
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    if str(transfer.branch_id) != str(branch.id):
        raise TransferSyncError("Only the branch a transfer was sent to can hold it.")
    if held:
        transfer.hold_note = (payload.get("reason") or "")[:255] or "Held"
        transfer.held_at = _dt(payload.get("heldAt")) or datetime.now(timezone.utc)
        transfer.held_by_name = payload.get("heldBy")
    else:
        transfer.hold_note = transfer.held_at = transfer.held_by_name = None
    await transfer.save(update_fields=["hold_note", "held_at", "held_by_name"])
    return "held" if held else "released"


async def _branch_dispute(branch: Branch, payload: dict) -> str:
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or payload.get("aggregateId") or ""))
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    transfer.dispute_open = True
    transfer.dispute_note = payload.get("note") or transfer.dispute_note
    await transfer.save(update_fields=["dispute_open", "dispute_note"])
    await publish(transfer)
    await alerts_service.notify(
        "transfer.dispute", f"{branch.name} opened a dispute on {transfer.transfer_number}", body=transfer.dispute_note,
        link=LINK, audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)), tone="bad",
    )
    return "dispute-opened"
