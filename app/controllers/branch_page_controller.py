"""One branch, thoroughly: the Executive's page for each branch.

Each tab is its own request, so opening a branch only costs what is on screen: the overview (headline figures against
the period before), people, stock, the branch's own books, and transfers in and out. Sales and Products are not here:
the page shows the KPI views (routes/executive.py /kpis) limited to the branch, so a figure on this page and the same
figure on the dashboard come from one builder and can't disagree.
"""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from tortoise import Tortoise
from tortoise.expressions import Q

from app.controllers import executive_controller as xc
from app.core.pk_time import pk_day
from app.models import BranchDailyStat, BranchSnapshotRun, BranchStockAlert, Transfer, User
from app.services import analytics_service as an
from app.services import executive_service as ex
from app.services.rbac_service import has_permission

D0 = Decimal("0")
CENTS = Decimal("0.01")
# Days of cover worth a word: under this an Item runs out soon, over the other it is sitting.
SOON_DAYS = 14
SITTING_DAYS = 90


def _s(v) -> str:
    return str((Decimal(str(v)) if v is not None else D0).quantize(CENTS))


def _qty(v) -> str:
    d = Decimal(str(v or 0)).quantize(Decimal("0.001")).normalize()
    return f"{d:f}"


def _iso(v) -> str | None:
    return v.isoformat() if v else None


async def _q(sql: str, *args) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, list(args))


async def scope_for(code: str, period: str | None, start: str | None, end: str | None) -> xc.Scope:
    """The dates, and the branch the page is about. An unknown code is a plain 404, so the page can say so."""
    base = await xc.resolve_scope(period, start, end, None)
    branch = await ex.branch_by_code(code or "")
    if not branch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No branch has the code {(code or '').upper()}. Choose one from the branch list.")
    return xc.Scope(base.period, branch, base.basis)


def _before(p: ex.Period) -> ex.Period | None:
    # "All time" has nothing before it to compare with.
    return None if p.compare_label == "-" else xc._prev(p)


# ── the page itself ───────────────────────────────────────────────────────────────────────────
async def head(scope: xc.Scope) -> dict:
    b = scope.branch
    run = await BranchSnapshotRun.filter(branch_id=b.id, status="complete").order_by("-completed_at").first()
    last_day = await BranchDailyStat.filter(branch_id=b.id).order_by("-day").first()
    return {
        "branch": {
            "code": b.code, "name": b.name, "city": b.city, "address": b.address, "phone": b.phone, "status": b.status,
            "hasServer": b.verified_at is not None, "lastSeenAt": _iso(b.last_seen_at),
            "stockAsOf": _iso(run.completed_at) if run else None, "lastTradingDay": _iso(last_day.day) if last_day else None,
        },
        "reporting": last_day is not None,
        "period": xc._period_out(scope.period).model_dump(mode="json"),
        "periods": [xc._period_out(ex.resolve_period(p)).model_dump(mode="json") for p in ex.PERIOD_IDS],
        "branches": await an.branch_options(),
    }


async def _stock_run(branch_id) -> BranchSnapshotRun | None:
    return await BranchSnapshotRun.filter(branch_id=branch_id, status="complete").order_by("-completed_at").first()


async def _stock_totals(branch_id, run: BranchSnapshotRun | None) -> dict:
    if not run:
        return {"lines": 0, "units": "0", "valueCost": "0.00", "valueRetail": "0.00", "outOfStock": 0}
    rows = await _q("""
        SELECT SUM(CASE WHEN CAST(qty AS REAL) > 0 THEN 1 ELSE 0 END) AS lines,
               SUM(CASE WHEN CAST(qty AS REAL) > 0 THEN CAST(qty AS REAL) ELSE 0 END) AS units,
               SUM(CASE WHEN CAST(qty AS REAL) > 0 THEN CAST(qty AS REAL) * CAST(avg_cost AS REAL) ELSE 0 END) AS cost,
               SUM(CASE WHEN CAST(qty AS REAL) > 0 THEN CAST(qty AS REAL) * CAST(price AS REAL) ELSE 0 END) AS retail,
               SUM(CASE WHEN CAST(qty AS REAL) <= 0 THEN 1 ELSE 0 END) AS out_lines
        FROM branch_product_stock WHERE branch_id = ? AND snapshot_id = ?
    """, str(branch_id), run.snapshot_id)
    r = rows[0] if rows else {}
    return {"lines": int(r.get("lines") or 0), "units": _qty(r.get("units")), "valueCost": _s(r.get("cost") or 0),
            "valueRetail": _s(r.get("retail") or 0), "outOfStock": int(r.get("out_lines") or 0)}


