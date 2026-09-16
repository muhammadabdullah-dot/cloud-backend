"""Taking a branch's own picture of itself and making it the Cloud's view.

This is the piece that turns the Executive dashboard from something a script populates into
something the branches populate. `scripts/import_branch_snapshot.py` used to do this by hand, once,
by opening a branch database file; the same aggregation now arrives over the authenticated sync
channel every two hours. Both paths land here, in one implementation — two would drift, and the
day they drifted the dashboard would disagree with itself depending on how the data got in.

**Why aggregates and not documents.** The branch's outbox carries "a sale happened, invoice
HO-2026-000143, Rs 100,000" — a notification, by design (contracts.md §3.5). It carries no line
items, no cashier, no tender split. Rebuilding per-product-per-day figures from that stream is
impossible, and enriching every outbox writer to carry full documents would mean replicating the
branch's entire schema into the Cloud before a single number appeared on screen. The branch already
computes these aggregates correctly — its own Reports screen is built on them — so it ships what it
knows. The event stream stays what it is: the record of what happened, and the thing that will
carry documents when a use case actually needs documents.

**Replace, never merge.** A snapshot is a fresh picture of the same branch, so the previous rows go.
Merging would double-count every day a branch later corrected — a voided sale would leave its
original figure sitting underneath the correction forever.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tortoise.transactions import in_transaction

from app.core import logs
from app.models import (
    Branch,
    BranchCashierStat,
    BranchCreditCustomer,
    BranchDailyStat,
    BranchDiscountOverride,
    BranchHourlyStat,
    BranchProductCashierStat,
    BranchProductStat,
    BranchProductStock,
    BranchReturn,
    BranchSnapshotRun,
    BranchStockAlert,
    BranchTenderStat,
    BranchStaffDuty,
    BranchTillClose,
)

D0 = Decimal("0")

# The aggregate tables a snapshot owns outright. Listed once so "replace everything this snapshot
# is responsible for" can't quietly fall out of step with what a snapshot actually carries.
AGGREGATE_MODELS = (
    BranchDailyStat, BranchCashierStat, BranchProductStat, BranchProductCashierStat, BranchStockAlert,
    BranchHourlyStat, BranchTillClose, BranchTenderStat, BranchDiscountOverride,
    BranchReturn, BranchCreditCustomer, BranchStaffDuty,
)


def dec(v) -> Decimal:
    if v is None or v == "":
        return D0
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return D0


def _dt(v):
    """Branch timestamps arrive as ISO strings over the wire and as datetimes from the local
    script. Accept both rather than making the caller care."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _identifiable(rows: list[dict], *keys: str) -> tuple[list[dict], int]:
    """The rows that carry the keys deciding what they are, and how many didn't.

    Every value in a snapshot is coerced (`dec`, `_int`, `_dt`), so a missing figure lands as a zero.
    The keys that say *what a row is* — its day, its sku — can't be coerced into anything, and
    reading them straight raised a KeyError that the route turned into a 500, which the branch reads
    as "head office refused this branch's figures" and drops the whole snapshot. One malformed row
    then costs a branch its entire month. Skipping it and counting it is what SnapshotIn's docstring
    already promises the branch (schemas/registration.py).
    """
    kept = [r for r in rows if all(r.get(key) not in (None, "") for key in keys)]
    return kept, len(rows) - len(kept)


