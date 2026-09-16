"""The Executive control room.

Two kinds of figure live here and they are never blurred together:

  * **live** — computed now, from the Cloud's own tables (the godown ledger, requisitions,
    transfers, suppliers). True as of this request.
  * **branch-snapshot** — what a branch last reported, folded from BranchDailyStat. True as of
    that branch's last sync, which is carried alongside every number as `asOf`.

An executive acting on a number needs to know which of those it is, so `source` and `asOf` travel
with every KPI rather than being a footnote on the page.

Everything folds from BranchDailyStat's one-row-per-branch-per-day grain. No KPI stores its own
total, for the same reason stock on hand is never stored: a derived figure cannot drift from the
rows that explain it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tortoise.exceptions import ValidationError
from tortoise.functions import Count, Sum

from app.models import (
    Bin,
    Branch,
    BranchCashierStat,
    BranchCreditCustomer,
    BranchDailyStat,
    BranchDiscountOverride,
    BranchHourlyStat,
    BranchProductCashierStat,
    BranchProductStat,
    BranchReturn,
    BranchStaffDuty,
    BranchStockAlert,
    BranchTenderStat,
    BranchTillClose,
    GRN,
    Product,
    Requisition,
    StockMovement,
    Supplier,
    Transfer,
)

D0 = Decimal("0")
_CENTS = Decimal("0.01")
LOW_STOCK_THRESHOLD = Decimal("20")


def _money(v: Decimal) -> Decimal:
    """Derived figures are divisions, which in Decimal carry 28 significant digits by default.
    Nobody wants Rs 6600.047619047619047619047619 on a dashboard, or on the wire."""
    return v.quantize(_CENTS)

# Pakistan is UTC+5 with no daylight saving, and branch trading days are stored against that
# local day. Using the server's UTC date would roll the business date over five hours early.
PKT = timezone(timedelta(hours=5))


def business_today() -> date:
    return datetime.now(PKT).date()


# ── periods ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Period:
    id: str
    label: str
    start: date
    end: date
    # The equal-length window immediately before, which every delta is measured against.
    prev_start: date
    prev_end: date
    compare_label: str


def resolve_period(period_id: str, today: date | None = None) -> Period:
    t = today or business_today()
    if period_id == "today":
        return Period("today", "Today", t, t, t - timedelta(days=1), t - timedelta(days=1), "yesterday")
    if period_id == "yesterday":
        y = t - timedelta(days=1)
        return Period("yesterday", "Yesterday", y, y, y - timedelta(days=1), y - timedelta(days=1), "the day before")
    if period_id == "7d":
        start = t - timedelta(days=6)
        return Period("7d", "Last 7 days", start, t, start - timedelta(days=7), start - timedelta(days=1), "the previous 7 days")
    if period_id == "30d":
        start = t - timedelta(days=29)
        return Period("30d", "Last 30 days", start, t, start - timedelta(days=30), start - timedelta(days=1), "the previous 30 days")
    if period_id == "ytd":
        start = date(t.year, 1, 1)
        # 29 February has no counterpart in the previous year, so the comparison window ends on the
        # 28th. Without this the whole Executive module is a 500 for that one day of the year, because
        # every endpoint here resolves a period before it does anything else.
        prev_day = 28 if (t.month, t.day) == (2, 29) else t.day
        return Period("ytd", "This year", start, t, date(t.year - 1, 1, 1), date(t.year - 1, t.month, prev_day), "the same period last year")
    if period_id == "all":
        return Period("all", "All time", date(2000, 1, 1), t, date(2000, 1, 1), date(2000, 1, 1), "—")
    return resolve_period("30d", t)


PERIOD_IDS = ["today", "yesterday", "7d", "30d", "ytd", "all"]


# ── snapshot folds ──────────────────────────────────────────────────────────
@dataclass
class Totals:
    gross_sales: Decimal = D0
    disc_total: Decimal = D0
    gst: Decimal = D0
    net_sales: Decimal = D0
    cogs: Decimal = D0
    invoices: int = 0
    items_sold: Decimal = D0
    returns_value: Decimal = D0
    returns_count: int = 0
    cash_collected: Decimal = D0
    credit_sales: Decimal = D0
    cash_in: Decimal = D0
    cash_out: Decimal = D0
    till_variance: Decimal = D0
    tills_closed: int = 0
    named_customers: int = 0
    trading_hours: Decimal = D0
    days: int = 0

    @property
    def gross_profit(self) -> Decimal:
        return self.net_sales - self.cogs

    @property
    def margin_percent(self) -> Decimal:
        return _money(self.gross_profit / self.net_sales * 100) if self.net_sales else D0

    @property
    def avg_basket(self) -> Decimal:
        return _money(self.net_sales / self.invoices) if self.invoices else D0

    @property
    def bills_per_hour(self) -> Decimal:
        return _money(Decimal(self.invoices) / self.trading_hours) if self.trading_hours else D0

    @property
    def net_cash(self) -> Decimal:
        return self.cash_collected + self.cash_in - self.cash_out


_SUM_FIELDS = (
    "gross_sales", "disc_total", "gst", "net_sales", "cogs", "items_sold",
    "returns_value", "cash_collected", "credit_sales", "cash_in", "cash_out", "till_variance",
)


async def totals_for(period: Period, branch_id: str | None = None) -> Totals:
    qs = BranchDailyStat.filter(day__gte=period.start, day__lte=period.end)
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    rows = await qs

    t = Totals(days=len({r.day for r in rows}))
    for r in rows:
        for f in _SUM_FIELDS:
            setattr(t, f, getattr(t, f) + (getattr(r, f) or D0))
        t.invoices += r.invoices
        t.returns_count += r.returns_count
        t.tills_closed += r.tills_closed
        t.named_customers += r.named_customers
        # Measured trading hours, not an assumed shift length — that is what makes bills-per-hour
        # a real figure rather than a division by a guess.
        if r.first_sale_at and r.last_sale_at:
            hours = Decimal((r.last_sale_at - r.first_sale_at).total_seconds()) / Decimal(3600)
            t.trading_hours += max(hours, Decimal("0.5"))
    return t


async def latest_stock_value() -> Decimal:
    """Branch stock is a right-now figure carried only on each branch's most recent reported day,
    so summing a date range would multiply it by the number of days in the range."""
    total = D0
    for branch in await Branch.all():
        row = await BranchDailyStat.filter(branch=branch).order_by("-day").first()
        if row:
            total += row.stock_value or D0
    return total


async def snapshot_as_of() -> datetime | None:
    """The oldest last-seen across reporting branches — the dashboard is only as fresh as its
    stalest contributor, and claiming the freshest would overstate it."""
    seen = [b.last_seen_at for b in await Branch.all() if b.last_seen_at]
    return min(seen) if seen else None


# ── live (cloud-owned) folds ────────────────────────────────────────────────
async def godown_stock_value() -> Decimal:
    rows = (
        await StockMovement.all().annotate(total=Sum("qty"))
        .group_by("product_id").values("product_id", "total")
    )
    if not rows:
        return D0
    prices = {str(p.id): p.price for p in await Product.filter(id__in=[r["product_id"] for r in rows])}
    return sum((Decimal(str(r["total"] or 0)) * prices.get(r["product_id"], D0) for r in rows), D0)


async def godown_balances() -> list[dict]:
    rows = (
        await StockMovement.all().annotate(total=Sum("qty"))
        .group_by("product_id").values("product_id", "total")
    )
    products = {str(p.id): p for p in await Product.filter(id__in=[r["product_id"] for r in rows])}
    out = []
    for r in rows:
        p = products.get(r["product_id"])
        out.append({
            "productId": r["product_id"],
            "name": p.name if p else r["product_id"],
            "sku": p.sku if p else None,
            "qty": Decimal(str(r["total"] or 0)),
        })
    return out


async def warehouse_status() -> dict:
    transfers = await Transfer.all().prefetch_related("lines")
    in_flight = [t for t in transfers if t.status in ("dispatched", "in_transit")]
    disputes = [t for t in transfers if t.dispute_open]
    awaiting_pick = [t for t in transfers if t.status == "approved"]
    pending_reqs = await Requisition.filter(status="pending").count()
    return {
        "stockValue": await godown_stock_value(),
        "inFlight": len(in_flight),
        "awaitingPick": len(awaiting_pick),
        "disputesOpen": len(disputes),
        "pendingRequisitions": pending_reqs,
        "transfers": transfers,
    }


async def purchase_status() -> dict:
    grns = await GRN.all().prefetch_related("lines")
    received_value = D0
    for g in grns:
        for l in g.lines:
            received_value += (l.qty or D0) * (l.unit_price or D0)
    return {
        "grnCount": len(grns),
        "receivedValue": received_value,
        "unapproved": len([g for g in grns if not g.approved]),
        "pendingRequisitions": await Requisition.filter(status="pending").count(),
        "approvedRequisitions": await Requisition.filter(status="approved").count(),
        "rejectedRequisitions": await Requisition.filter(status="rejected").count(),
    }


async def supplier_performance() -> list[dict]:
    """What the godown's own receiving history says about each supplier: how much has come in,
    over how many receipts, and how recently.

    Deliberately not "on-time delivery %" or a quality score — nothing in the MVP records a
    promised date or a rejection reason, so those would be invented numbers wearing a KPI label.
    """
    suppliers = {str(s.id): s for s in await Supplier.all()}
    grns = await GRN.all().prefetch_related("lines")
    agg: dict[str, dict] = {}
    for g in grns:
        sid = str(g.supplier_id)
        row = agg.setdefault(sid, {
            "supplierId": sid,
            "name": suppliers[sid].name if sid in suppliers else sid,
            "code": suppliers[sid].code if sid in suppliers else None,
            "receipts": 0, "value": D0, "units": D0, "bonusUnits": D0, "lastReceipt": None,
        })
        row["receipts"] += 1
        for l in g.lines:
            row["value"] += (l.qty or D0) * (l.unit_price or D0)
            row["units"] += l.qty or D0
            row["bonusUnits"] += l.bonus_qty or D0
        if row["lastReceipt"] is None or g.at > row["lastReceipt"]:
            row["lastReceipt"] = g.at
    for sid, s in suppliers.items():
        agg.setdefault(sid, {
            "supplierId": sid, "name": s.name, "code": s.code,
            "receipts": 0, "value": D0, "units": D0, "bonusUnits": D0, "lastReceipt": None,
        })
    return sorted(agg.values(), key=lambda r: r["value"], reverse=True)


# ── breakdowns ──────────────────────────────────────────────────────────────
async def top_products(period: Period, limit: int = 20) -> list[dict]:
    rows = await BranchProductStat.filter(day__gte=period.start, day__lte=period.end)
    agg: dict[str, dict] = {}
    for r in rows:
        row = agg.setdefault(r.product_sku, {
            "sku": r.product_sku, "name": r.product_name, "department": r.department,
            "category": r.category, "brand": r.brand, "qty": D0, "netSales": D0, "cogs": D0,
        })
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
    for row in agg.values():
        row["grossProfit"] = row["netSales"] - row["cogs"]
    return sorted(agg.values(), key=lambda r: r["netSales"], reverse=True)[:limit]


async def top_by(period: Period, attr: str, limit: int = 15) -> list[dict]:
    """Top categories / departments / brands — the same fold, grouped by a different column."""
    rows = await BranchProductStat.filter(day__gte=period.start, day__lte=period.end)
    agg: dict[str, dict] = {}
    for r in rows:
        key = getattr(r, attr) or "Unclassified"
        row = agg.setdefault(key, {"name": key, "qty": D0, "netSales": D0, "cogs": D0, "products": set()})
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
        row["products"].add(r.product_sku)
    out = []
    for row in agg.values():
        out.append({
            "name": row["name"], "qty": row["qty"], "netSales": row["netSales"],
            "grossProfit": row["netSales"] - row["cogs"], "products": len(row["products"]),
        })
    return sorted(out, key=lambda r: r["netSales"], reverse=True)[:limit]


async def cashier_ranking(period: Period, limit: int = 20) -> list[dict]:
    rows = await BranchCashierStat.filter(day__gte=period.start, day__lte=period.end)
    branches = await _branch_codes()
    agg: dict[str, dict] = {}
    for r in rows:
        row = agg.setdefault(r.cashier_name, {
            "name": r.cashier_name, "invoices": 0, "netSales": D0, "days": 0, "branchSet": set(),
            "qty": D0, "itemNetSales": D0, "cogs": D0, "items": set(),
        })
        row["invoices"] += r.invoices
        row["netSales"] += r.net_sales or D0
        row["days"] += 1
        row["branchSet"].add(branches.get(str(r.branch_id), "?"))
    # What each person's bills were made of, where the branch sends it: items, units, and the profit on them.
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end):
        row = agg.get(r.cashier_name)
        if row is None:
            continue
        row["qty"] += r.qty or D0
        row["itemNetSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
        row["items"].add(r.product_sku)
    total = sum((row["netSales"] for row in agg.values()), D0)
    for row in agg.values():
        row["avgBasket"] = _money(row["netSales"] / row["invoices"]) if row["invoices"] else D0
        row["share"] = _money(row["netSales"] / total * 100) if total else D0
        row["branches"] = ", ".join(sorted(row.pop("branchSet")))
        row["distinctItems"] = len(row.pop("items"))
        row["grossProfit"] = row["itemNetSales"] - row["cogs"] if row["itemNetSales"] else None
        row["marginPercent"] = _money(row["grossProfit"] / row["itemNetSales"] * 100) if row["itemNetSales"] else None
    return sorted(agg.values(), key=lambda r: r["netSales"], reverse=True)[:limit]


async def duty_by_day(period: Period) -> dict[tuple[str, date], dict]:
    """Per branch-day: how many people were put on a counter, and for how long."""
    out: dict[tuple[str, date], dict] = {}
    for r in await BranchStaffDuty.filter(day__gte=period.start, day__lte=period.end):
        row = out.setdefault((str(r.branch_id), r.day), {"people": set(), "minutes": 0, "counters": set()})
        row["people"].add(r.cashier_name)
        row["minutes"] += r.minutes
        if r.counter_name:
            row["counters"].add(r.counter_name)
    return out


async def duty_people(period: Period, day_iso: str | None = None) -> list[dict]:
    """Every spell on a counter in the period — who, where, how long."""
    qs = BranchStaffDuty.filter(day__gte=period.start, day__lte=period.end)
    if day_iso:
        try:
            qs = qs.filter(day=date.fromisoformat(day_iso))
        except ValueError:
            return []
    branches = await _branch_codes()
    rows: dict[tuple, dict] = {}
    for r in await qs:
        key = (r.day, r.cashier_name, r.counter_name)
        row = rows.setdefault(key, {
            "day": r.day.isoformat(), "name": r.cashier_name, "counter": r.counter_name or "—",
            "branch": branches.get(str(r.branch_id), "?"), "spells": 0, "minutes": 0,
        })
        row["spells"] += r.spells
        row["minutes"] += r.minutes
    for row in rows.values():
        row["hours"] = _money(Decimal(row["minutes"]) / 60)
    return sorted(rows.values(), key=lambda r: (r["day"], -r["minutes"]), reverse=True)


async def staffing(period: Period) -> list[dict]:
    """Per branch per day: how many people were put on a counter, how long they stood there, and how
    many of them actually rang a sale.

    Traded is measured from sales and always has been. On the floor is now a record of its own, kept by
    the branch when somebody is put on a counter — the only way a person who was there all morning and
    sold nothing can be counted at all. A branch that has not set counters up reports no duty, and the
    column says so rather than guessing.
    """
    rows = await BranchDailyStat.filter(day__gte=period.start, day__lte=period.end)
    branches = {str(b.id): b for b in await Branch.all()}
    duty = await duty_by_day(period)
    out = []
    for r in rows:
        b = branches.get(str(r.branch_id))
        d = duty.get((str(r.branch_id), r.day))
        traded = r.staff_on_duty
        assigned = len(d["people"]) if d else None
        out.append({
            "day": r.day.isoformat(), "branch": b.name if b else None, "code": b.code if b else None,
            "onDuty": assigned, "staffOnDuty": traded, "traded": traded,
            "hours": _money(Decimal(d["minutes"]) / 60) if d else None,
            "counters": len(d["counters"]) if d else None,
            # Somebody on a counter who never rang a sale — the person the old figure could not see.
            "onDutyNoSale": max(assigned - traded, 0) if assigned is not None else None,
            "invoices": r.invoices, "netSales": r.net_sales or D0,
        })
    return sorted(out, key=lambda r: r["day"], reverse=True)


async def branch_by_code(code: str) -> Branch | None:
    """The branch a drill-down was asked for, or None. A code off the query string may be anything,
    including longer than the column, which is rejected before the lookup runs — and either way there
    is no such branch."""
    try:
        return await Branch.get_or_none(code=code.upper())
    except ValidationError:
        return None


async def branch_comparison(period: Period) -> list[dict]:
    branches = await Branch.all()
    out = []
    for b in branches:
        t = await totals_for(period, str(b.id))
        latest = await BranchDailyStat.filter(branch=b).order_by("-day").first()
        out.append({
            "branchId": str(b.id), "code": b.code, "name": b.name, "status": b.status,
            "netSales": t.net_sales, "invoices": t.invoices, "grossProfit": t.gross_profit,
            "avgBasket": t.avg_basket, "cashCollected": t.cash_collected,
            "stockValue": (latest.stock_value if latest else D0) or D0,
            "staffOnDuty": latest.staff_on_duty if latest else 0,
            "lastSeenAt": b.last_seen_at,
            "reporting": latest is not None,
        })
    return sorted(out, key=lambda r: r["netSales"], reverse=True)


async def daily_series(period: Period, field_name: str = "net_sales") -> list[dict]:
    rows = await BranchDailyStat.filter(day__gte=period.start, day__lte=period.end)
    agg: dict[date, Decimal] = {}
    for r in rows:
        agg[r.day] = agg.get(r.day, D0) + (getattr(r, field_name) or D0)
    return [{"day": d.isoformat(), "value": v} for d, v in sorted(agg.items())]


async def stock_alerts(kind: str | None = None, limit: int = 200) -> tuple[list[dict], dict[str, int]]:
    qs = BranchStockAlert.all()
    if kind:
        qs = qs.filter(kind=kind)
    rows = await qs.limit(limit)
    branches = {str(b.id): b for b in await Branch.all()}
    totals: dict[str, int] = {}
    for r in await BranchStockAlert.all():
        totals[r.kind] = max(totals.get(r.kind, 0), r.total_of_kind)
    items = [{
        "branch": branches[str(r.branch_id)].name if str(r.branch_id) in branches else None,
        "kind": r.kind, "sku": r.product_sku, "name": r.product_name,
        "qty": r.qty, "expiry": r.expiry, "detail": r.detail,
    } for r in rows]
    return items, totals


async def godown_alerts() -> dict:
    """The godown's own exceptions. Branch alerts arrive via snapshot; these are computed live,
    so the two halves of "what is wrong with stock" have different freshness and say so."""
    balances = await godown_balances()
    out_of_stock = [b for b in balances if b["qty"] <= D0]
    low_stock = sorted([b for b in balances if D0 < b["qty"] < LOW_STOCK_THRESHOLD], key=lambda r: r["qty"])
    return {"outOfStock": out_of_stock, "lowStock": low_stock}


# ── business health ─────────────────────────────────────────────────────────
@dataclass
class HealthComponent:
    key: str
    label: str
    score: int
    weight: int
    detail: str


@dataclass
class Health:
    score: int
    band: str
    components: list[HealthComponent] = field(default_factory=list)


def _band(score: int) -> str:
    if score >= 80:
        return "Healthy"
    if score >= 60:
        return "Watch"
    return "Needs attention"


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


async def business_health(period: Period) -> Health:
    """A transparent composite, not a black box — every component and its weight is returned so
    the drill-down can show exactly how the number was reached. Nothing here is predictive or
    AI-derived; those need the AI engine, which does not exist.
    """
    now = await totals_for(period)
    prev = await totals_for(Period(period.id, period.label, period.prev_start, period.prev_end,
                                   period.prev_start, period.prev_end, period.compare_label))

    components: list[HealthComponent] = []

    # Sales trend: flat scores 60, +25% or better scores 100, −25% or worse scores 0.
    if prev.net_sales:
        change = float((now.net_sales - prev.net_sales) / prev.net_sales)
        trend_score = _clamp(60 + change * 160)
        detail = f"{change * 100:+.1f}% vs {period.compare_label}"
    else:
        trend_score, detail = 60, "no comparable prior period"
    components.append(HealthComponent("sales-trend", "Sales trend", trend_score, 30, detail))

    # Margin: 25% or better is full marks, 0% is zero.
    margin = float(now.margin_percent)
    components.append(HealthComponent(
        "margin", "Gross margin", _clamp(margin / 25 * 100), 25, f"{margin:.1f}% of net sales"))

    # Till accuracy: variance as a share of cash taken. 0 is perfect, 1% or worse is zero.
    if now.cash_collected:
        drift = abs(float(now.till_variance / now.cash_collected))
        till_score = _clamp(100 - drift * 10000)
        till_detail = f"Rs {abs(now.till_variance):,.0f} across {now.tills_closed} closes"
    else:
        till_score, till_detail = 100, "no cash taken in this period"
    components.append(HealthComponent("till-accuracy", "Till accuracy", till_score, 15, till_detail))

    # Stock health: out-of-stock lines against the godown's own range.
    alerts = await godown_alerts()
    balances = await godown_balances()
    lines = len(balances) or 1
    oos_ratio = len(alerts["outOfStock"]) / lines
    components.append(HealthComponent(
        "stock", "Stock availability", _clamp(100 - oos_ratio * 300), 15,
        f"{len(alerts['outOfStock'])} of {lines} godown lines out of stock"))

    # Supply chain: open disputes and requisitions waiting on a decision.
    wh = await warehouse_status()
    friction = wh["disputesOpen"] * 2 + wh["pendingRequisitions"]
    components.append(HealthComponent(
        "supply", "Supply chain", _clamp(100 - friction * 12), 10,
        f"{wh['disputesOpen']} open dispute(s), {wh['pendingRequisitions']} requisition(s) waiting"))

    # Reporting: branches that have actually reported at all.
    branches = await Branch.all()
    active = [b for b in branches if b.status == "active"]
    reporting = [b for b in active if b.last_seen_at]
    ratio = (len(reporting) / len(active)) if active else 1
    components.append(HealthComponent(
        "reporting", "Branch reporting", _clamp(ratio * 100), 5,
        f"{len(reporting)} of {len(active)} active branches have reported"))

    total_weight = sum(c.weight for c in components) or 1
    score = _clamp(sum(c.score * c.weight for c in components) / total_weight)
    return Health(score=score, band=_band(score), components=components)


async def live_alerts() -> list[dict]:
    """Everything that wants a human's attention, newest concern first. Each carries where it came
    from so the reader knows whether it is live or as-reported."""
    out: list[dict] = []

    wh = await warehouse_status()
    for t in wh["transfers"]:
        if t.dispute_open:
            out.append({
                "severity": "high", "kind": "dispute", "source": "live",
                "title": f"{t.transfer_number} — short-receipt dispute",
                "detail": t.dispute_note or "Received less than dispatched.",
            })
    if wh["pendingRequisitions"]:
        out.append({
            "severity": "medium", "kind": "requisition", "source": "live",
            "title": f"{wh['pendingRequisitions']} requisition(s) waiting on a decision",
            "detail": "Branches are waiting on the godown to approve or reject.",
        })

    godown = await godown_alerts()
    if godown["outOfStock"]:
        out.append({
            "severity": "high", "kind": "stock", "source": "live",
            "title": f"{len(godown['outOfStock'])} godown line(s) out of stock",
            "detail": ", ".join(b["name"] for b in godown["outOfStock"][:3]),
        })
    if godown["lowStock"]:
        out.append({
            "severity": "medium", "kind": "stock", "source": "live",
            "title": f"{len(godown['lowStock'])} godown line(s) below {LOW_STOCK_THRESHOLD}",
            "detail": ", ".join(f"{b['name']} ({b['qty']})" for b in godown["lowStock"][:3]),
        })

    _, totals = await stock_alerts()
    for kind, label, severity in (
        ("expired", "expired item(s) still on branch shelves", "high"),
        ("out-of-stock", "branch line(s) out of stock", "high"),
        ("near-expiry", "branch item(s) near expiry", "medium"),
        ("low-stock", "branch line(s) running low", "low"),
    ):
        if totals.get(kind):
            out.append({
                "severity": severity, "kind": "stock", "source": "branch-snapshot",
                "title": f"{totals[kind]} {label}",
                "detail": "As last reported by the branch.",
            })

    for b in await Branch.all():
        if b.status == "active" and not b.last_seen_at:
            out.append({
                "severity": "medium", "kind": "sync", "source": "live",
                "title": f"{b.name} has never reported",
                "detail": "Registered, but no data has arrived from this branch yet.",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda a: order.get(a["severity"], 3))


# ── the grain a drill-down lands on ─────────────────────────────────────────
# Everything below answers "why is that number what it is". A total an executive cannot get
# behind is a total they stop trusting. Each returns plain dicts so the controller can shape a
# table without needing a schema per breakdown.

async def daily_breakdown(period: Period, branch_id: str | None = None) -> list[dict]:
    """Day by day — the level between a period total and a single transaction."""
    qs = BranchDailyStat.filter(day__gte=period.start, day__lte=period.end)
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    agg: dict[date, dict] = {}
    for r in await qs:
        row = agg.setdefault(r.day, {
            "day": r.day.isoformat(), "netSales": D0, "grossSales": D0, "cogs": D0,
            "invoices": 0, "itemsSold": D0, "discTotal": D0, "returnsValue": D0,
            "cashCollected": D0, "creditSales": D0, "tillVariance": D0, "staffOnDuty": 0,
        })
        row["netSales"] += r.net_sales or D0
        row["grossSales"] += r.gross_sales or D0
        row["cogs"] += r.cogs or D0
        row["invoices"] += r.invoices
        row["itemsSold"] += r.items_sold or D0
        row["discTotal"] += r.disc_total or D0
        row["returnsValue"] += r.returns_value or D0
        row["cashCollected"] += r.cash_collected or D0
        row["creditSales"] += r.credit_sales or D0
        row["tillVariance"] += r.till_variance or D0
        row["staffOnDuty"] = max(row["staffOnDuty"], r.staff_on_duty)
    out = []
    for row in agg.values():
        row["grossProfit"] = row["netSales"] - row["cogs"]
        row["avgBasket"] = _money(row["netSales"] / row["invoices"]) if row["invoices"] else D0
        out.append(row)
    return sorted(out, key=lambda r: r["day"], reverse=True)


async def hourly_profile(period: Period) -> list[dict]:
    """Invoices and sales by hour of the local trading day. This is what bills-per-hour is
    actually about — where the queue forms, and therefore where staff need to be."""
    rows = await BranchHourlyStat.filter(day__gte=period.start, day__lte=period.end)
    agg: dict[int, dict] = {}
    days: dict[int, set] = {}
    for r in rows:
        row = agg.setdefault(r.hour, {"hour": r.hour, "invoices": 0, "netSales": D0})
        row["invoices"] += r.invoices
        row["netSales"] += r.net_sales or D0
        days.setdefault(r.hour, set()).add(r.day)
    out = []
    for hour, row in sorted(agg.items()):
        n = len(days.get(hour, ())) or 1
        out.append({
            **row,
            "label": "%02d:00-%02d:00" % (hour, (hour + 1) % 24),
            "daysTraded": n,
            "billsPerHour": _money(Decimal(row["invoices"]) / n),
        })
    return out


async def till_closes(period: Period, cashier: str | None = None) -> list[dict]:
    qs = BranchTillClose.filter(day__gte=period.start, day__lte=period.end)
    if cashier:
        qs = qs.filter(cashier_name=cashier)
    return [{
        "sessionNumber": r.session_number, "day": r.day.isoformat(), "cashier": r.cashier_name,
        "openedAt": r.opened_at, "closedAt": r.closed_at, "openingFloat": r.opening_float,
        "netCash": r.net_cash, "countedCash": r.counted_cash, "variance": r.variance,
    } for r in await qs.order_by("-closed_at")]


async def tender_mix(period: Period) -> list[dict]:
    """Cash, card and credit have completely different consequences for working capital, so one
    "sales" figure hides the question that actually matters."""
    rows = await BranchTenderStat.filter(day__gte=period.start, day__lte=period.end)
    agg: dict[str, dict] = {}
    for r in rows:
        row = agg.setdefault(r.code, {"code": r.code, "name": r.name, "uses": 0, "amount": D0})
        row["uses"] += r.uses
        row["amount"] += r.amount or D0
    total = sum((r["amount"] for r in agg.values()), D0)
    for row in agg.values():
        row["share"] = _money(row["amount"] / total * 100) if total else D0
    return sorted(agg.values(), key=lambda r: r["amount"], reverse=True)


async def discount_overrides(period: Period, approver: str | None = None) -> list[dict]:
    """Margin given away on a manager's signature, by name and invoice."""
    qs = BranchDiscountOverride.filter(day__gte=period.start, day__lte=period.end)
    if approver:
        qs = qs.filter(approved_by=approver)
    return [{
        "invoiceNumber": r.invoice_number, "at": r.at, "day": r.day.isoformat(),
        "cashier": r.cashier_name, "approvedBy": r.approved_by,
        "gross": r.gross, "discTotal": r.disc_total, "netValue": r.net_value,
        "discPercent": _money(r.disc_total / r.gross * 100) if r.gross else D0,
    } for r in await qs.order_by("-at")]


