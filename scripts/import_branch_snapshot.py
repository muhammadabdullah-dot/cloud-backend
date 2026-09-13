"""Import one branch's performance into the Cloud's snapshot tables.

    python -m scripts.import_branch_snapshot --branch HO --db ../branch-server/branch.db

**This is a stand-in for the sync protocol, not part of it.** It does by hand, once, what the
sync worker (I7) will do continuously: read what a branch knows and materialize the Cloud's view
of it. It is a script and never a runtime path — the Cloud Server process itself never opens a
branch database, because the two services share no storage (contracts.md §1).

Doing it this way rather than seeding invented numbers has a specific payoff: every Executive
figure can be checked against the Branch App's own Reports screen. If Executive says Head Office
sold Rs 1.7m and Branch Reports says something else, that is a real bug, which is not something a
hardcoded fixture can ever tell you.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM
from app.models import (
    Branch,
    BranchCashierStat,
    BranchCreditCustomer,
    BranchDailyStat,
    BranchDiscountOverride,
    BranchHourlyStat,
    BranchProductStat,
    BranchReturn,
    BranchStockAlert,
    BranchTenderStat,
    BranchTillClose,
)

# Branch trading days are stored shifted so the stored UTC hour maps to the branch's own local
# clock +5. Converting here means the hour-of-day profile reads as real shop hours (09:00–20:00)
# rather than as a UTC artefact nobody recognises.
PKT_OFFSET_HOURS = 5

# A dashboard is a glance. A real catalog can put thousands of items under the low-stock line, so
# only the most urgent are stored — `total_of_kind` carries the true count alongside.
ALERTS_PER_KIND = 200
LOW_STOCK_THRESHOLD = Decimal("20")
NEAR_EXPIRY_DAYS = 30

D0 = Decimal("0")


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else D0


def read_branch(db_path: Path) -> dict:
    """Everything the Cloud needs from one branch database, in plain SQL.

    Deliberately read-only and deliberately not using the branch's ORM models: this script must
    keep working when the branch schema moves on, and failing loudly on a renamed column beats
    importing a silently wrong number.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]

    daily = q("""
        SELECT date(at) AS day,
               COUNT(*)                         AS invoices,
               COALESCE(SUM(gross), 0)          AS gross_sales,
               COALESCE(SUM(disc_total), 0)     AS disc_total,
               COALESCE(SUM(gst), 0)            AS gst,
               COALESCE(SUM(net_value), 0)      AS net_sales,
               COALESCE(SUM(CASE WHEN is_credit_sale THEN net_value ELSE 0 END), 0) AS credit_sales,
               COALESCE(SUM(CASE WHEN is_credit_sale THEN 0 ELSE net_value END), 0) AS cash_collected,
               COUNT(DISTINCT CASE WHEN party_id IS NOT NULL THEN party_id END)     AS named_customers,
               MIN(at) AS first_sale_at,
               MAX(at) AS last_sale_at
        FROM sale_records GROUP BY date(at)
    """)

    items = q("""
        SELECT date(s.at) AS day, COALESCE(SUM(sl.qty), 0) AS items_sold
        FROM sale_lines sl JOIN sale_records s ON s.id = sl.sale_id
        GROUP BY date(s.at)
    """)

    # Cost of goods sold, from each product's weighted-average cost at import time. An exact
    # figure would need the cost captured on the sale line itself, which the branch does not
    # record — noted rather than silently presented as exact.
    cogs = q("""
        SELECT date(s.at) AS day, COALESCE(SUM(sl.qty * COALESCE(p.avg_cost, 0)), 0) AS cogs
        FROM sale_lines sl
        JOIN sale_records s ON s.id = sl.sale_id
        JOIN products p ON p.id = sl.product_id
        GROUP BY date(s.at)
    """)

    returns = q("""
        SELECT date(at) AS day, COUNT(*) AS returns_count,
               COALESCE(SUM(refund_total), 0) AS returns_value
        FROM return_records GROUP BY date(at)
    """)

    tills = q("""
        SELECT date(closed_at) AS day, COUNT(*) AS tills_closed,
               COALESCE(SUM(variance), 0) AS till_variance
        FROM till_sessions WHERE closed_at IS NOT NULL GROUP BY date(closed_at)
    """)

    cash = q("""
        SELECT date(at) AS day,
               COALESCE(SUM(CASE WHEN kind = 'in'  THEN amount ELSE 0 END), 0) AS cash_in,
               COALESCE(SUM(CASE WHEN kind = 'out' THEN amount ELSE 0 END), 0) AS cash_out
        FROM cash_movements GROUP BY date(at)
    """)

    staff = q("""
        SELECT date(at) AS day, COUNT(DISTINCT cashier_id) AS staff_on_duty
        FROM sale_records GROUP BY date(at)
    """)

    cashiers = q("""
        SELECT date(s.at) AS day, u.name AS cashier_name,
               COUNT(*) AS invoices, COALESCE(SUM(s.net_value), 0) AS net_sales
        FROM sale_records s JOIN users u ON u.id = s.cashier_id
        GROUP BY date(s.at), u.name
    """)

    products = q("""
        SELECT date(s.at) AS day, p.sku AS product_sku, p.name AS product_name,
               p.department, p.category, p.brand,
               COALESCE(SUM(sl.qty), 0) AS qty,
               COALESCE(SUM(sl.qty * sl.unit_price), 0) AS net_sales,
               COALESCE(SUM(sl.qty * COALESCE(p.avg_cost, 0)), 0) AS cogs
        FROM sale_lines sl
        JOIN sale_records s ON s.id = sl.sale_id
        JOIN products p ON p.id = sl.product_id
        GROUP BY date(s.at), p.sku
    """)

    # Stock value at sale price, folded over the whole ledger — the same figure the Branch App's
    # own Inventory Report computes server-side.
    stock_value = q("""
        SELECT COALESCE(SUM(b.qty * p.price), 0) AS stock_value
        FROM (SELECT product_id, SUM(qty) AS qty FROM stock_movements
              GROUP BY product_id HAVING SUM(qty) != 0) b
        JOIN products p ON p.id = b.product_id
    """)[0]["stock_value"]

    balances = q("""
        SELECT p.sku, p.name, b.qty
        FROM (SELECT product_id, SUM(qty) AS qty FROM stock_movements GROUP BY product_id) b
        JOIN products p ON p.id = b.product_id
    """)

    batches = q("""
        SELECT p.sku, p.name, bt.expiry, bt.received_qty
        FROM batches bt JOIN products p ON p.id = bt.product_id
        WHERE bt.expiry IS NOT NULL
    """)

    # ── the grain a drill-down lands on ─────────────────────────────────────
    hourly = q("""
        SELECT date(at) AS day, cast(strftime('%H', at) AS int) AS utc_hour,
               COUNT(*) AS invoices, COALESCE(SUM(net_value), 0) AS net_sales
        FROM sale_records GROUP BY date(at), utc_hour
    """)

    till_closes = q("""
        SELECT date(s.closed_at) AS day, s.session_number, u.name AS cashier_name,
               s.opened_at, s.closed_at, s.opening_float, s.net_cash, s.counted_cash, s.variance
        FROM till_sessions s LEFT JOIN users u ON u.id = s.opened_by_id
        WHERE s.closed_at IS NOT NULL
    """)

    tenders = q("""
        SELECT date(s.at) AS day, t.code, COALESCE(m.name, t.code) AS name,
               COUNT(*) AS uses, COALESCE(SUM(t.amount), 0) AS amount
        FROM sale_tenders t
        JOIN sale_records s ON s.id = t.sale_id
        LEFT JOIN payment_methods m ON m.code = t.code
        GROUP BY date(s.at), t.code
    """)

    overrides = q("""
        SELECT date(s.at) AS day, s.invoice_number, s.at,
               c.name AS cashier_name, a.name AS approved_by,
               s.gross, s.disc_total, s.net_value
        FROM sale_records s
        LEFT JOIN users c ON c.id = s.cashier_id
        LEFT JOIN users a ON a.id = s.discount_override_by_id
        WHERE s.discount_override_by_id IS NOT NULL
    """)

    return_rows = q("""
        SELECT date(r.at) AS day, r.at, s.invoice_number AS against_invoice,
               u.name AS cashier_name, r.refund_total,
               p.name AS product_name, p.sku AS product_sku, rl.qty
        FROM return_records r
        LEFT JOIN sale_records s ON s.id = r.against_id
        LEFT JOIN users u ON u.id = r.cashier_id
        LEFT JOIN return_lines rl ON rl.return_record_id = r.id
        LEFT JOIN products p ON p.id = rl.product_id
    """)

    credit = q("""
        SELECT code, name, phone, tier, credit_limit, credit_balance
        FROM parties WHERE credit_allowed = 1 AND is_walk_in = 0
    """)

    conn.close()
    return dict(
        daily=daily, items=items, cogs=cogs, returns=returns, tills=tills, cash=cash,
        staff=staff, cashiers=cashiers, products=products,
        stock_value=stock_value, balances=balances, batches=batches,
        hourly=hourly, till_closes=till_closes, tenders=tenders,
        overrides=overrides, return_rows=return_rows, credit=credit,
    )


