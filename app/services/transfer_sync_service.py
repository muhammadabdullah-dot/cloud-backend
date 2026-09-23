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

Every line carries its Item described in full, so the receiving branch never has to describe it again: department,
category, brand, pack and pieces, barcodes, GST, the discount lock, the reorder level, and the prices a branch new to
the Item starts from. From the godown that is the godown's Item. A branch sending to another branch describes its own
Items when it asks or dispatches; head office keeps that description on the line (TransferLine.item) and passes it on as
it came. Until the sending branch has reported a shipment head office asked it to send, its lines carry the godown's
copy marked `provisional`.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.core.pk_time import pk_day
from app.models import Branch, Product, ProductAlias, Transfer, TransferLine
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


# ── the Item on a transfer line ─────────────────────────────────────────────────────────────────

# Line key, the godown Item's column, the longest value the column holds.
_TEXT = (
    ("department", "department", 80), ("category", "category", 80), ("itemClass", "item_class", 80), ("subclass", "subclass", 80),
    ("brand", "brand", 120), ("manufacturer", "manufacturer", 120), ("variant", "variant", 60),
)
# What a line from a branch carries besides its Item: kept on the line itself, not in the description passed on.
_LINE_ONLY = ("qtySent", "qtyReceived", "unitCost", "provisional")


def _plain(value) -> str | None:
    """A number as it travels: plain digits, never 1E+1."""
    return None if value is None else format(Decimal(str(value)).normalize(), "f")


async def describe(product: Product) -> dict:
    """The godown's Item as a transfer line carries it to a branch. A branch keeps prices the godown doesn't (wholesale,
    piece, strip, pack and box), so those go blank and the branch's own rules price them."""
    aliases = await ProductAlias.filter(product_id=product.id)
    return {
        "sku": product.sku, "name": product.name, "unit": product.unit, "isWeighed": product.is_weighed,
        "taxRate": _plain(product.tax_rate), "price": _plain(product.price), "rpp": _plain(product.rpp),
        "barcode": product.barcode,
        "aliases": [
            {"code": a.code, "qty": _plain(a.qty), "remarks": a.remarks, "discPercent": _plain(a.disc_percent), "discFlat": _plain(a.disc_flat)}
            for a in aliases
        ],
        **{key: getattr(product, column) for key, column, _ in _TEXT},
        "origin": product.origin, "packUnit": product.pack_unit, "packSize": product.pack_size, "packsPerBox": product.packs_per_box,
        "piecesPerUnit": product.pieces_per_unit, "pieceUnit": product.piece_unit, "piecesPerStrip": product.pieces_per_strip,
        "lockDisc": product.lock_disc, "reorderLevel": _plain(product.reorder_level),
        "needsDetails": product.needs_details, "detailsNote": product.details_note if product.needs_details else None,
    }


def _item_of(line: dict) -> dict | None:
    """The description a branch's line carries, to pass on as it came; None from a branch whose software doesn't
    describe its Items yet (its line has only the code, name, unit, price and GST)."""
    if "aliases" not in line and "department" not in line:
        return None
    return {key: value for key, value in line.items() if key not in _LINE_ONLY}


def _text(line: dict, key: str, size: int) -> str | None:
    return str(line.get(key) or "").strip()[:size] or None


def _number(line: dict, key: str) -> Decimal | None:
    value = line.get(key)
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _count(line: dict, key: str, top: int | None = None) -> int | None:
    number = _number(line, key)
    if number is None or number < 1 or number != number.to_integral_value() or (top and number > top):
        return None
    return int(number)