def _figure(key: str, label: str, value: Decimal, before: Decimal | None, fmt: str, better: str | None,
            *, kpi: str | None = None, tab: str | None = None, sub: str | None = None, hint: str = "") -> dict:
    change = None
    if before is not None and before != D0:
        diff = value - before
        change = {
            "percent": float(diff / abs(before) * 100),
            "direction": "up" if diff > D0 else "down" if diff < D0 else "flat",
            "good": None if diff == D0 or better is None else ((diff > D0) == (better == "up")),
        }
    return {"key": key, "label": label, "value": _s(value), "before": None if before is None else _s(before), "format": fmt,
            "change": change, "kpi": kpi, "tab": tab, "sub": sub, "hint": hint}


async def overview(scope: xc.Scope) -> dict:
    p, bid, b = scope.period, scope.branch_id, scope.branch
    prev = _before(p)
    now = await ex.totals_for(p, bid)
    was = await ex.totals_for(prev, bid) if prev else None
    overrides = await ex.discount_overrides(p, branch_id=bid)
    run = await _stock_run(b.id)
    stock = await _stock_totals(b.id, run)
    retail = await ex.latest_stock_value(bid)
    _, alert_totals = await ex.stock_alerts(branch_id=bid, limit=1)

    def w(attr: str) -> Decimal | None:
        return None if was is None else Decimal(str(getattr(was, attr)))

    figures = [
        _figure("netSales", "Net sales", now.net_sales, w("net_sales"), "money", "up", tab="sales",
                sub=f"{now.invoices:,} bill(s) over {now.days} trading day(s)", hint="Everything sold, less discounts and returns."),
        _figure("grossProfit", "Gross profit", now.gross_profit, w("gross_profit"), "money", "up", kpi="gross-profit",
                sub=f"{float(now.margin_percent):.1f}% margin", hint="Net sales less what the goods sold had cost."),
        _figure("margin", "Gross margin", now.margin_percent, w("margin_percent"), "percent", "up", kpi="gross-profit",
                hint="Gross profit as a part of net sales."),
        _figure("bills", "Bills", Decimal(now.invoices), None if was is None else Decimal(was.invoices), "count", "up", kpi="customer-count",
                sub=f"{now.named_customers:,} to named customers"),
        _figure("avgBill", "Average bill", now.avg_basket, w("avg_basket"), "money", "up", kpi="avg-basket",
                hint="Net sales divided by the number of bills."),
        _figure("returns", "Returns", now.returns_value, w("returns_value"), "money", "down", kpi="returns",
                sub=f"{now.returns_count:,} return(s)"),
        _figure("discounts", "Discounts", now.disc_total, w("disc_total"), "money", "down", kpi="discount-overrides",
                sub=f"{len(overrides):,} above a salesperson's own limit", hint="Every discount given on bills in the period."),
        _figure("stockValue", "Stock value", retail, None, "money", None, tab="stock",
                sub=f"Rs {Decimal(stock['valueCost']):,.0f} at cost, {stock['lines']:,} Item(s)", hint="As last reported, at sale price."),
        _figure("cash", "Cash position", now.net_cash, w("net_cash"), "money", "up", kpi="cash-position",
                sub=f"Rs {now.credit_sales:,.0f} sold on credit", hint="Cash taken, plus drawer top-ups, less payouts."),
        _figure("tillVariance", "Till variance", now.till_variance, None, "money", None, kpi="till-variance",
                sub=f"{now.tills_closed:,} till close(s)", hint="Counted cash less what the till expected."),
    ]
    trend = await ex.daily_series(p, "net_sales", bid)
    transfers = await _transfer_counts(bid, p)
    attention = [
        {"label": "Out of stock", "count": alert_totals.get("out-of-stock", 0) or stock["outOfStock"], "tab": "stock", "tone": "bad"},
        {"label": "Low stock", "count": alert_totals.get("low-stock", 0), "tab": "stock", "tone": "watch"},
        {"label": "Expired or near expiry", "count": alert_totals.get("near-expiry", 0) + alert_totals.get("expired", 0), "tab": "stock", "tone": "bad"},
        {"label": "Transfers on the way", "count": transfers["inTransit"], "tab": "transfers", "tone": "info"},
        {"label": "Transfer disputes", "count": transfers["disputes"], "tab": "transfers", "tone": "bad"},
    ]
    return {
        "compareLabel": prev.compare_label if prev else None,
        "figures": figures,
        "trend": [{"day": t["day"], "value": _s(t["value"])} for t in trend],
        "attention": attention,
        "notes": [
            "Sales, bills, returns, discounts and cash are as the branch last reported them. Stock value is its latest stock picture.",
            f"Each change is against {prev.compare_label}, the same number of days just before." if prev else "All time has nothing before it to compare with.",
        ],
    }