def _by_day(rows: list[dict]) -> dict[str, dict]:
    return {r["day"]: r for r in rows if r.get("day")}


def build_alerts(data: dict) -> list[tuple[str, list[dict], int]]:
    """(kind, rows-to-store, true-total) for each stock exception."""
    out_of_stock, low_stock = [], []
    for r in data["balances"]:
        qty = _dec(r["qty"])
        if qty <= D0:
            out_of_stock.append({"sku": r["sku"], "name": r["name"], "qty": qty, "expiry": None, "detail": None})
        elif qty < LOW_STOCK_THRESHOLD:
            low_stock.append({"sku": r["sku"], "name": r["name"], "qty": qty, "expiry": None, "detail": f"{qty} left"})

    now = datetime.now(timezone.utc)
    near, expired = [], []
    for r in data["batches"]:
        raw = r["expiry"]
        try:
            exp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        days = (exp - now).days
        if days >= 0 and days > NEAR_EXPIRY_DAYS:
            continue  # plenty of shelf life left — not an exception
        row = {"sku": r["sku"], "name": r["name"], "qty": _dec(r["received_qty"]), "expiry": exp,
               "detail": f"expires in {days} days" if days >= 0 else f"expired {abs(days)} days ago"}
        if days < 0:
            expired.append(row)
        else:
            near.append(row)

    low_stock.sort(key=lambda r: r["qty"])
    near.sort(key=lambda r: r["expiry"])
    expired.sort(key=lambda r: r["expiry"])
    return [
        ("out-of-stock", out_of_stock[:ALERTS_PER_KIND], len(out_of_stock)),
        ("low-stock", low_stock[:ALERTS_PER_KIND], len(low_stock)),
        ("near-expiry", near[:ALERTS_PER_KIND], len(near)),
        ("expired", expired[:ALERTS_PER_KIND], len(expired)),
    ]


