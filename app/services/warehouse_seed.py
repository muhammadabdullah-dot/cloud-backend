"""Warehouse master data and opening ledger.

Numbers are taken from frontend-baseline.md §3.4, which documents exactly what cloud-app's
localStorage store has always shown: 7 receive + 2 dispatch movements, 2 batches, 2 pending
requisitions (REQ-0031, REQ-0030), 2 transfers (TR-0044 in transit, TR-0038 short-received with
an open dispute). Reproducing them exactly means the screens keep rendering the same figures as
they swap from localStorage to the server, so any difference after the swap is a real bug rather
than a data change.

Rack A / Bin 02 is deliberately seeded over its stated capacity (2,400 units against 2,000) —
the Racks & Bins capacity bar needs a genuine over-capacity case to show its red state.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import (
    Batch,
    Bin,
    Branch,
    Counter,
    Product,
    Requisition,
    StockMovement,
    Supplier,
    Transfer,
    TransferLine,
)

PRODUCTS = [
    ("p-1", "166247", "Coca Cola Bottle 1.5L", "150", "17", False, "bottle", "carton", 12),
    ("p-2", "166301", "Eggs (Dozen)", "320", "0", False, "dozen", "tray", 30),
    ("p-3", "100261", "Provas Duo Tablet 30s", "300", "0", False, "box", "case", 10),
    ("p-4", "200555", "Bananas (Loose)", "180", "0", True, "kg", None, None),
    ("p-5", "150210", "Lay's Chips 40g", "60", "17", False, "pack", "box", 24),
    ("p-6", "150900", "Nestle Milk Pack 1L", "260", "0", False, "pack", "crate", 12),
    ("p-7", "180044", "Panadol Extra 10s", "45", "0", False, "strip", "box", 10),
    ("p-8", "210087", "Tomatoes (Loose)", "90", "0", True, "kg", None, None),
    ("p-9", "190033", "Lifebuoy Soap 100g", "75", "17", False, "pc", "carton", 48),
    ("p-10", "220019", "Basmati Rice 1kg", "320", "0", True, "kg", None, None),
]

SUPPLIERS = [
    ("sup-1", "SUP786", "786 Traders (FINO)", "Fino Representative", "061-2116302"),
    ("sup-2", "SUPGRO", "Grocers United Distribution", "Waqas Ahmed", "0300-9988776"),
]

BINS = [
    ("loc-4", "A", "01", 1, 2000),
    ("loc-5", "A", "02", 2, 2000),
    ("loc-6", "B", "01", 1, 1500),
    ("loc-7", "B", "02", 3, 1500),
    ("loc-8", "C", "01", 1, 1000),
]

# (product_id, bin_id, kind, qty, days_ago)
OPENING_MOVEMENTS = [
    ("p-1", "loc-4", "receive", "960", 10),
    ("p-2", "loc-4", "receive", "360", 9),
    ("p-3", "loc-5", "receive", "480", 12),
    ("p-5", "loc-5", "receive", "1920", 8),
    ("p-6", "loc-6", "receive", "240", 6),
    ("p-7", "loc-6", "receive", "400", 11),
    ("p-9", "loc-7", "receive", "576", 14),
    # Already dispatched to branches, netted against the receipts above.
    ("p-1", "loc-4", "dispatch", "-120", 1),
    ("p-3", "loc-5", "dispatch", "-100", 4),
]

# (product_id, lot_number, days_until_expiry, received_qty)
BATCHES = [
    ("p-3", "PV-2408", 20, "480"),
    ("p-6", "NM-0912", 5, "240"),
]

# (requisition_number, branch_code, product_id, qty, days_ago)
REQUISITIONS = [
    ("REQ-0031", "FC", "p-1", "60", 0),
    ("REQ-0030", "FC", "p-5", "240", 1),
]

# The sequence each generator resumes from — matching the frontend's own `nextSeq`.
GRN_SEQ_SEED = 12
TRANSFER_SEQ_SEED = 45
REQUISITION_SEQ_SEED = 32


async def seed_warehouse_if_empty() -> None:
    """Guarded on the ledger rather than on Role.exists(), so it still runs on a database that was
    seeded before the warehouse tables existed."""
    if await StockMovement.exists():
        return

    now = datetime.now(timezone.utc)

    for id_, sku, name, price, tax, weighed, unit, pack_unit, pack_size in PRODUCTS:
        await Product.get_or_create(
            id=id_,
            defaults=dict(
                sku=sku, name=name, price=Decimal(price), tax_rate=Decimal(tax), is_weighed=weighed,
                unit=unit, pack_unit=pack_unit, pack_size=pack_size,
            ),
        )
    for id_, code, name, contact, phone in SUPPLIERS:
        await Supplier.get_or_create(id=id_, defaults=dict(code=code, name=name, contact_person=contact, phone=phone))
    for id_, rack, bin_, priority, capacity in BINS:
        await Bin.get_or_create(id=id_, defaults=dict(rack=rack, bin=bin_, priority=priority, capacity_units=capacity))

    for product_id, bin_id, kind, qty, days_ago in OPENING_MOVEMENTS:
        await StockMovement.create(
            product_id=product_id, bin_id=bin_id, kind=kind, qty=Decimal(qty),
            origin_user=None, at=now - timedelta(days=days_ago),
        )

    for product_id, lot, days_until_expiry, qty in BATCHES:
        await Batch.create(
            product_id=product_id, lot_number=lot,
            expiry=now + timedelta(days=days_until_expiry), received_qty=Decimal(qty),
        )

    branches = {b.code: b for b in await Branch.all()}
    for number, branch_code, product_id, qty, days_ago in REQUISITIONS:
        branch = branches.get(branch_code)
        if not branch:  # a database whose branches were renamed or removed — skip rather than crash
            continue
        await Requisition.create(
            requisition_number=number, branch=branch, product_id=product_id,
            qty_requested=Decimal(qty), status="pending", requested_at=now - timedelta(days=days_ago),
        )

    # These used to ship to a "Head Office" branch, which was the godown sending stock to
    # itself. Head office IS the godown; transfers go out to branches. Targeted at a real
    # branch instead, and skipped entirely if none is registered.
    destination = next((branches[c] for c in ("FC",) if c in branches), None) or next(iter(branches.values()), None)
    if destination:
        t1 = await Transfer.create(
            transfer_number="TR-0044", branch=destination, status="in_transit",
            vehicle="LEB-4471", driver="Nadeem",
            requested_at=now - timedelta(days=1), approved_at=now - timedelta(days=1), dispatched_at=now,
            dispute_open=False,
        )
        await TransferLine.create(transfer=t1, product_id="p-1", qty_sent=Decimal("120"), qty_received=None)

        t2 = await Transfer.create(
            transfer_number="TR-0038", branch=destination, status="received_short",
            vehicle="LEB-2290", driver="Kashif",
            requested_at=now - timedelta(days=4), approved_at=now - timedelta(days=4),
            dispatched_at=now - timedelta(days=3), received_at=now - timedelta(days=2),
            dispute_open=True, dispute_note="Branch received 96 of 100. 4 boxes missing.",
        )
        await TransferLine.create(transfer=t2, product_id="p-3", qty_sent=Decimal("100"), qty_received=Decimal("96"))

    # Prime the sequences so the first generated document continues the frontend's numbering
    # rather than restarting at 1. A Counter holding V means `next_value` hands out V next, so the
    # seed goes in as-is.
    for name, seed in (
        ("warehouse_grn", GRN_SEQ_SEED),
        ("transfer", TRANSFER_SEQ_SEED),
        ("requisition", REQUISITION_SEQ_SEED),
    ):
        await Counter.get_or_create(id=name, defaults={"value": seed})