async def returns_detail(period: Period) -> list[dict]:
    qs = BranchReturn.filter(day__gte=period.start, day__lte=period.end)
    return [{
        "at": r.at, "day": r.day.isoformat(), "againstInvoice": r.against_invoice,
        "cashier": r.cashier_name, "productName": r.product_name, "productSku": r.product_sku,
        "qty": r.qty, "refundTotal": r.refund_total,
    } for r in await qs.order_by("-at")]


async def credit_customers() -> list[dict]:
    return [{
        "code": r.code, "name": r.name, "phone": r.phone, "tier": r.tier,
        "creditLimit": r.credit_limit, "creditBalance": r.credit_balance,
        "headroom": (r.credit_limit or D0) - (r.credit_balance or D0),
        "usedPercent": _money(r.credit_balance / r.credit_limit * 100) if r.credit_limit else D0,
    } for r in await BranchCreditCustomer.all()]


# ── focused views: one category, one product, one cashier, one day, one supplier ──
async def products_where(period: Period, attr: str, value: str, limit: int = 100) -> list[dict]:
    """The products inside one category / brand / department — what a Top Categories row opens."""
    rows = await BranchProductStat.filter(day__gte=period.start, day__lte=period.end)
    wanted = (value or "").strip().lower()
    agg: dict[str, dict] = {}
    for r in rows:
        actual = (getattr(r, attr) or "Unclassified").strip().lower()
        if actual != wanted:
            continue
        row = agg.setdefault(r.product_sku, {
            "sku": r.product_sku, "name": r.product_name, "category": r.category,
            "brand": r.brand, "department": r.department, "qty": D0, "netSales": D0, "cogs": D0,
        })
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
    for row in agg.values():
        row["grossProfit"] = row["netSales"] - row["cogs"]
        row["marginPercent"] = _money(row["grossProfit"] / row["netSales"] * 100) if row["netSales"] else D0
    return sorted(agg.values(), key=lambda r: r["netSales"], reverse=True)[:limit]