async def apply_aggregates(branch: Branch, data: dict, source: str = "sync") -> dict:
    """Replace this branch's entire aggregate picture with what it just sent.

    One transaction: a half-applied snapshot — yesterday's product rows against today's daily
    totals — would be a dashboard that silently disagrees with itself, and nobody would know which
    half to trust.
    """
    skipped = 0

    def usable(rows: list[dict], *keys: str) -> list[dict]:
        nonlocal skipped
        kept, dropped = _identifiable(rows or [], *keys)
        skipped += dropped
        return kept

    daily = usable(data.get("daily"), "day")
    products = usable(data.get("products"), "day", "sku")
    cashiers = usable(data.get("cashiers"), "day")
    product_cashiers = usable(data.get("productCashiers"), "day", "sku")
    hourly = usable(data.get("hourly"), "day")
    till_closes = usable(data.get("tillCloses"), "day")
    duties = usable(data.get("duties"), "day")
    tenders = usable(data.get("tenders"), "day")
    overrides = usable(data.get("overrides"), "day")
    returns = usable(data.get("returns"), "day")
    # Credit customers and alerts are keyed off nothing but their own values, which are all coerced,
    # so there is nothing here that could fail to identify a row.
    credit_customers = data.get("creditCustomers") or []
    alerts = data.get("alerts") or []
    stock_value = dec(data.get("stockValue") or data.get("stock_value"))
    latest_day = max((r["day"] for r in daily), default=None)

    async with in_transaction():
        for model in AGGREGATE_MODELS:
            await model.filter(branch=branch).delete()

        await BranchDailyStat.bulk_create([
            BranchDailyStat(
                branch=branch, day=r["day"],
                gross_sales=dec(r.get("grossSales")), disc_total=dec(r.get("discTotal")),
                gst=dec(r.get("gst")), net_sales=dec(r.get("netSales")),
                invoices=_int(r.get("invoices")), items_sold=dec(r.get("itemsSold")),
                cogs=dec(r.get("cogs")),
                returns_value=dec(r.get("returnsValue")), returns_count=_int(r.get("returnsCount")),
                cash_collected=dec(r.get("cashCollected")), credit_sales=dec(r.get("creditSales")),
                cash_in=dec(r.get("cashIn")), cash_out=dec(r.get("cashOut")),
                till_variance=dec(r.get("tillVariance")), tills_closed=_int(r.get("tillsClosed")),
                staff_on_duty=_int(r.get("staffOnDuty")),
                first_sale_at=_dt(r.get("firstSaleAt")), last_sale_at=_dt(r.get("lastSaleAt")),
                named_customers=_int(r.get("namedCustomers")),
                # Stock is a right-now figure, not a per-day one: it belongs to the latest day
                # alone, so a month-wide sum can't add it up 31 times.
                stock_value=stock_value if r["day"] == latest_day else D0,
            ) for r in daily
        ], batch_size=200)

        await BranchCashierStat.bulk_create([
            BranchCashierStat(
                branch=branch, day=r["day"], cashier_name=r.get("cashierName") or "-",
                invoices=_int(r.get("invoices")), net_sales=dec(r.get("netSales")),
            ) for r in cashiers
        ], batch_size=200)

        await BranchProductStat.bulk_create([
            BranchProductStat(
                branch=branch, day=r["day"], product_sku=r["sku"], product_name=r.get("name") or r["sku"],
                department=r.get("department"), category=r.get("category"), brand=r.get("brand"),
                qty=dec(r.get("qty")), net_sales=dec(r.get("netSales")), cogs=dec(r.get("cogs")),
            ) for r in products
        ], batch_size=500)

        # Who sold how much of each item. A branch still on an older version doesn't send it; the item and person
        # views say so rather than showing nobody.
        await BranchProductCashierStat.bulk_create([
            BranchProductCashierStat(
                branch=branch, day=r["day"], product_sku=r["sku"], cashier_name=r.get("cashierName") or "-",
                invoices=_int(r.get("invoices")), qty=dec(r.get("qty")), net_sales=dec(r.get("netSales")), cogs=dec(r.get("cogs")),
            ) for r in product_cashiers
        ], batch_size=500)

        await BranchHourlyStat.bulk_create([
            BranchHourlyStat(
                branch=branch, day=r["day"], hour=_int(r.get("hour")),
                invoices=_int(r.get("invoices")), net_sales=dec(r.get("netSales")),
            ) for r in hourly
        ], batch_size=500)

        await BranchTillClose.bulk_create([
            BranchTillClose(
                branch=branch, day=r["day"], session_number=r.get("sessionNumber") or "-",
                cashier_name=r.get("cashierName") or "-",
                opened_at=_dt(r.get("openedAt")), closed_at=_dt(r.get("closedAt")),
                counter_name=r.get("counterName"),
                opening_float=dec(r.get("openingFloat")), net_cash=dec(r.get("netCash")),
                counted_cash=dec(r.get("countedCash")), variance=dec(r.get("variance")),
            ) for r in till_closes
        ], batch_size=200)

        await BranchStaffDuty.bulk_create([
            BranchStaffDuty(
                branch=branch, day=r["day"], cashier_name=r.get("cashierName") or "-",
                counter_name=r.get("counterName"), spells=_int(r.get("spells")), minutes=_int(r.get("minutes")),
            ) for r in duties
        ], batch_size=500)

        await BranchTenderStat.bulk_create([
            BranchTenderStat(
                branch=branch, day=r["day"], code=r.get("code") or "?", name=r.get("name") or r.get("code") or "?",
                uses=_int(r.get("uses")), amount=dec(r.get("amount")),
            ) for r in tenders
        ], batch_size=500)

        await BranchDiscountOverride.bulk_create([
            BranchDiscountOverride(
                branch=branch, day=r["day"], invoice_number=r.get("invoiceNumber") or "-",
                at=_dt(r.get("at")), cashier_name=r.get("cashierName"), approved_by=r.get("approvedBy"),
                gross=dec(r.get("gross")), disc_total=dec(r.get("discTotal")), net_value=dec(r.get("netValue")),
            ) for r in overrides
        ], batch_size=200)

        await BranchReturn.bulk_create([
            BranchReturn(
                branch=branch, day=r["day"], at=_dt(r.get("at")), against_invoice=r.get("againstInvoice"),
                cashier_name=r.get("cashierName"), refund_total=dec(r.get("refundTotal")),
                product_name=r.get("productName"), product_sku=r.get("productSku"), qty=dec(r.get("qty")),
            ) for r in returns
        ], batch_size=200)

        await BranchCreditCustomer.bulk_create([
            BranchCreditCustomer(
                branch=branch, code=r.get("code") or "-", name=r.get("name") or "-", phone=r.get("phone"),
                tier=r.get("tier"), credit_limit=dec(r.get("creditLimit")),
                credit_balance=dec(r.get("creditBalance")),
            ) for r in credit_customers
        ], batch_size=200)

        # Alerts arrive already trimmed and ranked by the branch — it is the only side that can
        # see the whole catalog, and shipping 47,000 rows so the Cloud can keep 200 of them would
        # be a lot of branch link for no extra truth. `totalOfKind` carries the real count.
        await BranchStockAlert.bulk_create([
            BranchStockAlert(
                branch=branch, kind=r.get("kind") or "low-stock", product_sku=r.get("sku") or "-",
                product_name=r.get("name") or "-", qty=dec(r.get("qty")), expiry=_dt(r.get("expiry")),
                detail=r.get("detail"), total_of_kind=_int(r.get("totalOfKind")),
            ) for r in alerts
        ], batch_size=500)

        branch.last_seen_at = datetime.now(timezone.utc)
        await branch.save(update_fields=["last_seen_at"])

    if skipped:
        # Worth a line even though the snapshot went through: it means the branch is sending
        # something malformed, and nobody would ever find out from figures that merely look thin.
        logs.log.warning(
            "snapshot from %s: %s row(s) had no day or sku and were skipped; the rest was applied",
            branch.code, skipped,
        )

    # Counts are of what was stored, not of what arrived — a figure an operator compares against the
    # branch's own report has to be the number of rows that actually landed.
    return {
        "tradingDays": len(daily),
        "productDays": len(products),
        "cashierDays": len(cashiers),
        "productPersonDays": len(product_cashiers),
        "dutyDays": len(duties),
        "alerts": len(alerts),
        "stockValue": str(stock_value),
        "skippedRows": skipped,
        "source": source,
    }