async def _state(transfer: Transfer) -> dict:
    await transfer.fetch_related("lines__product", "branch", "source_branch")
    source = transfer.source_branch
    lines = []
    for line in transfer.lines:
        item = line.item if isinstance(line.item, dict) and line.item else None
        lines.append({
            **(item or await describe(line.product)), "sku": line.product.sku,
            "qtySent": str(line.qty_sent), "qtyReceived": None if line.qty_received is None else str(line.qty_received),
            "unitCost": None if line.unit_cost is None else str(line.unit_cost),
            # Branch to branch, before the sending branch has described the Item: the godown's copy, to be replaced.
            **({"provisional": True} if source and not item else {}),
        })
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
        "lines": lines,
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
    doesn't have that code yet: a branch-to-branch transfer can carry anything a branch sells.

    A new one takes the whole description the line carries (a branch describes its Items on every transfer it sends).
    One head office already has is left as it is: the godown's Items are head office's to describe."""
    from app.services import items_service, price_history_service

    sku = str(line.get("sku") or "").strip()
    if not sku:
        raise TransferSyncError("A transfer line has no Item code.")
    product = await Product.get_or_none(sku=sku)
    if product:
        return product
    product_id = sku if not await Product.exists(id=sku) else f"br-{sku}"
    origin = str(line.get("origin") or "").strip().lower()
    barcode = str(line.get("barcode") or "").strip()
    if not barcode or len(barcode) > 60 or barcode == sku or await items_service.code_owner(barcode):
        barcode = None
    note = f"Still to complete at the branch it came from: {str(line.get('detailsNote') or 'check its details').strip()}"[:200]
    product = await Product.create(
        id=product_id[:60], sku=sku, name=(str(line.get("name") or "").strip() or sku)[:200], price=_number(line, "price") or Decimal("0"),
        tax_rate=_number(line, "taxRate") or Decimal("0"), unit=_text(line, "unit", 30) or "pc", is_weighed=bool(line.get("isWeighed")),
        rpp=_number(line, "rpp"), barcode=barcode, lock_disc=bool(line.get("lockDisc")),
        **{column: _text(line, key, size) for key, column, size in _TEXT},
        origin=origin if origin in ("local", "imported") else None,
        pack_unit=_text(line, "packUnit", 30), pack_size=_count(line, "packSize"), packs_per_box=_count(line, "packsPerBox"),
        pieces_per_unit=_count(line, "piecesPerUnit", 1000), piece_unit=_text(line, "pieceUnit", 20),
        pieces_per_strip=_count(line, "piecesPerStrip", 1000), reorder_level=_number(line, "reorderLevel"),
        needs_details=bool(line.get("needsDetails")), details_note=note if line.get("needsDetails") else None,
    )
    seen = {sku, barcode}
    for alias in line.get("aliases") or []:
        code = str((alias or {}).get("code") or "").strip()
        qty = _number(alias, "qty")
        if not code or len(code) > 60 or code in seen or not qty or await items_service.code_owner(code):
            continue
        seen.add(code)
        await ProductAlias.create(
            product=product, code=code, remarks=_text(alias, "remarks", 255), qty=qty,
            disc_percent=_number(alias, "discPercent") or Decimal("0"), disc_flat=_number(alias, "discFlat") or Decimal("0"),
        )
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
        await TransferLine.create(
            transfer=transfer, product=product, qty_sent=Decimal(str(line.get("qtySent") or "0")), unit_cost=_unit_cost(line), item=_item_of(line),
        )
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
        # The sending branch's cost when the stock actually left, and its own description of each Item: a shipment head
        # office asked it to send has carried the godown's copy until now.
        sent = {str(l.get("sku")): l for l in data.get("lines") or []}
        for line in await TransferLine.filter(transfer=existing).prefetch_related("product"):
            reported = sent.get(line.product.sku)
            if reported is None:
                continue
            if _unit_cost(reported) is not None:
                line.unit_cost = _unit_cost(reported)
            line.item = _item_of(reported) or line.item
            await line.save(update_fields=["unit_cost", "item"])
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
        await TransferLine.create(
            transfer=transfer, product=product, qty_sent=Decimal(str(line.get("qtySent") or "0")), unit_cost=_unit_cost(line), item=_item_of(line),
        )
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


def add_to_dispute_note(previous: str | None, latest: str, size: int = 255) -> str:
    """A transfer's dispute note is its whole story: what the branch reported, how it was settled, any reopening.
    Something new goes after what is already there, and when the column is full the oldest words give way,
    never the newest."""
    previous, latest = (previous or "").strip(), latest.strip()
    if not previous:
        return latest[:size]
    room = size - len(latest) - 3
    if room < 2:
        return latest[:size]
    if len(previous) > room:
        previous = "…" + previous[len(previous) - room + 1:].lstrip()
    return f"{previous} · {latest}"


async def _branch_dispute(branch: Branch, payload: dict) -> str:
    transfer = await Transfer.get_or_none(id=str(payload.get("transferId") or payload.get("aggregateId") or ""))
    if not transfer:
        raise TransferSyncError("That transfer isn't known at head office.")
    words = (payload.get("note") or "").strip() or "The branch reported a problem with this delivery."
    if (transfer.dispute_note or "").strip():
        # Opened again after an earlier dispute: the earlier report and how it was settled stay on record.
        try:
            day = pk_day(datetime.fromisoformat(payload["at"])) if payload.get("at") else pk_day()
        except ValueError:
            day = pk_day()
        who = payload.get("by") or branch.name
        words = add_to_dispute_note(transfer.dispute_note, f"Reopened by {who} on {day:%d %b %Y}: {words}")
    transfer.dispute_open = True
    transfer.dispute_note = words
    await transfer.save(update_fields=["dispute_open", "dispute_note"])
    await publish(transfer)
    await alerts_service.notify(
        "transfer.dispute", f"{branch.name} opened a dispute on {transfer.transfer_number}", body=transfer.dispute_note,
        link=LINK, audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)), tone="bad",
    )
    return "dispute-opened"