async def import_branch(code: str, db_path: Path) -> None:
    branch = await Branch.get_or_none(code=code.upper())
    if not branch:
        raise SystemExit(f"No branch registered with code {code.upper()} — register it in Admin → Branches first.")

    print(f"Reading {db_path} …")
    data = read_branch(db_path)

    items, cogs = _by_day(data["items"]), _by_day(data["cogs"])
    returns, tills = _by_day(data["returns"]), _by_day(data["tills"])
    cash, staff = _by_day(data["cash"]), _by_day(data["staff"])

    # Replace rather than merge: a re-import is a fresh picture of the same branch, and leaving
    # yesterday's rows behind would double-count on any day the branch later corrected.
    await BranchDailyStat.filter(branch=branch).delete()
    await BranchCashierStat.filter(branch=branch).delete()
    await BranchProductStat.filter(branch=branch).delete()
    await BranchStockAlert.filter(branch=branch).delete()
    await BranchHourlyStat.filter(branch=branch).delete()
    await BranchTillClose.filter(branch=branch).delete()
    await BranchTenderStat.filter(branch=branch).delete()
    await BranchDiscountOverride.filter(branch=branch).delete()
    await BranchReturn.filter(branch=branch).delete()
    await BranchCreditCustomer.filter(branch=branch).delete()

    stock_value = _dec(data["stock_value"])
    latest_day = max((r["day"] for r in data["daily"]), default=None)

    daily_rows = []
    for r in data["daily"]:
        day = r["day"]
        daily_rows.append(BranchDailyStat(
            branch=branch, day=day,
            gross_sales=_dec(r["gross_sales"]), disc_total=_dec(r["disc_total"]), gst=_dec(r["gst"]),
            net_sales=_dec(r["net_sales"]), invoices=r["invoices"],
            items_sold=_dec(items.get(day, {}).get("items_sold")),
            cogs=_dec(cogs.get(day, {}).get("cogs")),
            returns_value=_dec(returns.get(day, {}).get("returns_value")),
            returns_count=returns.get(day, {}).get("returns_count", 0) or 0,
            cash_collected=_dec(r["cash_collected"]), credit_sales=_dec(r["credit_sales"]),
            cash_in=_dec(cash.get(day, {}).get("cash_in")), cash_out=_dec(cash.get(day, {}).get("cash_out")),
            till_variance=_dec(tills.get(day, {}).get("till_variance")),
            tills_closed=tills.get(day, {}).get("tills_closed", 0) or 0,
            staff_on_duty=staff.get(day, {}).get("staff_on_duty", 0) or 0,
            first_sale_at=r["first_sale_at"], last_sale_at=r["last_sale_at"],
            named_customers=r["named_customers"] or 0,
            # Stock is a right-now figure, not a per-day one; it belongs to the latest day only,
            # so a month-wide sum can't accidentally add it up 31 times.
            stock_value=stock_value if day == latest_day else D0,
        ))
    await BranchDailyStat.bulk_create(daily_rows, batch_size=200)

    await BranchCashierStat.bulk_create([
        BranchCashierStat(
            branch=branch, day=r["day"], cashier_name=r["cashier_name"],
            invoices=r["invoices"], net_sales=_dec(r["net_sales"]),
        ) for r in data["cashiers"]
    ], batch_size=200)

    await BranchProductStat.bulk_create([
        BranchProductStat(
            branch=branch, day=r["day"], product_sku=r["product_sku"], product_name=r["product_name"],
            department=r["department"], category=r["category"], brand=r["brand"],
            qty=_dec(r["qty"]), net_sales=_dec(r["net_sales"]), cogs=_dec(r["cogs"]),
        ) for r in data["products"]
    ], batch_size=500)

    await BranchHourlyStat.bulk_create([
        BranchHourlyStat(
            branch=branch, day=r["day"], hour=(r["utc_hour"] + PKT_OFFSET_HOURS) % 24,
            invoices=r["invoices"], net_sales=_dec(r["net_sales"]),
        ) for r in data["hourly"]
    ], batch_size=500)

    await BranchTillClose.bulk_create([
        BranchTillClose(
            branch=branch, day=r["day"], session_number=r["session_number"],
            cashier_name=r["cashier_name"] or "—", opened_at=r["opened_at"], closed_at=r["closed_at"],
            opening_float=_dec(r["opening_float"]), net_cash=_dec(r["net_cash"]),
            counted_cash=_dec(r["counted_cash"]), variance=_dec(r["variance"]),
        ) for r in data["till_closes"]
    ], batch_size=200)

    await BranchTenderStat.bulk_create([
        BranchTenderStat(
            branch=branch, day=r["day"], code=r["code"], name=r["name"],
            uses=r["uses"], amount=_dec(r["amount"]),
        ) for r in data["tenders"]
    ], batch_size=500)

    await BranchDiscountOverride.bulk_create([
        BranchDiscountOverride(
            branch=branch, day=r["day"], invoice_number=r["invoice_number"], at=r["at"],
            cashier_name=r["cashier_name"], approved_by=r["approved_by"],
            gross=_dec(r["gross"]), disc_total=_dec(r["disc_total"]), net_value=_dec(r["net_value"]),
        ) for r in data["overrides"]
    ], batch_size=200)

    await BranchReturn.bulk_create([
        BranchReturn(
            branch=branch, day=r["day"], at=r["at"], against_invoice=r["against_invoice"],
            cashier_name=r["cashier_name"], refund_total=_dec(r["refund_total"]),
            product_name=r["product_name"], product_sku=r["product_sku"], qty=_dec(r["qty"]),
        ) for r in data["return_rows"]
    ], batch_size=200)

    await BranchCreditCustomer.bulk_create([
        BranchCreditCustomer(
            branch=branch, code=r["code"], name=r["name"], phone=r["phone"], tier=r["tier"],
            credit_limit=_dec(r["credit_limit"]), credit_balance=_dec(r["credit_balance"]),
        ) for r in data["credit"]
    ], batch_size=200)

    alert_rows = []
    for kind, rows, total in build_alerts(data):
        for r in rows:
            alert_rows.append(BranchStockAlert(
                branch=branch, kind=kind, product_sku=r["sku"], product_name=r["name"],
                qty=r["qty"], expiry=r["expiry"], detail=r["detail"], total_of_kind=total,
            ))
    await BranchStockAlert.bulk_create(alert_rows, batch_size=500)

    branch.last_seen_at = datetime.now(timezone.utc)
    await branch.save()

    counts = defaultdict(int)
    for a in alert_rows:
        counts[a.kind] += 1
    print(f"  {branch.code} {branch.name}")
    print(f"    {len(daily_rows)} trading days  ({min(r.day for r in daily_rows)} … {max(r.day for r in daily_rows)})"
          if daily_rows else "    no trading days")
    print(f"    {len(data['cashiers'])} cashier-days, {len(data['products'])} product-days")
    print(f"    stock value Rs {stock_value:,.0f}")
    print(f"    alerts stored: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "    no alerts")
    print(f"    detail: {len(data['hourly'])} hour-buckets, {len(data['till_closes'])} till closes, "
          f"{len(data['tenders'])} tender-days, {len(data['overrides'])} override(s), "
          f"{len(data['return_rows'])} return line(s), {len(data['credit'])} credit customer(s)")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--branch", required=True, help="Branch code as registered on the Cloud, e.g. HO")
    ap.add_argument("--db", required=True, help="Path to that branch's branch.db")
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"No such database: {db_path}")

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        await import_branch(args.branch, db_path)
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