async def product_days(period: Period, sku: str) -> tuple[list[dict], dict]:
    """One product, day by day, plus its period totals."""
    rows = await BranchProductStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku)
    days: dict[date, dict] = {}
    totals = {"qty": D0, "netSales": D0, "cogs": D0, "name": sku, "category": None, "brand": None}
    for r in rows:
        totals["name"] = r.product_name
        totals["category"] = r.category
        totals["brand"] = r.brand
        d = days.setdefault(r.day, {"day": r.day.isoformat(), "qty": D0, "netSales": D0, "cogs": D0})
        d["qty"] += r.qty or D0
        d["netSales"] += r.net_sales or D0
        d["cogs"] += r.cogs or D0
        totals["qty"] += r.qty or D0
        totals["netSales"] += r.net_sales or D0
        totals["cogs"] += r.cogs or D0
    for d in days.values():
        d["grossProfit"] = d["netSales"] - d["cogs"]
    totals["grossProfit"] = totals["netSales"] - totals["cogs"]
    totals["marginPercent"] = _money(totals["grossProfit"] / totals["netSales"] * 100) if totals["netSales"] else D0
    return sorted(days.values(), key=lambda r: r["day"]), totals


async def cashier_days(period: Period, name: str) -> tuple[list[dict], dict]:
    rows = await BranchCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name)
    days = []
    totals = {"invoices": 0, "netSales": D0, "days": 0}
    for r in sorted(rows, key=lambda x: x.day):
        days.append({
            "day": r.day.isoformat(), "invoices": r.invoices, "netSales": r.net_sales or D0,
            "avgBasket": _money((r.net_sales or D0) / r.invoices) if r.invoices else D0,
        })
        totals["invoices"] += r.invoices
        totals["netSales"] += r.net_sales or D0
        totals["days"] += 1
    totals["avgBasket"] = _money(totals["netSales"] / totals["invoices"]) if totals["invoices"] else D0
    by_day: dict[str, dict] = {}
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name):
        d = by_day.setdefault(r.day.isoformat(), {"qty": D0, "itemNet": D0, "cogs": D0, "items": set()})
        d["qty"] += r.qty or D0
        d["itemNet"] += r.net_sales or D0
        d["cogs"] += r.cogs or D0
        d["items"].add(r.product_sku)
    for row in days:
        d = by_day.get(row["day"])
        row["qty"] = d["qty"] if d else None
        row["items"] = len(d["items"]) if d else None
        row["grossProfit"] = d["itemNet"] - d["cogs"] if d else None
    return days, totals