# ── item-level stock, which arrives in chunks ──────────────────────────────────────────────────

async def begin_stock(branch: Branch, snapshot_id: str, source: str = "sync") -> BranchSnapshotRun:
    run, _ = await BranchSnapshotRun.get_or_create(
        branch=branch, snapshot_id=snapshot_id, defaults={"status": "building", "source": source},
    )
    return run


def _stock_fields(r: dict) -> dict:
    origin = r.get("origin") or {}
    return dict(
        product_name=(r.get("name") or r["sku"])[:200],
        department=r.get("department"), category=r.get("category"), brand=r.get("brand"),
        qty=dec(r.get("qty")), avg_cost=dec(r.get("avgCost")), price=dec(r.get("price")),
        last_sold_at=_dt(r.get("lastSoldAt")), last_received_at=_dt(r.get("lastReceivedAt")),
        units_sold_period=dec(r.get("unitsSold")), days_with_sales=_int(r.get("daysWithSales")),
        origin_warehouse=dec(origin.get("warehouse")), origin_branches=dec(origin.get("branches")),
        origin_within=dec(origin.get("within")),
        flows=r.get("flows") or None, locations=r.get("locations") or None, last_in=r.get("lastIn"),
        last_moved_at=_dt(r.get("lastMovedAt")),
    )