# ── people ────────────────────────────────────────────────────────────────────────────────────
async def people(scope: xc.Scope) -> dict:
    p, bid = scope.period, scope.branch_id
    ranking = await ex.cashier_ranking(p, limit=100000, branch_id=bid)
    overrides = await ex.discount_overrides(p, branch_id=bid)
    returns = await ex.returns_detail(p, bid)
    closes = await ex.till_closes(p, branch_id=bid)
    rows: dict[str, dict] = {}

    def row(name: str) -> dict:
        return rows.setdefault(name, {
            "name": name, "invoices": 0, "netSales": D0, "avgBill": D0, "units": D0, "grossProfit": None, "days": 0,
            "perDay": D0, "share": D0, "discountsOnBills": D0, "discountsOnBillsCount": 0, "discountsApproved": D0,
            "discountsApprovedCount": 0, "returnsValue": D0, "returnsCount": 0, "tillVariance": D0, "tillCloses": 0,
        })

    for r in ranking:
        me = row(r["name"])
        me.update(invoices=r["invoices"], netSales=r["netSales"], avgBill=r["avgBasket"], units=r["qty"], grossProfit=r["grossProfit"],
                  days=r["days"], share=r["share"])
        me["perDay"] = ex._money(r["netSales"] / r["days"]) if r["days"] else D0
    for o in overrides:
        if o["cashier"]:
            me = row(o["cashier"])
            me["discountsOnBills"] += o["discTotal"] or D0
            me["discountsOnBillsCount"] += 1
        if o["approvedBy"]:
            me = row(o["approvedBy"])
            me["discountsApproved"] += o["discTotal"] or D0
            me["discountsApprovedCount"] += 1
    for r in returns:
        # Once per return, not once per line: a return of three Items is one return and one refund.
        if r["cashier"] and r["firstLine"]:
            me = row(r["cashier"])
            me["returnsValue"] += r["refundTotal"] or D0
            me["returnsCount"] += 1
    for c in closes:
        if c["cashier"]:
            me = row(c["cashier"])
            me["tillVariance"] += c["variance"] or D0
            me["tillCloses"] += 1

    def out(r: dict) -> dict:
        return {**r, **{k: _s(r[k]) for k in ("netSales", "avgBill", "perDay", "share", "discountsOnBills", "discountsApproved", "returnsValue", "tillVariance")},
                "units": _qty(r["units"]), "grossProfit": None if r["grossProfit"] is None else _s(r["grossProfit"])}

    # Ranked by sales per day worked, so someone who worked two days is not "lowest" for that alone.
    sellers = sorted((r for r in rows.values() if r["invoices"]), key=lambda r: (r["perDay"], r["netSales"]), reverse=True)
    n = len(sellers)
    top_n = min(5, math.ceil(n / 2))
    low_n = min(5, n // 2)
    everyone = sorted(rows.values(), key=lambda r: (r["netSales"], r["name"]), reverse=True)
    return {
        "top": [out(r) for r in sellers[:top_n]],
        "lowest": [out(r) for r in list(reversed(sellers))[:low_n]],
        "rows": [out(r) for r in everyone],
        "notes": [
            "Top and lowest are ranked by net sales per day worked, so someone who worked fewer days isn't marked low for that alone.",
            "A person's sales are the bills they rang at this branch. Units are the Items on those bills.",
            "Discounts on their bills and discounts they approved are the ones above a salesperson's own limit, which a manager signed off.",
            "Returns are the refunds they took. Till variance is counted cash less what the till expected, on the tills they closed.",
            "Someone who only approved discounts or took returns shows in the full list with no sales of their own.",
        ],
    }


# ── stock ─────────────────────────────────────────────────────────────────────────────────────
async def stock(scope: xc.Scope) -> dict:
    p, b = scope.period, scope.branch
    bid = str(b.id)
    run = await _stock_run(b.id)
    totals = await _stock_totals(b.id, run)
    alerts, alert_totals = await ex.stock_alerts(branch_id=bid, limit=100000)
    now = await ex.totals_for(p, bid)
    days = await an._trading_days(p.start, p.end, b.code)

    def alert_rows(kinds: tuple[str, ...]) -> list[dict]:
        rows = [a for a in alerts if a["kind"] in kinds]
        return [{"sku": a["sku"], "name": a["name"], "kind": a["kind"], "qty": _qty(a["qty"]), "expiry": _iso(a["expiry"]), "detail": a["detail"]}
                for a in rows[:100]]

    cover_rows: list[dict] = []
    not_sold: list[dict] = []
    not_sold_summary = await an.dead_stock_summary(b.code)
    if run:
        cover_rows = await _q("""
            WITH sold AS (
                SELECT product_sku AS sku, SUM(CAST(qty AS REAL)) AS units
                FROM branch_product_stats WHERE branch_id = ? AND day >= ? AND day <= ? GROUP BY product_sku
            )
            SELECT s.product_sku AS sku, s.product_name AS name, s.category AS category, CAST(s.qty AS REAL) AS qty,
                   CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL) AS value_cost, sold.units AS units, s.last_sold_at AS last_sold_at
            FROM branch_product_stock s JOIN sold ON sold.sku = s.product_sku
            WHERE s.branch_id = ? AND s.snapshot_id = ? AND sold.units > 0
        """, bid, str(p.start), str(p.end), bid, run.snapshot_id)
        not_sold = await _q(f"""
            SELECT s.product_sku AS sku, s.product_name AS name, s.category AS category, CAST(s.qty AS REAL) AS qty,
                   CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL) AS value_cost, s.last_sold_at AS last_sold_at
            FROM branch_product_stock s
            WHERE s.branch_id = ? AND s.snapshot_id = ? AND CAST(s.qty AS REAL) > 0
              AND (s.last_sold_at IS NULL OR julianday('now') - julianday(s.last_sold_at) >= {an.STALE_DAYS})
            ORDER BY value_cost DESC LIMIT 50
        """, bid, run.snapshot_id)

    per_item = []
    for r in cover_rows:
        per_day = (r["units"] or 0) / days if days else 0
        cover = (r["qty"] or 0) / per_day if per_day > 0 and (r["qty"] or 0) > 0 else (0.0 if per_day > 0 else None)
        per_item.append({"sku": r["sku"], "name": r["name"], "category": r["category"], "qty": _qty(r["qty"]), "perDay": round(per_day, 2),
                         "cover": None if cover is None else round(cover, 1), "valueCost": _s(r["value_cost"] or 0), "lastSoldAt": r["last_sold_at"]})
    soon = sorted((r for r in per_item if r["cover"] is not None and r["cover"] < SOON_DAYS), key=lambda r: (r["cover"], -r["perDay"]))[:50]
    sitting = sorted((r for r in per_item if r["cover"] is not None and r["cover"] > SITTING_DAYS), key=lambda r: -Decimal(r["valueCost"]))[:50]

    # Days of cover for the whole shop: stock at cost against what the goods sold cost per trading day.
    cogs_per_day = (now.cogs / days) if days else D0
    shop_cover = float(Decimal(totals["valueCost"]) / cogs_per_day) if cogs_per_day > D0 else None
    stale = [not_sold_summary[k] for k in ("stale", "dead", "never-sold")]
    return {
        "asOf": _iso(run.completed_at) if run else None,
        "totals": {**totals, "shopCover": None if shop_cover is None else round(shop_cover, 1), "tradingDays": days},
        "counts": {
            "outOfStock": alert_totals.get("out-of-stock", 0) or totals["outOfStock"], "lowStock": alert_totals.get("low-stock", 0),
            "nearExpiry": alert_totals.get("near-expiry", 0), "expired": alert_totals.get("expired", 0),
            "notSold90": sum(s["lines"] for s in stale), "notSold90Value": _s(sum((Decimal(s["value"]) for s in stale), D0)),
            "runningOut": len([r for r in per_item if r["cover"] is not None and r["cover"] < SOON_DAYS]),
        },
        "lowStock": alert_rows(("out-of-stock", "low-stock")),
        "nearExpiry": alert_rows(("expired", "near-expiry")),
        "notSold90": [{"sku": r["sku"], "name": r["name"], "category": r["category"], "qty": _qty(r["qty"]), "valueCost": _s(r["value_cost"] or 0),
                       "lastSoldAt": r["last_sold_at"]} for r in not_sold],
        "runningOut": soon,
        "sitting": sitting,
        "notes": [
            "Stock is the branch's latest complete stock picture. Low stock, out of stock and expiry are the branch's own lists, most urgent first.",
            f"Days of cover is stock on hand divided by units sold per trading day in the period ({days} trading day(s)). For the whole shop it is stock at cost against the cost of what sold per day.",
            f"Running out soon means under {SOON_DAYS} days of cover; sitting means over {SITTING_DAYS} days, largest money first.",
            f"Not sold in {an.STALE_DAYS} days counts stock whose last sale, over the branch's whole history, is {an.STALE_DAYS} or more days ago, or that never sold.",
        ],
    }


# ── the branch's books ────────────────────────────────────────────────────────────────────────
async def accounts(scope: xc.Scope, user: User) -> dict:
    from app.models import AccountsSettings, Voucher
    from app.services import accounts_reports_service as books

    p, code = scope.period, scope.branch.code
    if not await has_permission(user, "accounts.branch-books", "R"):
        return {"allowed": False, "hasBooks": False}
    chart = await books.chart_map([code])
    if not chart:
        return {"allowed": True, "hasBooks": False}
    prev = _before(p)
    now = await books.income_statement([code], False, p.start, p.end)
    was = await books.income_statement([code], False, prev.start, prev.end) if prev else None
    bal = {k: dr - cr for k, (dr, cr) in (await books._sums([code], end=p.end)).items()}

    def total(kinds, sign=1) -> Decimal:
        return sign * sum((bal.get(k, D0) for k, m in chart.items() if m["kind"] in kinds), D0)

    def pair(key: str) -> dict:
        return {"now": now[key], "before": was[key] if was else None}

    expenses = []
    for section in now["sections"]:
        if section["key"] in ("operating", "financial"):
            expenses += [{"name": g["name"], "amount": g["amount"]} for g in section["groups"]]
    expenses.sort(key=lambda g: -Decimal(g["amount"]))
    row = await AccountsSettings.get_or_none(book=code)
    return {
        "allowed": True, "hasBooks": True, "book": code, "from": p.start.isoformat(), "to": p.end.isoformat(),
        "compareLabel": prev.compare_label if prev else None,
        "netSales": pair("netSales"), "costOfSales": pair("costOfSales"), "grossProfit": pair("grossProfit"), "grossMargin": pair("grossMargin"),
        "otherIncome": pair("otherIncome"),
        "expenses": {"now": _s(Decimal(now["operatingExpenses"]) + Decimal(now["financialExpenses"])),
                     "before": _s(Decimal(was["operatingExpenses"]) + Decimal(was["financialExpenses"])) if was else None},
        "netProfit": pair("netProfit"), "netMargin": pair("netMargin"),
        "cash": _s(total(("cash",))), "bank": _s(total(("bank", "wallet"))),
        "receivable": _s(total(("customer",))), "payable": _s(total(("supplier",), -1)),
        "expenseGroups": expenses[:10],
        "vouchers": await Voucher.filter(book=code, status="posted", date__gte=p.start, date__lte=p.end).count(),
        "booksStart": _iso(row.books_start) if row else None, "lockedUntil": _iso(row.locked_until) if row else None,
        "lastReceivedAt": _iso(row.last_received_at) if row else None,
        "notes": [
            f"From {scope.branch.name}'s own books as they reached head office: posted vouchers only.",
            f"Cash, bank, what customers owe and what is owed to suppliers are balances at the end of the period ({p.end:%d %b %Y}).",
        ],
    }


# ── transfers ─────────────────────────────────────────────────────────────────────────────────
STATUS_LABEL = {
    "requested": "Waiting for an answer", "approved": "Agreed, not sent yet", "dispatched": "On its way", "in_transit": "On its way",
    "received": "Received", "received_short": "Received short", "cancelled": "Cancelled",
}
OPEN = ("requested", "approved", "dispatched", "in_transit")


def _local_day(at: datetime | None):
    if at is None:
        return None
    return pk_day(at)


async def _transfer_rows(branch_id: str) -> list[Transfer]:
    return await Transfer.filter(Q(branch_id=branch_id) | Q(source_branch_id=branch_id)).prefetch_related("lines", "branch", "source_branch")


async def _transfer_counts(branch_id: str, p: ex.Period, rows: list[Transfer] | None = None) -> dict:
    rows = rows if rows is not None else await Transfer.filter(Q(branch_id=branch_id) | Q(source_branch_id=branch_id))
    in_period = [t for t in rows if (d := _local_day(t.requested_at)) and p.start <= d <= p.end]
    return {
        "in": len([t for t in in_period if str(t.branch_id) == branch_id]),
        "out": len([t for t in in_period if str(t.source_branch_id) == branch_id]),
        "inTransit": len([t for t in rows if t.status in ("dispatched", "in_transit")]),
        "waiting": len([t for t in rows if t.status in ("requested", "approved")]),
        "disputes": len([t for t in rows if t.dispute_open]),
    }


async def transfers(scope: xc.Scope) -> dict:
    p, bid = scope.period, scope.branch_id
    rows = await _transfer_rows(bid)
    counts = await _transfer_counts(bid, p, rows)
    out = []
    for t in sorted(rows, key=lambda t: t.requested_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        day = _local_day(t.requested_at)
        if not ((day and p.start <= day <= p.end) or t.status in OPEN or t.dispute_open):
            continue
        incoming = str(t.branch_id) == bid
        other = (t.source_branch.name if t.source_branch_id and t.source_branch else "Central Godown") if incoming else t.branch.name
        sent = sum((line.qty_sent or D0 for line in t.lines), D0)
        received = [line.qty_received for line in t.lines if line.qty_received is not None]
        out.append({
            "id": str(t.id), "number": t.transfer_number, "direction": "In" if incoming else "Out", "other": other,
            "status": t.status, "statusLabel": STATUS_LABEL.get(t.status, t.status.replace("_", " ")), "ack": t.ack_status,
            "requestedAt": _iso(t.requested_at), "dispatchedAt": _iso(t.dispatched_at), "receivedAt": _iso(t.received_at),
            "lines": len(t.lines), "units": _qty(sent), "unitsReceived": _qty(sum(received, D0)) if received else None,
            "valueCost": _s(sum(((line.qty_sent or D0) * (line.unit_cost or D0) for line in t.lines), D0)),
            "dispute": t.dispute_open, "disputeNote": t.dispute_note, "notes": t.notes,
        })
    return {
        "counts": counts, "rows": out[:300],
        "notes": [
            "In counts stock sent to this branch from the godown or another branch; out counts what this branch sent. Both are for transfers asked for in the period.",
            "The list also keeps every transfer still open (waiting, agreed or on its way) and every open dispute, whenever it started.",
        ],
    }