async def product_day_people(period: Period, sku: str) -> dict[str, str]:
    """For each day an item sold, who sold it and how many: "Hina Malik 12 · Ali Raza 4"."""
    per: dict[str, dict[str, Decimal]] = {}
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku):
        people = per.setdefault(r.day.isoformat(), {})
        people[r.cashier_name] = people.get(r.cashier_name, D0) + (r.qty or D0)
    out = {}
    for day, people in per.items():
        ranked = sorted(people.items(), key=lambda kv: kv[1], reverse=True)
        out[day] = " · ".join(f"{n} ({q.normalize():f})" for n, q in ranked)
    return out


async def day_detail(day_iso: str) -> dict:
    """Everything about one trading day — what a row in the daily breakdown opens into."""
    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        return {}
    one = Period("day", day_iso, day, day, day, day, "the day before")
    return {
        "day": day_iso,
        "hours": await hourly_profile(one),
        "cashiers": await cashier_ranking(one, limit=20),
        "products": await top_products(one, limit=20),
        "tenders": await tender_mix(one),
        "closes": await till_closes(one),
        "totals": await totals_for(one),
    }


async def supplier_receipts(supplier_id: str) -> tuple[list[dict], dict]:
    """Every GRN line the godown booked in from one supplier — live, from Cloud's own records."""
    # The id arrives off the query string and may be anything: one the column cannot even hold is
    # rejected before the lookup runs, which turned a hand-edited link into a 500 rather than the
    # empty view an id that simply doesn't exist already gives.
    try:
        supplier = await Supplier.get_or_none(id=supplier_id)
        grns = await GRN.filter(supplier_id=supplier_id).prefetch_related("lines")
    except ValidationError:
        supplier, grns = None, []
    product_ids = {str(l.product_id) for g in grns for l in g.lines}
    products = {str(p.id): p for p in await Product.filter(id__in=list(product_ids))} if product_ids else {}
    bins = {str(b.id): b for b in await Bin.all()}
    rows = []
    for g in grns:
        for l in g.lines:
            prod = products.get(str(l.product_id))
            rows.append({
                "grnNumber": g.grn_number, "at": g.at, "partyInvNo": g.party_inv_no,
                "bin": bins[str(g.bin_id)].label if str(g.bin_id) in bins else str(g.bin_id),
                "productName": prod.name if prod else str(l.product_id),
                "productSku": prod.sku if prod else None,
                "qty": l.qty, "bonusQty": l.bonus_qty, "unitPrice": l.unit_price,
                "lineValue": (l.qty or D0) * (l.unit_price or D0),
            })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows, {
        "name": supplier.name if supplier else supplier_id,
        "code": supplier.code if supplier else None,
        "receipts": len(grns),
        "value": sum((r["lineValue"] for r in rows), D0),
    }