async def add_stock_chunk(branch: Branch, snapshot_id: str, rows: list[dict]) -> int:
    """Append one chunk. Rows carry the snapshot id, so they are invisible to every reader until
    `complete_stock` promotes them — a branch whose link dies mid-push leaves the Cloud showing the
    last complete picture rather than half a shop.

    A row with no sku is skipped rather than raising, the same as in `apply_stock_changes`: there is
    nothing to file it under, and one of them must not cost the branch its whole stock list."""
    rows, skipped = _identifiable(rows, "sku")
    if skipped:
        logs.log.warning("stock list from %s: %s row(s) with no sku were skipped", branch.code, skipped)
    await BranchProductStock.bulk_create([
        BranchProductStock(branch=branch, snapshot_id=snapshot_id, product_sku=r["sku"], **_stock_fields(r))
        for r in rows
    ], batch_size=1000, ignore_conflicts=True)
    return len(rows)


async def apply_stock_changes(branch: Branch, rows: list[dict]) -> dict:
    """Items that moved at the branch since its last full push, written straight into the picture readers
    see. Without a complete picture there is nothing to update — the branch sends the full list first."""
    snapshot_id = await latest_stock_snapshot(branch)
    if snapshot_id is None:
        return {"applied": 0, "needsFull": True}
    now = datetime.now(timezone.utc)
    applied = 0
    async with in_transaction():
        existing = {
            s.product_sku: s for s in await BranchProductStock.filter(
                branch=branch, snapshot_id=snapshot_id, product_sku__in=[r["sku"] for r in rows if r.get("sku")],
            )
        }
        new = []
        for r in rows:
            if not r.get("sku"):
                continue
            fields = {**_stock_fields(r), "changed_at": now}
            row = existing.get(r["sku"])
            if row:
                await BranchProductStock.filter(id=row.id).update(**fields)
            else:
                new.append(BranchProductStock(branch=branch, snapshot_id=snapshot_id, product_sku=r["sku"], **fields))
            applied += 1
        if new:
            await BranchProductStock.bulk_create(new, batch_size=1000, ignore_conflicts=True)
        await BranchSnapshotRun.filter(branch=branch, snapshot_id=snapshot_id).update(last_change_at=now)
    return {"applied": applied, "needsFull": False}


async def complete_stock(branch: Branch, snapshot_id: str) -> dict:
    """The branch says the picture is whole. Promote it and drop every earlier generation."""
    async with in_transaction():
        kept = await BranchProductStock.filter(branch=branch, snapshot_id=snapshot_id).count()
        await BranchProductStock.filter(branch=branch).exclude(snapshot_id=snapshot_id).delete()
        run = await BranchSnapshotRun.get_or_none(branch=branch, snapshot_id=snapshot_id)
        if run:
            run.status = "complete"
            run.completed_at = datetime.now(timezone.utc)
            run.stock_rows = kept
            await run.save()
        # Superseded runs are history, not clutter — but only the last few are interesting.
        stale = await BranchSnapshotRun.filter(branch=branch).exclude(snapshot_id=snapshot_id).order_by("-started_at")
        for old in stale[5:]:
            await old.delete()
    return {"stockRows": kept}


async def latest_stock_snapshot(branch: Branch) -> str | None:
    """The generation every stock reader should filter on. Anything else is either superseded or
    still arriving."""
    run = await BranchSnapshotRun.filter(branch=branch, status="complete").order_by("-completed_at").first()
    return run.snapshot_id if run else None