async def godown_stock_rows(limit: int = 300) -> list[dict]:
    """Godown stock line by line, valued — the live half of Inventory Value."""
    balances = await godown_balances()
    products = {str(p.id): p for p in await Product.all()}
    rows = []
    for b in balances:
        prod = products.get(b["productId"])
        price = prod.price if prod else D0
        rows.append({
            "name": b["name"], "sku": b["sku"], "qty": b["qty"],
            "price": price, "value": b["qty"] * price,
        })
    return sorted(rows, key=lambda r: r["value"], reverse=True)[:limit]


# ── who sold what: an item opens onto its people, a person onto their items ──────────────────────
async def _branch_codes() -> dict[str, str]:
    return {str(b.id): b.code for b in await Branch.all()}


async def _item_info(period: Period) -> dict[str, dict]:
    """Names and taxonomy by sku. The per-person rows carry only the sku; the item rows carry the rest."""
    info: dict[str, dict] = {}
    for r in await BranchProductStat.filter(day__gte=period.start, day__lte=period.end):
        info[r.product_sku] = {"name": r.product_name, "category": r.category or "Unclassified", "brand": r.brand or "Unclassified",
                               "department": r.department or "Unclassified"}
    return info


def _profit(row: dict) -> dict:
    row["grossProfit"] = row["netSales"] - row["cogs"]
    row["marginPercent"] = _money(row["grossProfit"] / row["netSales"] * 100) if row["netSales"] else D0
    return row


async def person_items_known(period: Period) -> bool:
    """Whether any branch has sent who-sold-what for this period (an older branch version doesn't)."""
    return await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end).exists()


async def product_people(period: Period, sku: str) -> list[dict]:
    """Everyone who sold one item: how many, for how much, over how many bills, and their share of the item."""
    branches = await _branch_codes()
    agg: dict[str, dict] = {}
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku):
        row = agg.setdefault(r.cashier_name, {"name": r.cashier_name, "sku": sku, "invoices": 0, "qty": D0, "netSales": D0, "cogs": D0,
                                               "daySet": set(), "branchSet": set()})
        row["invoices"] += r.invoices
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
        row["daySet"].add(r.day)
        row["branchSet"].add(branches.get(str(r.branch_id), "?"))
    total_net = sum((row["netSales"] for row in agg.values()), D0)
    total_qty = sum((row["qty"] for row in agg.values()), D0)
    for row in agg.values():
        _profit(row)
        row["days"] = len(row.pop("daySet"))
        row["branches"] = ", ".join(sorted(row.pop("branchSet")))
        row["share"] = _money(row["netSales"] / total_net * 100) if total_net else D0
        row["qtyShare"] = _money(row["qty"] / total_qty * 100) if total_qty else D0
    return sorted(agg.values(), key=lambda r: (r["netSales"], r["qty"]), reverse=True)


async def product_branches(period: Period, sku: str) -> list[dict]:
    branches = await _branch_codes()
    agg: dict[str, dict] = {}
    for r in await BranchProductStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku):
        code = branches.get(str(r.branch_id), "?")
        row = agg.setdefault(code, {"code": code, "qty": D0, "netSales": D0, "cogs": D0})
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
    return sorted((_profit(r) for r in agg.values()), key=lambda r: r["netSales"], reverse=True)


async def person_products(period: Period, name: str) -> list[dict]:
    """Everything one person sold: each item with units, value, profit, bills, days, the share of this person's own
    sales it makes up, and the share of that item's sales this person made."""
    info = await _item_info(period)
    mine = await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name)
    skus = {r.product_sku for r in mine}
    item_totals: dict[str, Decimal] = {}
    if skus:
        for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku__in=list(skus)):
            item_totals[r.product_sku] = item_totals.get(r.product_sku, D0) + (r.net_sales or D0)
    agg: dict[str, dict] = {}
    for r in mine:
        meta = info.get(r.product_sku, {})
        row = agg.setdefault(r.product_sku, {"sku": r.product_sku, "name": meta.get("name", r.product_sku), "person": name,
                                             "category": meta.get("category", "Unclassified"), "brand": meta.get("brand", "Unclassified"),
                                             "invoices": 0, "qty": D0, "netSales": D0, "cogs": D0, "daySet": set()})
        row["invoices"] += r.invoices
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
        row["daySet"].add(r.day)
    person_total = sum((row["netSales"] for row in agg.values()), D0)
    for sku, row in agg.items():
        _profit(row)
        row["days"] = len(row.pop("daySet"))
        row["shareOfPerson"] = _money(row["netSales"] / person_total * 100) if person_total else D0
        row["shareOfItem"] = _money(row["netSales"] / item_totals[sku] * 100) if item_totals.get(sku) else D0
    return sorted(agg.values(), key=lambda r: (r["netSales"], r["qty"]), reverse=True)


async def person_categories(period: Period, name: str) -> list[dict]:
    agg: dict[str, dict] = {}
    for item in await person_products(period, name):
        row = agg.setdefault(item["category"], {"name": item["category"], "person": name, "items": 0, "qty": D0, "netSales": D0, "cogs": D0})
        row["items"] += 1
        row["qty"] += item["qty"]
        row["netSales"] += item["netSales"]
        row["cogs"] += item["cogs"]
    total = sum((row["netSales"] for row in agg.values()), D0)
    out = []
    for row in agg.values():
        _profit(row)
        row["share"] = _money(row["netSales"] / total * 100) if total else D0
        out.append(row)
    return sorted(out, key=lambda r: r["netSales"], reverse=True)


async def person_product_days(period: Period, name: str, sku: str) -> tuple[list[dict], dict]:
    """One person and one item, day by day, with where that sits against the item's and the person's whole period."""
    info = (await _item_info(period)).get(sku, {})
    days: dict[date, dict] = {}
    item_net = item_qty = person_net = D0
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku):
        item_net += r.net_sales or D0
        item_qty += r.qty or D0
        if r.cashier_name != name:
            continue
        d = days.setdefault(r.day, {"day": r.day.isoformat(), "invoices": 0, "qty": D0, "netSales": D0, "cogs": D0})
        d["invoices"] += r.invoices
        d["qty"] += r.qty or D0
        d["netSales"] += r.net_sales or D0
        d["cogs"] += r.cogs or D0
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name):
        person_net += r.net_sales or D0
    rows = [_profit(d) for d in sorted(days.values(), key=lambda d: d["day"])]
    totals = _profit({
        "name": info.get("name", sku), "category": info.get("category"), "brand": info.get("brand"),
        "invoices": sum((d["invoices"] for d in rows), 0), "qty": sum((d["qty"] for d in rows), D0),
        "netSales": sum((d["netSales"] for d in rows), D0), "cogs": sum((d["cogs"] for d in rows), D0),
    })
    totals["days"] = len(rows)
    totals["shareOfItem"] = _money(totals["netSales"] / item_net * 100) if item_net else D0
    totals["qtyShareOfItem"] = _money(totals["qty"] / item_qty * 100) if item_qty else D0
    totals["shareOfPerson"] = _money(totals["netSales"] / person_net * 100) if person_net else D0
    return rows, totals


async def people_where(period: Period, attr: str, value: str) -> list[dict]:
    """Who sells a category or a brand, and how much of it each."""
    wanted = (value or "").strip().lower()
    skus = {sku for sku, meta in (await _item_info(period)).items() if meta.get(attr, "Unclassified").strip().lower() == wanted}
    agg: dict[str, dict] = {}
    if skus:
        for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku__in=list(skus)):
            row = agg.setdefault(r.cashier_name, {"name": r.cashier_name, "invoices": 0, "qty": D0, "netSales": D0, "cogs": D0, "itemSet": set()})
            row["invoices"] += r.invoices
            row["qty"] += r.qty or D0
            row["netSales"] += r.net_sales or D0
            row["cogs"] += r.cogs or D0
            row["itemSet"].add(r.product_sku)
    total = sum((row["netSales"] for row in agg.values()), D0)
    for row in agg.values():
        _profit(row)
        row["items"] = len(row.pop("itemSet"))
        row["share"] = _money(row["netSales"] / total * 100) if total else D0
    return sorted(agg.values(), key=lambda r: r["netSales"], reverse=True)


async def top_sellers(period: Period) -> dict[str, dict]:
    """For each item, the person who sold the most of it, their share, and how many people sold it at all."""
    per: dict[str, dict[str, Decimal]] = {}
    for r in await BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end):
        people = per.setdefault(r.product_sku, {})
        people[r.cashier_name] = people.get(r.cashier_name, D0) + (r.net_sales or D0)
    out = {}
    for sku, people in per.items():
        name, amount = max(people.items(), key=lambda kv: kv[1])
        total = sum(people.values(), D0)
        out[sku] = {"name": name, "share": _money(amount / total * 100) if total else D0, "people": len(people)}
    return out


async def people_on_day(day_iso: str) -> list[dict]:
    """Everyone who sold on one day: their bills and takings, and what those bills were made of."""
    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        return []
    agg: dict[str, dict] = {}
    for r in await BranchCashierStat.filter(day=day):
        row = agg.setdefault(r.cashier_name, {"name": r.cashier_name, "invoices": 0, "billTotal": D0, "qty": D0, "netSales": D0, "cogs": D0, "itemSet": set()})
        row["invoices"] += r.invoices
        row["billTotal"] += r.net_sales or D0
    for r in await BranchProductCashierStat.filter(day=day):
        row = agg.setdefault(r.cashier_name, {"name": r.cashier_name, "invoices": 0, "billTotal": D0, "qty": D0, "netSales": D0, "cogs": D0, "itemSet": set()})
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
        row["itemSet"].add(r.product_sku)
    for row in agg.values():
        _profit(row)
        row["items"] = len(row.pop("itemSet"))
        row["avgBasket"] = _money(row["billTotal"] / row["invoices"]) if row["invoices"] else D0
    return sorted(agg.values(), key=lambda r: r["billTotal"], reverse=True)


async def returns_handled(period: Period, name: str) -> list[dict]:
    return [r for r in await returns_detail(period) if r["cashier"] == name]


async def overrides_rung(period: Period, name: str) -> list[dict]:
    return [r for r in await discount_overrides(period) if r["cashier"] == name]

