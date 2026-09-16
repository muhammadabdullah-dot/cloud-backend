"""The KPI catalogue.

Each KPI is one builder function returning a tile, plus one returning its drill-down. Keeping
both next to each other is deliberate: a tile whose headline and whose detail page are computed
in different files is how the two quietly stop agreeing.

The KPI list comes from the Executive Dashboard section of the ERP blueprint (Step #2, "MODULES →
1. Executive Dashboard"). Everything the blueprint asks for that has no data behind it in this
MVP is returned in `notBuilt` rather than silently dropped or faked.
"""
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status

from app.schemas.executive import (
    Crumb,
    DetailSection,
    ChartOut,
    ChartPoint,
    KpiDelta,
    KpiDetailOut,
    KpiListOut,
    KpiOut,
    NotBuiltOut,
    PeriodOut,
    SeriesPoint,
    TableColumn,
)
from app.services import analytics_service as an
from app.services import executive_service as ex

D0 = Decimal("0")

LIVE = "live"
SNAPSHOT = "branch-snapshot"


# ── formatting ──────────────────────────────────────────────────────────────
def money(v: Decimal | None) -> str:
    return f"Rs {(v or D0):,.0f}"


def num(v: Decimal | int | None) -> str:
    if v is None:
        return "0"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def pct(v: Decimal | float | None) -> str:
    return f"{float(v or 0):.1f}%"


def _delta(now: Decimal, before: Decimal, label: str, higher_is_better: bool = True) -> KpiDelta | None:
    if before == D0:
        # No baseline to compare against — showing "+100%" against zero would be noise dressed
        # up as a signal.
        return None
    change = now - before
    percent = float(change / before * 100)
    direction = "up" if change > D0 else "down" if change < D0 else "flat"
    good = None if change == D0 else ((change > D0) == higher_is_better)
    return KpiDelta(value=change, percent=percent, direction=direction, good=good, label=f"vs {label}")


def _money_pct(part: Decimal | None, whole: Decimal | None) -> Decimal:
    return ex._money(Decimal(part) / Decimal(whole) * 100) if part is not None and whole else D0


def _period_out(p: ex.Period) -> PeriodOut:
    return PeriodOut(id=p.id, label=p.label, start=p.start, end=p.end, compareLabel=p.compare_label)


def _prev(p: ex.Period) -> ex.Period:
    return ex.Period(p.id, p.label, p.prev_start, p.prev_end, p.prev_start, p.prev_end, p.compare_label)


# What the blueprint asks for that this MVP has no data for. Each says what module would supply
# it, so the gap reads as sequencing rather than omission.
NOT_BUILT = [
    NotBuiltOut(label="Online Orders", needs="Ecommerce module."),
    NotBuiltOut(label="Delivery Status", needs="Delivery / dispatch-to-customer module."),
    NotBuiltOut(label="AI Recommendations", needs="AI engine."),
    NotBuiltOut(label="Predictive Sales", needs="AI engine — forecasting needs a trained model, not an average."),
]


# ── the grid ────────────────────────────────────────────────────────────────
# KPIs whose tile said nothing a chart doesn't say better, and which therefore no longer appear on
# the grid.
#
# Each of these was a tile that could only ever print the *winner* — "Top product: Eggs", "Best
# cashier: Cashier", "Branch comparison: Head Office". That names first place and hides the shape:
# whether first is miles clear or a nose ahead, and who second and third are. The chart answers
# both, so keeping the tile as well was the same fact printed twice and a grid a third longer than
# it needed to be.
#
# They are removed from the GRID, not from the system. Every one still has a drill-down, and the
# charts link straight into it — the chart is now the front door that the tile used to be.
CHART_BACKED = {
    "top-products", "top-categories", "top-brands",
    "best-cashier", "branch-comparison",
    "abc-analysis", "xyz-analysis",
}


async def list_kpis(period_id: str, *, include_charted: bool = False) -> KpiListOut:
    """The grid. `include_charted` returns the chart-backed KPIs too — `kpi_detail` needs them to
    build a headline for a drill-down that is no longer reachable from a tile."""
    period = ex.resolve_period(period_id)
    prev = _prev(period)
    now = await ex.totals_for(period)
    before = await ex.totals_for(prev)
    as_of = await ex.snapshot_as_of()

    items: list[KpiOut] = []

    def add(**kw) -> None:
        kw.setdefault("source", SNAPSHOT)
        kw.setdefault("asOf", as_of)
        kw.setdefault("ctaLabel", "See the detail")
        items.append(KpiOut(**kw))

    # ── Sales ───────────────────────────────────────────────────────────────
    today = ex.resolve_period("today")
    today_t = await ex.totals_for(today)
    yday_t = await ex.totals_for(_prev(today))
    add(id="sales-today", label="Today's Sales", group="Sales",
        hint="Net value of everything sold today, across every branch that has reported.",
        value=today_t.net_sales, display=money(today_t.net_sales), unit="PKR",
        sub=f"{today_t.invoices} invoice(s)",
        delta=_delta(today_t.net_sales, yday_t.net_sales, "yesterday"))

    for pid, label in (("7d", "This Week"), ("30d", "This Month"), ("ytd", "This Year")):
        p = ex.resolve_period(pid)
        t = await ex.totals_for(p)
        b = await ex.totals_for(_prev(p))
        add(id=f"sales-{pid}", label=label, group="Sales",
            hint=f"Net sales over {p.label.lower()}, compared with {p.compare_label}.",
            value=t.net_sales, display=money(t.net_sales), unit="PKR",
            sub=f"{t.invoices} invoice(s)", delta=_delta(t.net_sales, b.net_sales, p.compare_label))

    add(id="gross-profit", label="Gross Profit", group="Sales",
        hint="Net sales minus the cost of what was sold. Expenses come off in Net Profit, from the books.",
        value=now.gross_profit, display=money(now.gross_profit), unit="PKR",
        sub=f"{pct(now.margin_percent)} margin",
        delta=_delta(now.gross_profit, before.gross_profit, period.compare_label))

    # Net profit comes from the books — head office's and every branch's that has synced — not from the snapshot.
    from app.services import accounts_reports_service as books

    company, _ = await books.resolve_books("ALL")
    statement = await books.income_statement(company, True, period.start, period.end)
    earlier = await books.income_statement(company, True, prev.start, prev.end)
    net_profit = Decimal(statement["netProfit"])
    expenses = Decimal(statement["operatingExpenses"]) + Decimal(statement["financialExpenses"])
    add(id="net-profit", label="Net Profit", group="Sales",
        hint="From the books: net sales, less the cost of what was sold, less every expense posted — head office and every branch whose books have synced.",
        value=net_profit, display=money(net_profit), unit="PKR", source="books", asOf=None,
        sub=f"{money(expenses)} expenses", delta=_delta(net_profit, Decimal(earlier["netProfit"]), period.compare_label),
        severity="bad" if net_profit < D0 else None, ctaLabel="See what's behind it")

    add(id="avg-basket", label="Average Basket", group="Sales",
        hint="Net sales divided by the number of invoices — what a typical customer spends per visit.",
        value=now.avg_basket, display=money(now.avg_basket), unit="PKR",
        sub=f"across {now.invoices} invoice(s)",
        delta=_delta(now.avg_basket, before.avg_basket, period.compare_label))

    add(id="customer-count", label="Customer Count", group="Sales",
        hint="Invoices rung in the period. Named Party customers are counted separately below.",
        value=Decimal(now.invoices), display=num(now.invoices), unit="count",
        sub=f"{now.named_customers} named Party customer(s)",
        delta=_delta(Decimal(now.invoices), Decimal(before.invoices), period.compare_label))

    add(id="bills-per-hour", label="Bills Per Hour", group="Sales",
        hint="Invoices divided by measured trading hours — the gap between each branch's first and last sale, not an assumed shift length.",
        value=now.bills_per_hour, display=f"{float(now.bills_per_hour):.1f}", unit="rate",
        sub=f"{float(now.trading_hours):.0f} trading hour(s)",
        delta=_delta(now.bills_per_hour, before.bills_per_hour, period.compare_label))

    # ── Money ───────────────────────────────────────────────────────────────
    add(id="cash-position", label="Cash Position", group="Money",
        hint="Cash taken, plus drawer top-ups, less payouts. Credit sales are excluded — they never entered a drawer.",
        value=now.net_cash, display=money(now.net_cash), unit="PKR",
        sub=f"{money(now.credit_sales)} on credit",
        delta=_delta(now.net_cash, before.net_cash, period.compare_label))

    overrides = await ex.discount_overrides(period)
    override_total = sum((o["discTotal"] for o in overrides), D0)
    approvers = {o["approvedBy"] for o in overrides if o["approvedBy"]}
    add(id="discount-overrides", label="Discount Overrides", group="Money",
        hint="Discounts given above a salesperson's own authority, and the manager who signed each one off. Margin given away on someone's signature.",
        value=override_total, display=money(override_total), unit="PKR",
        sub=f"{len(overrides)} sale(s) · {len(approvers)} approver(s)" if overrides else "none in this period",
        severity="watch" if overrides else "good")

    add(id="returns", label="Returns", group="Sales",
        hint="Goods brought back and refunded. Returns come straight off net sales, so a rising figure is margin leaving the business.",
        value=now.returns_value, display=money(now.returns_value), unit="PKR",
        sub=f"{now.returns_count} return(s)",
        delta=_delta(now.returns_value, before.returns_value, period.compare_label, higher_is_better=False),
        severity="watch" if now.returns_value > D0 else "good")

    variance_bad = abs(now.till_variance) > D0
    add(id="till-variance", label="Till Variance", group="Money",
        hint="Counted cash minus what the system expected, across every till close in the period.",
        value=now.till_variance, display=money(now.till_variance), unit="PKR",
        sub=f"{now.tills_closed} till close(s)",
        severity="bad" if variance_bad else "good")

    # ── Stock ───────────────────────────────────────────────────────────────
    branch_stock = await ex.latest_stock_value()
    godown_stock = await ex.godown_stock_value()
    add(id="inventory-value", label="Inventory Value", group="Stock",
        hint="Branch floors plus the godown, valued at sale price. The godown half is live; the branch half is as last reported.",
        value=branch_stock + godown_stock, display=money(branch_stock + godown_stock), unit="PKR",
        sub=f"godown {money(godown_stock)}", source="mixed")

    _, alert_totals = await ex.stock_alerts()
    godown = await ex.godown_alerts()
    oos = alert_totals.get("out-of-stock", 0) + len(godown["outOfStock"])
    low = alert_totals.get("low-stock", 0) + len(godown["lowStock"])
    expiring = alert_totals.get("near-expiry", 0) + alert_totals.get("expired", 0)

    add(id="out-of-stock", label="Out of Stock", group="Stock",
        hint="Lines showing zero or negative stock, across branch floors and the godown.",
        value=Decimal(oos), display=num(oos), unit="count",
        sub=f"{len(godown['outOfStock'])} in the godown",
        severity="bad" if oos else "good", source="mixed")

    add(id="low-stock", label="Low Stock", group="Stock",
        hint=f"Lines below {ex.LOW_STOCK_THRESHOLD} units.",
        value=Decimal(low), display=num(low), unit="count",
        sub=f"{len(godown['lowStock'])} in the godown",
        severity="watch" if low else "good", source="mixed")

    add(id="expiring", label="Expiring & Expired", group="Stock",
        hint="Batches already expired or within 30 days of expiry. For groceries and pharmacy this is where value is actually lost.",
        value=Decimal(expiring), display=num(expiring), unit="count",
        sub=f"{alert_totals.get('expired', 0)} already expired",
        severity="bad" if alert_totals.get("expired") else ("watch" if expiring else "good"))

    # ── Classification ──────────────────────────────────────────────────────
    # Four different questions about the same shelf, kept apart on purpose: which lines earn the
    # money (ABC), which can be forecast (XYZ), which have stopped selling (dead stock), and which
    # move fastest (velocity). Collapsing them into one "product score" is how a flag becomes
    # impossible to explain.
    abc_rows, abc_cov = await an.abc(period.start, period.end)
    a_lines = [r for r in abc_rows if r["abcClass"] == "A"]
    a_share = sum(Decimal(str(r["sharePct"])) for r in a_lines)
    add(id="abc-analysis", label="ABC Classification", group="Stock",
        hint="Pareto by value: A carries to 80% of net sales, B to 95%, C the tail. Where buying attention is worth spending.",
        value=Decimal(len(a_lines)), display=num(len(a_lines)), unit="count",
        sub=(f"A lines = {pct(a_share)} of sales" if a_lines else "nothing sold in this period"),
        ctaLabel="See the classification")

    xyz_rows, xyz_cov = await an.xyz(period.start, period.end)
    if xyz_cov.reason:
        # Not enough trading history to say anything. Say that, rather than printing a class
        # breakdown computed from noise.
        add(id="xyz-analysis", label="XYZ Demand Pattern", group="Stock",
            hint="How predictable each line's daily demand is — the input to safety stock.",
            display="Not enough history", unit="text", sub=f"{xyz_cov.trading_days} trading day(s) so far",
            severity="watch", ctaLabel="Why not")
    else:
        steady = [r for r in xyz_rows if r["xyzClass"] == "X"]
        erratic = [r for r in xyz_rows if r["xyzClass"] == "Z"]
        # "0 steady out of 424" is arithmetically right and tells an executive nothing. When
        # nothing is forecastable, the finding IS that nothing is forecastable — lead with it.
        if steady:
            display, sub, severity = num(len(steady)), f"lines steady enough to plan on, of {num(len(xyz_rows))}", None
        else:
            display, sub = "None steady", f"all {num(len(xyz_rows))} lines sell too sparsely to forecast"
            severity = "watch"
        add(id="xyz-analysis", label="XYZ Demand Pattern", group="Stock",
            hint="How predictable each line's daily demand is. X is steady, Y variable, Z erratic — computed across every trading day, including the days a line sold nothing.",
            value=Decimal(len(steady)), display=display, unit="count", sub=sub, severity=severity,
            ctaLabel="See the classification")

    # Summary only — the tile needs three numbers, not 47,000 rows. See dead_stock_summary().
    dead_summary = await an.dead_stock_summary()
    dead_value = sum((Decimal(v["value"]) for v in dead_summary.values()), D0)
    dead_lines = sum(v["lines"] for v in dead_summary.values())
    add(id="dead-stock", label="Dead Stock", group="Stock",
        hint="Stock on the shelf that nothing is pulling through — never sold, or not sold in 90+ days. Valued at cost, because that is the money actually tied up.",
        value=dead_value, display=money(dead_value), unit="PKR",
        sub=f"{num(dead_lines)} line(s) not moving",
        severity="bad" if dead_lines else "good", ctaLabel="See what's stuck")

    move_rows, _ = await an.movement(period.start, period.end)
    fast = [r for r in move_rows if r["band"] == "fast"]
    add(id="movement", label="Fast & Slow Moving", group="Stock",
        hint="Units sold per trading day, banded. A shelf-space question, not a money question — a cheap line can be the fastest thing in the shop and still be C-class.",
        value=Decimal(len(fast)), display=num(len(fast)), unit="count",
        sub=(f"fast lines of {num(len(move_rows))}" if move_rows else "nothing sold in this period"),
        ctaLabel="See the bands")

    # ── Performance ─────────────────────────────────────────────────────────
    products = await ex.top_products(period, limit=1)
    add(id="top-products", label="Top Products", group="Performance",
        hint="Ranked by net sales in the period.",
        display=products[0]["name"] if products else "—", unit="text",
        sub=money(products[0]["netSales"]) if products else "no sales in this period")

    cats = await ex.top_by(period, "category", limit=1)
    add(id="top-categories", label="Top Categories", group="Performance",
        hint="Product categories ranked by net sales, using the catalog's own taxonomy.",
        display=cats[0]["name"] if cats else "—", unit="text",
        sub=money(cats[0]["netSales"]) if cats else "no sales in this period")

    brands = await ex.top_by(period, "brand", limit=1)
    add(id="top-brands", label="Top Brands", group="Performance",
        hint="Brands ranked by net sales.",
        display=brands[0]["name"] if brands else "—", unit="text",
        sub=money(brands[0]["netSales"]) if brands else "no sales in this period")

    cashiers = await ex.cashier_ranking(period, limit=1)
    add(id="best-cashier", label="Best Salesperson", group="Performance",
        hint="Ranked by net sales rung in the period. The person who rings the sale is the only sales attribution that exists — there is no separate commission-carrying salesperson.",
        display=cashiers[0]["name"] if cashiers else "—", unit="text",
        sub=f"{money(cashiers[0]['netSales'])} · {cashiers[0]['invoices']} invoice(s)" if cashiers else "no sales in this period")

    staffing = await ex.staffing(period)
    latest = staffing[0] if staffing else None
    # What the branch says it put on the counters, when it says anything; otherwise the old measure,
    # which can only see people who rang something.
    latest_staff = (latest["onDuty"] if latest and latest["onDuty"] is not None else (latest["traded"] if latest else 0)) or 0
    # Sits with Sales rather than in a Performance group of its own. Once Top Products, Top
    # Categories, Top Brands, Best Cashier and Branch Comparison became charts, Performance held
    # exactly one tile — and a group heading over a single card is furniture, not structure. Staff
    # on Duty belongs next to Customer Count and Bills Per Hour anyway: they are all "what did the
    # trading day look like".
    add(id="staffing", label="Staff on Duty", group="Sales",
        hint="How many people were on a counter, per branch per day — and how many of them rang a sale.",
        value=Decimal(latest_staff), display=num(latest_staff), unit="count",
        sub=(f"on {latest['day']} · {num(latest['traded'])} rang a sale" if latest and latest["onDuty"] is not None
             else f"rang a sale on {latest['day']}" if latest else "no trading days in this period"))

    comparison = await ex.branch_comparison(period)
    reporting = [b for b in comparison if b["reporting"]]
    add(id="branch-comparison", label="Branch Comparison", group="Performance",
        hint="Every registered branch ranked by net sales. A branch that has never reported shows as such rather than as zero.",
        display=comparison[0]["name"] if reporting else "—", unit="text",
        sub=f"{len(reporting)} of {len(comparison)} branch(es) reporting")

    # ── Supply ──────────────────────────────────────────────────────────────
    wh = await ex.warehouse_status()
    add(id="warehouse-status", label="Warehouse Status", group="Supply",
        hint="The godown right now: stock value, what is in flight, and what is stuck.",
        value=wh["stockValue"], display=money(wh["stockValue"]), unit="PKR",
        sub=f"{wh['inFlight']} in flight · {wh['disputesOpen']} dispute(s)",
        severity="bad" if wh["disputesOpen"] else "good", source=LIVE, asOf=None)

    ps = await ex.purchase_status()
    add(id="purchase-status", label="Purchase Status", group="Supply",
        hint="Goods received into the godown, and requisitions waiting on a decision.",
        value=ps["receivedValue"], display=money(ps["receivedValue"]), unit="PKR",
        sub=f"{ps['grnCount']} GRN(s) · {ps['pendingRequisitions']} requisition(s) pending",
        severity="watch" if ps["pendingRequisitions"] else "good", source=LIVE, asOf=None)

    suppliers = await ex.supplier_performance()
    active_suppliers = [s for s in suppliers if s["receipts"]]
    add(id="supplier-performance", label="Supplier Performance", group="Supply",
        hint="Receipts into the godown by supplier. On-time and quality scoring need a promised date and a rejection reason, neither of which is recorded yet.",
        display=active_suppliers[0]["name"] if active_suppliers else "—", unit="text",
        sub=f"{len(active_suppliers)} of {len(suppliers)} supplier(s) have delivered",
        source=LIVE, asOf=None)

    # ── Health ──────────────────────────────────────────────────────────────
    health = await ex.business_health(period)
    add(id="health-score", label="Business Health Score", group="Health",
        hint="A weighted composite of sales trend, margin, till accuracy, stock availability, supply friction and branch reporting. Nothing here is predicted — every component is measured.",
        value=Decimal(health.score), display=f"{health.score}", unit="score", sub=health.band,
        severity="good" if health.score >= 80 else ("watch" if health.score >= 60 else "bad"),
        source="mixed")

    alerts = await ex.live_alerts()
    high = [a for a in alerts if a["severity"] == "high"]
    add(id="live-alerts", label="Live Alerts", group="Health",
        hint="Everything currently asking for a decision, most serious first.",
        value=Decimal(len(alerts)), display=num(len(alerts)), unit="count",
        sub=f"{len(high)} need attention now" if high else "nothing urgent",
        severity="bad" if high else ("watch" if alerts else "good"), source="mixed")

    branches = await ex.Branch.all()
    never = [b for b in branches if b.status == "active" and not b.last_seen_at]
    add(id="sync-health", label="Sync Health", group="Health",
        hint="Which branches have reported, and how recently. Live push is not built yet — snapshots are imported.",
        value=Decimal(len(branches) - len(never)), display=f"{len(branches) - len(never)}/{len(branches)}", unit="text",
        sub=f"{len(never)} never reported" if never else "all branches reporting",
        severity="watch" if never else "good", source=LIVE, asOf=None)

    return KpiListOut(
        period=_period_out(period),
        periods=[_period_out(ex.resolve_period(p)) for p in ex.PERIOD_IDS],
        businessDate=ex.business_today(),
        asOf=as_of,
        items=items if include_charted else [k for k in items if k.id not in CHART_BACKED],
        charts=await _build_charts(period),
        notBuilt=NOT_BUILT,
    )


# ── drill-downs ─────────────────────────────────────────────────────────────
def _cols(*specs) -> list[TableColumn]:
    """(key, label, align, format) — or add (linkTo, linkParam[, linkValueKey]) to make the cell
    a link into another KPI."""
    out = []
    for spec in specs:
        k, l, a, f = spec[:4]
        param = spec[5] if len(spec) > 5 else None
        many = param if isinstance(param, dict) else None
        out.append(TableColumn(
            key=k, label=l, align=a, format=f,
            linkTo=spec[4] if len(spec) > 4 else None,
            linkParam=None if many else param,
            linkValueKey=spec[6] if len(spec) > 6 else None,
            linkParams=many,
        ))
    return out


# One person's sales of one item: the link needs both.
PERSON_ITEM = {"name": "person", "sku": "sku"}

_NO_PERSON_ITEMS = ("No branch has sent who-sold-what for this period yet. It arrives with the branch's next report "
                    "once the branch server is on the current version.")


def _section(title: str, columns: list[TableColumn], rows: list[dict], empty: str, subtitle: str | None = None,
             action: tuple[str, str, dict] | None = None) -> DetailSection:
    return DetailSection(
        title=title, subtitle=subtitle, columns=columns, rows=rows, emptyText=empty,
        actionLabel=action[0] if action else None, actionKpi=action[1] if action else None,
        actionFocus=action[2] if action else {},
    )


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


async def _build_charts(period: ex.Period) -> list[ChartOut]:
    """The ranked pictures that sit under the tile grid.

    A tile answers "what is the number"; these answer "and then what" — second, third, fourth, all
    the way down the chart. Top Products as a tile can only ever name the winner, which tells an
    executive nothing about whether the winner is miles ahead or a nose in front. The bars make the
    shape of the distribution visible, which is the actual question.

    Every bar carries its own destination, so a chart is as navigable as a table: clicking the
    tallest bar and clicking the top row of the drill-down land in the same place.
    """
    charts: list[ChartOut] = []

    def bars(chart_id: str, title: str, rows: list[dict], *, label_key: str, value_key: str,
             link_to: str | None = None, link_param: str | None = None, link_key: str | None = None,
             sub_key: str | None = None, unit: str = "PKR", subtitle: str | None = None,
             cta_kpi: str | None = None, cta_label: str | None = None, empty: str = "Nothing to show yet.",
             note: str | None = None, fmt=None) -> ChartOut:
        fmt = fmt or (lambda v: money(v))
        return ChartOut(
            id=chart_id, title=title, subtitle=subtitle, kind="bar-h", unit=unit,
            points=[
                ChartPoint(
                    label=str(r.get(label_key) or "Unclassified"),
                    value=Decimal(str(r.get(value_key) or 0)),
                    display=fmt(Decimal(str(r.get(value_key) or 0))),
                    sub=(str(r[sub_key]) if sub_key and r.get(sub_key) else None),
                    linkTo=link_to, linkParam=link_param,
                    linkValue=(str(r.get(link_key or label_key)) if r.get(link_key or label_key) else None),
                ) for r in rows
            ],
            emptyText=empty, ctaKpi=cta_kpi, ctaLabel=cta_label, note=note,
        )

    # ── the trading day's shape ─────────────────────────────────────────────
    hours = await ex.hourly_profile(period)
    if hours:
        busiest = max(hours, key=lambda h: h["netSales"])["hour"]
        charts.append(ChartOut(
            id="sales-by-hour", title="Sales through the day",
            subtitle="Average per trading hour across the period",
            kind="bar-v", unit="PKR",
            points=[
                ChartPoint(
                    label="%02d" % h["hour"], value=h["netSales"], display=money(h["netSales"]),
                    sub=h["label"],
                    # The busiest hour is the one worth the eye — it is where staffing and till
                    # cover get argued about.
                    tone="accent" if h["hour"] == busiest else None,
                ) for h in hours
            ],
            emptyText="No branch has reported an hourly profile yet.",
            ctaKpi="bills-per-hour", ctaLabel="See the hour-by-hour detail",
            note="Hours are branch local time (UTC+5).",
        ))

    # ── who and what is selling ─────────────────────────────────────────────
    charts.append(bars(
        "chart-top-products", "Top products by value", await ex.top_products(period, limit=12),
        label_key="name", value_key="netSales", sub_key="category",
        link_to="product-detail", link_param="sku", link_key="sku",
        cta_kpi="top-products", cta_label="See all 100",
        empty="No sales in this period.",
        note="Click a bar for who sold it and its day-by-day trend.",
    ))
    charts.append(bars(
        "chart-top-categories", "Top categories", await ex.top_by(period, "category", limit=10),
        label_key="name", value_key="netSales",
        link_to="category-detail", link_param="name",
        cta_kpi="top-categories", cta_label="See every category",
        empty="No sales in this period.",
    ))
    charts.append(bars(
        "chart-top-brands", "Top brands", await ex.top_by(period, "brand", limit=10),
        label_key="name", value_key="netSales",
        link_to="brand-detail", link_param="name",
        cta_kpi="top-brands", cta_label="See every brand",
        empty="No sales in this period.",
    ))

    # ── who is performing ───────────────────────────────────────────────────
    branches = await ex.branch_comparison(period)
    charts.append(bars(
        "chart-branches", "Branch comparison", branches,
        label_key="name", value_key="netSales", sub_key="code",
        link_to="branch-detail", link_param="code", link_key="code",
        cta_kpi="branch-comparison", cta_label="See the full comparison",
        empty="No branch has reported yet.",
        note="A branch that has never reported shows zero rather than being hidden — silence is a finding.",
    ))
    charts.append(bars(
        "chart-staff", "Salesperson performance", await ex.cashier_ranking(period, limit=12),
        label_key="name", value_key="netSales", sub_key="invoices",
        link_to="cashier-detail", link_param="name",
        cta_kpi="best-cashier", cta_label="See every salesperson",
        empty="Nobody has rung a sale in this period.",
        note="Ranked by net sales rung. Click a bar for everything that person sold, their days and their till closes.",
    ))

    # ── how the catalog is shaped ───────────────────────────────────────────
    abc_rows, _ = await an.abc(period.start, period.end)
    if abc_rows:
        summary = _band_summary(abc_rows, "abcClass", ("A", "B", "C"), {})
        charts.append(bars(
            "chart-abc", "ABC — where the value sits", summary,
            label_key="klass", value_key="netSales",
            link_to="abc-class", link_param="name", link_key="raw",
            subtitle="Net sales by Pareto class",
            cta_kpi="abc-analysis", cta_label="See the classification",
            empty="Nothing sold in this period.",
            note="A carries to 80% of cumulative sales, B to 95%, C the tail. Click a bar for its lines.",
        ))

    xyz_rows, xyz_cov = await an.xyz(period.start, period.end)
    if xyz_rows:
        summary = _band_summary(xyz_rows, "xyzClass", ("X", "Y", "Z"), {})
        charts.append(bars(
            "chart-xyz", "XYZ — how predictable demand is", summary,
            label_key="klass", value_key="lines",
            link_to="xyz-class", link_param="name", link_key="raw",
            unit="count", fmt=lambda v: num(v),
            subtitle="Lines by demand pattern",
            cta_kpi="xyz-analysis", cta_label="See the classification",
            empty="Nothing sold in this period.",
            note="X steady, Y variable, Z erratic — measured across every trading day, including the days a line sold nothing.",
        ))
    elif xyz_cov.reason:
        charts.append(ChartOut(
            id="chart-xyz", title="XYZ — how predictable demand is", kind="bar-h", unit="count",
            points=[], emptyText=xyz_cov.reason,
            ctaKpi="xyz-analysis", ctaLabel="Why not",
            note=f"Needs {an.MIN_DAYS_FOR_XYZ} trading days. It will start answering on its own.",
        ))

    return charts


def _band_summary(rows: list[dict], key: str, order: tuple[str, ...], meanings: dict[str, str],
                  label_map: dict[str, str] | None = None) -> list[dict]:
    """Roll a classified product list up into one row per class.

    Both a share of *lines* and a share of *sales* are reported, because the gap between them is
    the finding. "12% of the lines earn 80% of the money" is the sentence somebody acts on; either
    number alone is just a count.
    """
    total_sales = sum((Decimal(r.get("netSales") or 0) for r in rows), D0)
    total_lines = len(rows) or 1
    out = []
    for klass in order:
        members = [r for r in rows if r.get(key) == klass]
        if not members:
            continue
        sales = sum((Decimal(r.get("netSales") or 0) for r in members), D0)
        profit = sum((Decimal(r.get("grossProfit") or 0) for r in members), D0)
        out.append({
            # `raw` is the machine value the drill-down link passes; `klass` is what a person reads.
            "raw": klass,
            "klass": (label_map or {}).get(klass, klass),
            "meaning": meanings.get(klass, ""),
            "lines": len(members),
            "linesPct": float(Decimal(len(members)) / Decimal(total_lines) * 100),
            "netSales": str(sales),
            "salesPct": float(sales / total_sales * 100) if total_sales else 0.0,
            "grossProfit": str(profit),
        })
    return out


def _headline(kpi_id: str, label: str, group: str, hint: str, display: str, unit: str,
              sub: str | None = None, value: Decimal | None = None,
              source: str = SNAPSHOT, as_of=None) -> KpiOut:
    """A focused view is not on the dashboard grid, so it builds its own headline."""
    return KpiOut(
        id=kpi_id, label=label, group=group, hint=hint, value=value, display=display,
        unit=unit, sub=sub, source=source, asOf=as_of, ctaLabel="See the detail",
    )


# Second-level views, reached by clicking a row rather than a tile.
FOCUSED = {
    "category-detail", "brand-detail", "product-detail", "cashier-detail", "cashier-product-detail",
    "branch-detail", "day-detail", "supplier-detail", "godown-stock", "credit-customers",
    "abc-class", "xyz-class", "dead-stock-band", "movement-band",
}

_SALES_KPIS = {"sales-today": "today", "sales-7d": "7d", "sales-30d": "30d", "sales-ytd": "ytd"}


async def kpi_detail(kpi_id: str, period_id: str, focus: dict[str, str] | None = None) -> KpiDetailOut:
    focus = {k: v for k, v in (focus or {}).items() if v}
    period = ex.resolve_period(period_id)
    as_of = await ex.snapshot_as_of()

    if kpi_id in FOCUSED:
        return await _focused(kpi_id, period, focus, as_of)

    # The full set, including the chart-backed KPIs the grid no longer shows — their drill-downs
    # are exactly where the charts link to, so they must still resolve.
    grid = await list_kpis(period_id, include_charted=True)
    headline = next((k for k in grid.items if k.id == kpi_id), None)
    if not headline:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No KPI with id {kpi_id}")
    # Period totals, for the drill-downs whose notes quote the surrounding figures.
    now = await ex.totals_for(period)
    base = dict(id=kpi_id, label=headline.label, group=headline.group, hint=headline.hint,
                headline=headline, period=_period_out(period), source=headline.source, asOf=headline.asOf)

    # ── sales, by day — each day opens into its own hour-by-hour picture ──────
    if kpi_id in _SALES_KPIS:
        scope = ex.resolve_period(_SALES_KPIS[kpi_id])
        series = await ex.daily_series(scope, "net_sales")
        rows = await ex.daily_breakdown(scope)
        return KpiDetailOut(
            **base,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=p["day"], value=p["value"]) for p in series],
            columns=_cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("netSales", "Net sales", "right", "money"),
                ("invoices", "Invoices", "right", "number"),
                ("avgBasket", "Avg basket", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("cashCollected", "Cash taken", "right", "money"),
                ("creditSales", "On credit", "right", "money"),
                ("returnsValue", "Returns", "right", "money"),
                ("staffOnDuty", "Staff", "right", "number"),
            ),
            rows=rows,
            emptyText="No branch reported sales in this period.",
            notes=[
                "One row per trading day. Click a day to see its hour-by-hour profile, who was on, and what sold.",
                "Gross profit uses each product's weighted-average cost at snapshot time; it is not captured on the sale line, so it is close rather than exact.",
                "Operating expenses are not included anywhere — Net Profit needs the Finance module.",
            ],
        )

    # ── gross profit — the question is which categories earn it ──────────────
    if kpi_id == "gross-profit":
        rows = await ex.top_by(period, "category", limit=40)
        for r in rows:
            r["marginPercent"] = (r["grossProfit"] / r["netSales"] * 100) if r["netSales"] else D0
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Category", "left", "text", "category-detail", "name"),
                ("products", "Items", "right", "number"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
            ),
            rows=rows,
            emptyText="No sales in this period.",
            notes=[
                "Ranked by gross profit contribution. Click a category to see the products inside it.",
                "Margin is gross profit as a share of that category's own net sales — a big earner on thin margin and a small one on fat margin are different problems.",
                "Cost comes from each product's weighted-average cost; operating expenses are not deducted.",
            ],
        )

    # ── average basket — whose baskets, and how they differ ───────────────────
    if kpi_id == "avg-basket":
        rows = await ex.cashier_ranking(period, limit=50)
        days = await ex.daily_breakdown(period)
        return KpiDetailOut(
            **base,
            seriesLabel="Average basket by day",
            series=[SeriesPoint(day=d["day"], value=d["avgBasket"]) for d in reversed(days)],
            columns=_cols(
                ("name", "Salesperson", "left", "text", "cashier-detail", "name"),
                ("invoices", "Invoices", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("avgBasket", "Avg basket", "right", "money"),
                ("days", "Days worked", "right", "number"),
            ),
            rows=rows,
            emptyText="No sales in this period.",
            notes=[
                "Basket size is where upselling shows up. Two cashiers with the same invoice count and different baskets are doing different jobs.",
                "Click a cashier for their day-by-day figures.",
            ],
        )

    # ── customer count — invoices are visits; these are the accounts ──────────
    if kpi_id == "customer-count":
        rows = await ex.credit_customers()
        days = await ex.daily_breakdown(period)
        return KpiDetailOut(
            **base,
            seriesLabel="Invoices by day",
            series=[SeriesPoint(day=d["day"], value=Decimal(d["invoices"])) for d in reversed(days)],
            # The accounts sit in a section rather than the main table so they carry an onward link:
            # Credit Customers ranks the same people by what they owe, and this is the only way in.
            sections=[_section(
                "Credit accounts",
                _cols(
                    ("name", "Customer", "left", "text"), ("code", "Code", "left", "mono"),
                    ("tier", "Tier", "left", "text"),
                    ("creditLimit", "Credit limit", "right", "money"),
                    ("creditBalance", "Owed now", "right", "money"),
                    ("headroom", "Headroom", "right", "money"),
                    ("usedPercent", "Limit used", "right", "percent"),
                ),
                rows,
                "No customers are on credit terms.",
                action=("See what they owe", "credit-customers", {}),
            )],
            notes=[
                f"{now.invoices} invoices were rung in this period; {now.named_customers} were attached to a named Party. The rest were walk-in, which the till does not identify.",
                "Credit accounts lists the customers who carry credit — the ones whose behaviour actually costs money if it changes.",
            ],
        )

    # ── bills per hour — the whole point is which hour ────────────────────────
    if kpi_id == "bills-per-hour":
        rows = await ex.hourly_profile(period)
        return KpiDetailOut(
            **base,
            seriesLabel="Invoices by hour of day",
            series=[SeriesPoint(day=f"{r['hour']:02d}", value=Decimal(r["invoices"]), label=f"{r['hour']:02d}h") for r in rows],
            columns=_cols(
                ("label", "Hour", "left", "text"),
                ("invoices", "Invoices", "right", "number"),
                ("billsPerHour", "Bills / hour", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("daysTraded", "Days traded", "right", "number"),
            ),
            rows=rows,
            emptyText="No trading recorded in this period.",
            notes=[
                "Hours are the branch's own local clock. This is where the queue forms, and therefore where staff need to be.",
                "Bills/hour divides that hour's invoices by the number of days the branch actually traded in it — not by the whole period.",
            ],
        )

    # ── cash position — how customers actually paid ───────────────────────────
    if kpi_id == "cash-position":
        rows = await ex.tender_mix(period)
        series = await ex.daily_series(period, "cash_collected")
        return KpiDetailOut(
            **base,
            seriesLabel="Cash collected by day",
            series=[SeriesPoint(day=p["day"], value=p["value"]) for p in series],
            columns=_cols(
                ("name", "Payment method", "left", "text"), ("code", "Code", "left", "mono"),
                ("uses", "Times used", "right", "number"),
                ("amount", "Amount", "right", "money"),
                ("share", "Share", "right", "percent"),
            ),
            rows=rows,
            emptyText="Nothing was tendered in this period.",
            notes=[
                f"Cash taken {money(now.cash_collected)}, drawer top-ups {money(now.cash_in)}, payouts {money(now.cash_out)} — net {money(now.net_cash)}.",
                f"Credit sales of {money(now.credit_sales)} are excluded from cash: goods left the shop but nothing entered a drawer.",
                "Cash, card and credit have different consequences for working capital, which a single sales figure hides.",
            ],
        )

    # ── till variance — which drawer, whose shift ─────────────────────────────
    if kpi_id == "till-variance":
        rows = await ex.till_closes(period)
        worst = max((abs(r["variance"]) for r in rows), default=D0)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("sessionNumber", "Session", "left", "mono"),
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("cashier", "Salesperson", "left", "text", "cashier-detail", "name", "cashier"),
                ("openingFloat", "Float", "right", "money"),
                ("netCash", "Expected", "right", "money"),
                ("countedCash", "Counted", "right", "money"),
                ("variance", "Variance", "right", "money"),
            ),
            rows=rows,
            emptyText="No till has closed in this period.",
            notes=[
                "Every close, not a total — a net-zero variance can hide a large over and a large under on the same day.",
                f"Largest single variance in this period: {money(worst)}.",
                "Click a cashier to see their whole record, or a day to see that day's trading.",
            ],
        )

    # ── discount overrides — margin given away, by name ───────────────────────
    if kpi_id == "discount-overrides":
        rows = await ex.discount_overrides(period)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("invoiceNumber", "Invoice", "left", "mono"),
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("cashier", "Rung by", "left", "text", "cashier-detail", "name", "cashier"),
                ("approvedBy", "Approved by", "left", "text"),
                ("gross", "Gross", "right", "money"),
                ("discTotal", "Discount", "right", "money"),
                ("discPercent", "Disc %", "right", "percent"),
                ("netValue", "Net", "right", "money"),
            ),
            rows=rows,
            emptyText="No discount needed a manager's approval in this period.",
            notes=[
                "A cashier may discount up to their own authority; anything above needs a manager to sign in at the till. This is every one of those.",
                "The approver's name is recorded on the sale, not the cashier's — which is the point of the control.",
            ],
        )

    # ── returns ──────────────────────────────────────────────────────────────
    if kpi_id == "returns":
        rows = await ex.returns_detail(period)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("againstInvoice", "Against invoice", "left", "mono"),
                ("productName", "Item", "left", "text", "product-detail", "sku", "productSku"),
                ("qty", "Qty", "right", "number"),
                ("cashier", "Handled by", "left", "text", "cashier-detail", "name", "cashier"),
                ("refundTotal", "Refund", "right", "money"),
            ),
            rows=rows,
            emptyText="Nothing was returned in this period.",
            notes=[
                "Each return is tied to the invoice it came back against, so a pattern against one till or one item is visible rather than buried in a total.",
                "Refund value is what left the drawer; the stock went back on the shelf at the same time.",
            ],
        )

    # ── inventory value ──────────────────────────────────────────────────────
    if kpi_id == "inventory-value":
        branch_rows = await ex.branch_comparison(period)
        godown = await ex.godown_stock_value()
        # Each row says where it drills to: a branch floor opens that branch's trading, the godown
        # opens its own ledger line by line.
        rows = [{"name": r["name"], "code": r["code"], "kind": "Branch floor",
                 "value": r["stockValue"], "target": "branch-detail", "targetValue": r["code"]}
                for r in branch_rows]
        rows.append({"name": "Godown (Warehouse)", "code": "—", "kind": "Godown",
                     "value": godown, "target": "godown-stock", "targetValue": "godown"})
        return KpiDetailOut(
            **base,
            columns=[
                TableColumn(key="name", label="Location", align="left", format="text",
                            linkToKey="target", linkParam="code", linkValueKey="targetValue"),
                TableColumn(key="kind", label="Kind", align="left", format="text"),
                TableColumn(key="value", label="Stock value", align="right", format="money"),
            ],
            rows=sorted(rows, key=lambda r: r["value"], reverse=True),
            notes=[
                "Valued at sale price, not at cost.",
                "The godown figure is live and can be broken down line by line — see Warehouse Status. Branch floors report a single total, so there is no per-item breakdown for them until sync carries it.",
            ],
        )

    # ── stock exceptions ─────────────────────────────────────────────────────
    if kpi_id in ("out-of-stock", "low-stock", "expiring"):
        kinds = {"out-of-stock": ["out-of-stock"], "low-stock": ["low-stock"],
                 "expiring": ["expired", "near-expiry"]}[kpi_id]
        rows: list[dict] = []
        for kind in kinds:
            items, _ = await ex.stock_alerts(kind, limit=300)
            rows.extend(items)
        if kpi_id in ("out-of-stock", "low-stock"):
            godown = await ex.godown_alerts()
            key = "outOfStock" if kpi_id == "out-of-stock" else "lowStock"
            for b in godown[key]:
                rows.append({"branch": "Godown (Warehouse)", "kind": kpi_id, "sku": b["sku"],
                             "name": b["name"], "qty": b["qty"], "expiry": None, "detail": None})
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("branch", "Where", "left", "text"),
                ("qty", "Qty", "right", "number"),
                ("detail", "Note", "left", "text"),
            ),
            rows=rows[:300],
            emptyText="Nothing flagged — stock is healthy on this measure.",
            notes=[
                "Branch rows are as last reported; godown rows are live.",
                "Capped at the most urgent 300 lines. The tile's count is the true total.",
            ],
        )

    # ── classification ───────────────────────────────────────────────────────
    if kpi_id == "abc-analysis":
        rows, cov = await an.abc(period.start, period.end)
        bands = _band_summary(rows, "abcClass", ("A", "B", "C"), {
            "A": "The lines carrying the business", "B": "Solid middle", "C": "The long tail",
        })
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("klass", "Class", "left", "text", "abc-class", "name", "raw"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Lines", "right", "number"),
                ("linesPct", "% of lines", "right", "percent"),
                ("netSales", "Net sales", "right", "money"),
                ("salesPct", "% of sales", "right", "percent"),
                ("grossProfit", "Gross profit", "right", "money"),
            ),
            rows=bands,
            emptyText="Nothing sold in this period, so there is nothing to rank.",
            notes=[
                "Products ranked by net sales, then cut on the cumulative curve: A to 80%, B to 95%, C the rest. The standard Pareto cuts, fixed rather than tunable so periods stay comparable.",
                "By value, not by units — a pharmacy line selling four boxes at Rs 3,000 outranks a sweet selling four hundred at Rs 5. Volume is the Fast & Slow Moving tile.",
                f"Covers {num(cov.products)} product(s) that sold across {num(cov.trading_days)} trading day(s). Lines that sold nothing in the period are not ranked — they are in Dead Stock.",
                "Click a class to see every line in it.",
            ],
        )

    if kpi_id == "xyz-analysis":
        rows, cov = await an.xyz(period.start, period.end)
        if cov.reason:
            return KpiDetailOut(
                **base, columns=[], rows=[],
                emptyText=cov.reason,
                notes=[
                    cov.reason,
                    "XYZ measures how much daily demand varies around its own average. With only a handful of trading days that ratio is arithmetic, not information — it would label almost every line 'erratic' and be reporting noise.",
                    f"It needs {an.MIN_DAYS_FOR_XYZ} trading days. This will start answering on its own once the branches have reported that much.",
                    "ABC, Dead Stock and Fast & Slow Moving do not have this constraint and are live now.",
                ],
            )
        bands = _band_summary(rows, "xyzClass", ("X", "Y", "Z"), {
            "X": "Steady — plan on it", "Y": "Variable — seasonal or promo-driven", "Z": "Erratic — hard to forecast",
        })
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("klass", "Class", "left", "text", "xyz-class", "name", "raw"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Lines", "right", "number"),
                ("linesPct", "% of lines", "right", "percent"),
                ("netSales", "Net sales", "right", "money"),
                ("salesPct", "% of sales", "right", "percent"),
            ),
            rows=bands,
            emptyText="Nothing sold in this period.",
            notes=[
                "Coefficient of variation of daily demand: standard deviation divided by the mean. X is 0.5 or below, Y up to 1.0, Z above — the conventional cuts.",
                "Measured across EVERY trading day in the period, including the days a line sold nothing. That is the point: an item selling forty units on one day and nothing on the other thirty is erratic, and averaging only its selling days would call it perfectly steady.",
                f"Based on {num(cov.trading_days)} trading day(s). More history makes this sharper; it is least reliable for lines that sell rarely.",
                "Read it alongside ABC: an A line that is also Z — depended on and unforecastable — is the one worth a conversation.",
            ],
        )

    if kpi_id == "dead-stock":
        summary = await an.dead_stock_summary()
        meaning = {
            "never-sold": "Never sold since it was taken into stock",
            "dead": f"No sale in {an.DEAD_DAYS}+ days",
            "stale": f"No sale in {an.STALE_DAYS}-{an.DEAD_DAYS} days",
        }
        label = {"never-sold": "Never sold", "dead": f"Dead ({an.DEAD_DAYS}+ days)",
                 "stale": f"Stale ({an.STALE_DAYS}-{an.DEAD_DAYS} days)"}
        band_rows = [{
            # `band` is the slug the link passes; `bandLabel` is what a person reads. Showing the
            # slug was a small thing that made the table look like debug output.
            "band": b, "bandLabel": label[b], "meaning": meaning[b],
            "lines": summary[b]["lines"], "valueCost": summary[b]["value"],
        } for b in ("never-sold", "dead", "stale") if summary[b]["lines"]]
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("bandLabel", "Band", "left", "text", "dead-stock-band", "name", "band"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Lines", "right", "number"),
                ("valueCost", "Value at cost", "right", "money"),
            ),
            rows=band_rows,
            emptyText="Every line holding stock has sold recently. Nothing is stuck.",
            notes=[
                "Valued at cost, not retail: this is money already spent and sitting still, which is the figure a buying decision turns on.",
                "'Last sold' comes from each branch's full sales history, not from the window of days the Cloud holds — an item last sold eighteen months ago is invisible to a 30-day dataset and obvious to the branch, so the branch computes it.",
                "Only lines with stock actually on hand are counted. A discontinued line at zero stock is not dead money.",
                "Click a band to see the lines in it, biggest value first.",
            ],
        )

    if kpi_id == "movement":
        rows, cov = await an.movement(period.start, period.end)
        bands = _band_summary(rows, "band", ("fast", "medium", "slow"), {
            "fast": "Top 20% by units per day", "medium": "Next 30%", "slow": "Bottom 50% of what sold",
        }, label_map={"fast": "Fast", "medium": "Medium", "slow": "Slow"})
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("klass", "Band", "left", "text", "movement-band", "name", "raw"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Lines", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("salesPct", "% of sales", "right", "percent"),
            ),
            rows=bands,
            emptyText="Nothing sold in this period.",
            notes=[
                f"Units sold per trading day across {num(cov.trading_days)} day(s), then banded by rank.",
                "A velocity question, not a value one. A cheap line can be the fastest mover in the shop and still sit in ABC class C — which is exactly why these are separate tiles.",
                "Lines that sold nothing at all are not ranked here; they are in Dead Stock.",
            ],
        )

    # ── performance ──────────────────────────────────────────────────────────
    if kpi_id == "top-products":
        rows = await ex.top_products(period, limit=100)
        sellers = await ex.top_sellers(period)
        for r in rows:
            r["marginPercent"] = (r["grossProfit"] / r["netSales"] * 100) if r["netSales"] else D0
            top = sellers.get(r["sku"])
            r["person"] = top["name"] if top else None
            r["personShare"] = top["share"] if top else None
            r["people"] = top["people"] if top else None
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("brand", "Brand", "left", "text", "brand-detail", "name"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
                ("person", "Sold most by", "left", "text", "cashier-product-detail", PERSON_ITEM),
                ("personShare", "Their share", "right", "percent"),
                ("people", "Sold by", "right", "number"),
            ),
            rows=rows,
            emptyText="No sales in this period.",
            notes=[
                "Top 100 by net sales. Click an item for who sold it and its day-by-day trend, or a category or brand to see its whole group.",
                "Sold most by is the person with the largest share of the item's net sales; Sold by counts everyone who sold it. Click the name for that person's sales of that item.",
                "Taxonomy comes from the branch catalog's own department / category / brand fields; anything unrecorded groups as Unclassified rather than being dropped.",
            ],
        )

    if kpi_id in ("top-categories", "top-brands"):
        attr = "category" if kpi_id == "top-categories" else "brand"
        target = "category-detail" if attr == "category" else "brand-detail"
        rows = await ex.top_by(period, attr, limit=40)
        for r in rows:
            r["marginPercent"] = (r["grossProfit"] / r["netSales"] * 100) if r["netSales"] else D0
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Category" if attr == "category" else "Brand", "left", "text", target, "name"),
                ("products", "Distinct items", "right", "number"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
            ),
            rows=rows,
            emptyText="No sales in this period.",
            notes=[
                f"Ranked by net sales. Click a {attr} to see every item inside it.",
                "Items with nothing recorded in this field group as Unclassified rather than being dropped — hiding them would understate the total.",
            ],
        )

    if kpi_id == "best-cashier":
        rows = await ex.cashier_ranking(period, limit=50)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Salesperson", "left", "text", "cashier-detail", "name"),
                ("branches", "Branch", "left", "mono"),
                ("invoices", "Invoices", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("share", "Share", "right", "percent"),
                ("avgBasket", "Avg basket", "right", "money"),
                ("distinctItems", "Items", "right", "number"),
                ("qty", "Units", "right", "number"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
                ("days", "Days worked", "right", "number"),
            ),
            rows=rows,
            emptyText="No sales rung in this period.",
            notes=[
                "Ranked by net sales rung. Average basket sits alongside because volume and value reward different behaviour — the top seller and the best upseller are often not the same person.",
                "Click a person for everything they sold, item by item and category by category, with their days, till closes, discounts and returns.",
                "Items, units and gross profit come from the lines on their bills; net sales and share come from the bill totals.",
                "This MVP has no salesperson separate from the cashier, so this is the only sales attribution available.",
            ],
        )

    if kpi_id == "branch-comparison":
        rows = await ex.branch_comparison(period)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Branch", "left", "text", "branch-detail", "code", "code"),
                ("code", "Code", "left", "mono"),
                ("netSales", "Net sales", "right", "money"),
                ("invoices", "Invoices", "right", "number"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("avgBasket", "Avg basket", "right", "money"),
                ("stockValue", "Stock value", "right", "money"),
                ("staffOnDuty", "Staff", "right", "number"),
                ("status", "Status", "left", "text"),
            ),
            rows=rows,
            notes=[
                "Every registered branch appears, including ones that have never reported — those show zeros with a status rather than being hidden, because an absent branch is a fact, not a blank.",
                "Click a branch for its own day-by-day trading.",
            ],
        )

    if kpi_id == "staffing":
        rows = await ex.staffing(period)
        spells = await ex.duty_people(period)
        known = any(r["onDuty"] is not None for r in rows)
        notes = [
            "On the floor is who a branch put on a counter — its own record, kept when a manager assigns "
            "somebody or a cashier opens a till there. Rang a sale is measured from the bills.",
            "Someone on a counter who rang nothing is not idle by itself: they may have been packing, "
            "restocking or covering a queue. It is the gap worth asking about, not an answer.",
            "This is not attendance. A rota, biometric clock-in and payroll belong to the HR module, which is not built.",
        ]
        if not known:
            notes.insert(0, "No branch has reported counter duty for this period yet, so only what was rung is known. "
                            "A branch reports duty once it is on the version with the Counter Board.")
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("branch", "Branch", "left", "text"),
                ("onDuty", "On the floor", "right", "number"),
                ("traded", "Rang a sale", "right", "number"),
                ("onDutyNoSale", "On, no sale", "right", "number"),
                ("hours", "Hours on counters", "right", "number"),
                ("counters", "Counters used", "right", "number"),
                ("invoices", "Invoices", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
            ),
            rows=rows,
            emptyText="No branch traded in this period.",
            sections=[_section(
                "Who was on which counter",
                _cols(
                    ("day", "Day", "left", "date", "day-detail", "day"),
                    ("name", "Person", "left", "text", "cashier-detail", "name"),
                    ("counter", "Counter", "left", "text"),
                    ("branch", "Branch", "left", "mono"),
                    ("hours", "Hours", "right", "number"),
                    ("spells", "Spells", "right", "number"),
                ),
                spells,
                "No branch has reported who stood at which counter yet.",
            )],
            notes=notes,
        )

    # ── supply ───────────────────────────────────────────────────────────────
    if kpi_id == "warehouse-status":
        wh = await ex.warehouse_status()
        rows = [{
            "transferNumber": t.transfer_number, "status": t.status,
            "vehicle": t.vehicle or "—", "driver": t.driver or "—",
            "dispute": "Open" if t.dispute_open else "—",
            "requestedAt": t.requested_at, "dispatchedAt": t.dispatched_at,
        } for t in wh["transfers"]]
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("transferNumber", "Transfer", "left", "mono"),
                ("status", "Status", "left", "text"),
                ("vehicle", "Vehicle", "left", "text"),
                ("driver", "Driver", "left", "text"),
                ("requestedAt", "Requested", "left", "datetime"),
                ("dispatchedAt", "Dispatched", "left", "datetime"),
                ("dispute", "Dispute", "left", "text"),
            ),
            rows=rows,
            emptyText="Nothing outbound from the godown.",
            notes=[
                f"Godown stock value {money(wh['stockValue'])} across live ledger lines; {wh['awaitingPick']} transfer(s) approved and waiting on a pick.",
                "Live — the Cloud's own data, not a branch report.",
            ],
        )

    if kpi_id == "purchase-status":
        rows, _ = await _grn_rows()
        ps = await ex.purchase_status()
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("grnNumber", "GRN", "left", "mono"),
                ("supplierName", "Supplier", "left", "text", "supplier-detail", "id", "supplierId"),
                ("partyInvNo", "Their invoice", "left", "text"),
                ("bin", "Into", "left", "text"),
                ("lines", "Lines", "right", "number"),
                ("units", "Units", "right", "number"),
                ("value", "Value", "right", "money"),
                ("approved", "Approved", "left", "text"),
                ("at", "Received", "left", "datetime"),
            ),
            rows=rows,
            emptyText="Nothing has been received into the godown yet.",
            notes=[
                f"{ps['pendingRequisitions']} requisition(s) pending, {ps['approvedRequisitions']} approved, {ps['rejectedRequisitions']} rejected.",
                "Every goods receipt, not a count. Click a supplier to see everything they have delivered.",
                "Live — from the godown's own receiving records.",
            ],
        )

    if kpi_id == "supplier-performance":
        rows = await ex.supplier_performance()
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Supplier", "left", "text", "supplier-detail", "id", "supplierId"),
                ("code", "Code", "left", "mono"),
                ("receipts", "Receipts", "right", "number"),
                ("units", "Units in", "right", "number"),
                ("bonusUnits", "Bonus units", "right", "number"),
                ("value", "Value received", "right", "money"),
                ("lastReceipt", "Last receipt", "left", "datetime"),
            ),
            rows=rows,
            notes=[
                "Only what the godown's receiving history can prove: volume, value and recency. Click a supplier for the individual lines.",
                "On-time delivery and quality scores need a promised date and a rejection reason on the GRN. Neither is recorded, so neither is shown rather than being estimated.",
                "Bonus units are free stock — they raise what is on the shelf without raising what was paid, which is why they are counted separately.",
            ],
        )

    # ── health ───────────────────────────────────────────────────────────────
    if kpi_id == "health-score":
        health = await ex.business_health(period)
        rows = [{"label": c.label, "score": c.score, "weight": f"{c.weight}%", "detail": c.detail}
                for c in health.components]
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("label", "Component", "left", "text"), ("score", "Score", "right", "number"),
                ("weight", "Weight", "right", "text"), ("detail", "Measured", "left", "text"),
            ),
            rows=rows,
            notes=[
                f"Overall {health.score}/100 — {health.band}. Each component scores 0–100 and contributes at its stated weight.",
                "Every component is measured from real records. Nothing here is forecast or AI-derived.",
            ],
        )

    if kpi_id == "live-alerts":
        alerts = await ex.live_alerts()
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("severity", "Severity", "left", "severity"), ("title", "What", "left", "text"),
                ("detail", "Detail", "left", "text"), ("source", "Source", "left", "source"),
            ),
            rows=alerts,
            emptyText="Nothing is asking for a decision right now.",
            notes=["Live items are true as of this moment; branch-snapshot items are as last reported."],
        )

    if kpi_id == "sync-health":
        rows = [{"name": b.name, "code": b.code, "status": b.status,
                 "lastSeenAt": b.last_seen_at, "syncUrl": b.sync_url or "not set"}
                for b in await ex.Branch.all()]
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Branch", "left", "text", "branch-detail", "code", "code"),
                ("code", "Code", "left", "mono"), ("status", "Status", "left", "text"),
                ("lastSeenAt", "Last reported", "left", "datetime"),
                ("syncUrl", "Endpoint", "left", "mono"),
            ),
            rows=rows,
            notes=[
                "Branch push is not built yet. Snapshots are imported by hand, so 'last reported' is when that import ran.",
                "The godown is not listed — it is this same Cloud service, so it is always current.",
            ],
        )

    if kpi_id == "net-profit":
        return KpiDetailOut(**base, notes=[
            "Net profit is worked out from the posted vouchers of head office and every branch whose books have reached head office.",
            "Open Accounts → Income Statement and choose Whole company for every line behind it, or one branch for that branch alone.",
        ])
    return KpiDetailOut(**base, notes=["No detailed breakdown has been built for this KPI yet."])


async def _grn_rows():
    """Goods receipts as table rows, newest first."""
    grns = await ex.GRN.all().prefetch_related("lines")
    suppliers = {str(s.id): s for s in await ex.Supplier.all()}
    bins = {str(b.id): b for b in await ex.Bin.all()}
    rows = []
    for g in grns:
        value = sum(((l.qty or D0) * (l.unit_price or D0) for l in g.lines), D0)
        units = sum(((l.qty or D0) + (l.bonus_qty or D0) for l in g.lines), D0)
        rows.append({
            "grnNumber": g.grn_number, "supplierId": str(g.supplier_id),
            "supplierName": suppliers[str(g.supplier_id)].name if str(g.supplier_id) in suppliers else str(g.supplier_id),
            "partyInvNo": g.party_inv_no or "—",
            "bin": bins[str(g.bin_id)].label if str(g.bin_id) in bins else str(g.bin_id),
            "lines": len(g.lines), "units": units, "value": value,
            "approved": "Yes" if g.approved else "Pending", "at": g.at,
        })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows, sum((r["value"] for r in rows), D0)


# ── focused (second-level) views ─────────────────────────────────────────────
async def _focused(kpi_id: str, period: ex.Period, focus: dict, as_of) -> KpiDetailOut:
    p_out = _period_out(period)

    if kpi_id in ("category-detail", "brand-detail"):
        attr = "category" if kpi_id == "category-detail" else "brand"
        name = focus.get("name", "")
        rows = await ex.products_where(period, attr, name, limit=200)
        sellers = await ex.top_sellers(period)
        for r in rows:
            top = sellers.get(r["sku"])
            r["person"] = top["name"] if top else None
            r["personShare"] = top["share"] if top else None
        people = await ex.people_where(period, attr, name)
        total = sum((r["netSales"] for r in rows), D0)
        profit = sum((r["grossProfit"] for r in rows), D0)
        head = _headline(
            kpi_id, name or f"(no {attr})", "Performance",
            f"Every item in the {attr} “{name}”, ranked by net sales, and who sells it.",
            money(total), "PKR", f"{len(rows)} item(s) · {money(profit)} gross profit · sold by {len(people)} person(s)",
            total, as_of=as_of)
        return KpiDetailOut(
            id=kpi_id, label=name or f"(no {attr})", group="Performance",
            hint=head.hint, focusLabel=name,
            parentKpi="top-categories" if attr == "category" else "top-brands",
            parentLabel="Top Categories" if attr == "category" else "Top Brands",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            tableTitle="Items",
            columns=_cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("brand" if attr == "category" else "category",
                 "Brand" if attr == "category" else "Category", "left", "text"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
                ("person", "Sold most by", "left", "text", "cashier-product-detail", PERSON_ITEM),
                ("personShare", "Their share", "right", "percent"),
            ),
            rows=rows,
            emptyText=f"Nothing in this {attr} sold during the period.",
            sections=[_section(
                f"Who sells this {attr}",
                _cols(
                    ("name", "Salesperson", "left", "text", "cashier-detail", "name"),
                    ("items", "Items", "right", "number"),
                    ("qty", "Units", "right", "number"),
                    ("netSales", "Net sales", "right", "money"),
                    ("share", f"Of the {attr}", "right", "percent"),
                    ("invoices", "Bills", "right", "number"),
                    ("grossProfit", "Gross profit", "right", "money"),
                    ("marginPercent", "Margin", "right", "percent"),
                ),
                people,
                _NO_PERSON_ITEMS if rows else f"Nothing in this {attr} sold during the period.",
            )],
            notes=[
                "Click an item for who sold it and its day-by-day trend; click a name for everything that person sold.",
                f"Bills counts each bill once per item, so a bill carrying two items of this {attr} is counted twice.",
            ],
        )

    if kpi_id == "product-detail":
        sku = focus.get("sku", "")
        days, totals = await ex.product_days(period, sku)
        people = await ex.product_people(period, sku)
        sold_by = await ex.product_day_people(period, sku)
        for d in days:
            d["soldBy"] = sold_by.get(d["day"])
            d["marginPercent"] = _money_pct(d["grossProfit"], d["netSales"])
        branches = await ex.product_branches(period, sku)
        people_net = sum((p["netSales"] for p in people), D0)
        lead = people[0] if people else None
        sub = f"{num(totals['qty'])} sold · {pct(totals['marginPercent'])} margin"
        if people:
            sub += f" · sold by {len(people)} person(s)"
        head = _headline(
            kpi_id, totals["name"], "Performance",
            f"Who sold {totals['name']}, how much each, and how it sold day by day.",
            money(totals["netSales"]), "PKR", sub, totals["netSales"], as_of=as_of)
        notes = [
            f"Category {totals['category'] or 'Unclassified'} · brand {totals['brand'] or 'Unclassified'}.",
            "The person is whoever rang the bill. Click a name for that person's sales of this item, day by day.",
        ]
        if lead:
            notes.append(f"{lead['name']} sold the most: {num(lead['qty'])} of {num(totals['qty'])} units, "
                         f"{pct(lead['share'])} of the value.")
        if people and abs(people_net - totals["netSales"]) >= Decimal("1"):
            notes.append(f"The people add up to {money(people_net)} against the item's {money(totals['netSales'])}: "
                         "a branch that has not yet sent who-sold-what is missing from the people.")
        notes.append("Only days on which it actually sold appear — a gap is a day with no sale, not missing data.")
        sections = [_section(
            "Day by day",
            _cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
                ("soldBy", "Sold by (units each)", "left", "text"),
            ),
            list(reversed(days)),
            "This item did not sell during the period.",
        )]
        if len(branches) > 1:
            sections.append(_section(
                "By branch",
                _cols(
                    ("code", "Branch", "left", "text", "branch-detail", "code"),
                    ("qty", "Qty sold", "right", "number"),
                    ("netSales", "Net sales", "right", "money"),
                    ("grossProfit", "Gross profit", "right", "money"),
                    ("marginPercent", "Margin", "right", "percent"),
                ),
                branches, "No branch sold it.",
            ))
        return KpiDetailOut(
            id=kpi_id, label=totals["name"], group="Performance", hint=head.hint,
            focusLabel=f"{totals['name']} ({sku})",
            parentKpi="top-products", parentLabel="Top Products",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=d["day"], value=d["netSales"]) for d in days],
            tableTitle="Who sold it",
            columns=_cols(
                ("name", "Salesperson", "left", "text", "cashier-product-detail", PERSON_ITEM),
                ("qty", "Qty sold", "right", "number"),
                ("qtyShare", "Of units", "right", "percent"),
                ("netSales", "Net sales", "right", "money"),
                ("share", "Of value", "right", "percent"),
                ("invoices", "Bills", "right", "number"),
                ("days", "Days", "right", "number"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
                ("branches", "Branch", "left", "mono"),
            ),
            rows=[{**p, "person": p["name"]} for p in people],
            emptyText=_NO_PERSON_ITEMS if days else "This item did not sell during the period.",
            sections=sections,
            notes=notes,
        )

    if kpi_id == "cashier-detail":
        name = focus.get("name", "")
        days, totals = await ex.cashier_days(period, name)
        closes = await ex.till_closes(period, cashier=name)
        variance = sum((c["variance"] for c in closes), D0)
        items = await ex.person_products(period, name)
        categories = await ex.person_categories(period, name)
        rung = await ex.overrides_rung(period, name)
        approved = await ex.discount_overrides(period, approver=name)
        returns = await ex.returns_handled(period, name)
        ranking = await ex.cashier_ranking(period, limit=1000)
        place = next((i for i, r in enumerate(ranking, 1) if r["name"] == name), None)
        me = ranking[place - 1] if place else None
        item_net = sum((i["netSales"] for i in items), D0)
        item_profit = sum((i["grossProfit"] for i in items), D0)
        units = sum((i["qty"] for i in items), D0)
        sub = f"{totals['invoices']} invoice(s) over {totals['days']} day(s)"
        if items:
            sub += f" · {len(items)} item(s), {num(units)} unit(s) · {money(item_profit)} gross profit"
        head = _headline(
            kpi_id, name, "Performance", f"Everything {name} sold in the period: item by item, category by category, and day by day.",
            money(totals["netSales"]), "PKR", sub, totals["netSales"], as_of=as_of)
        notes = []
        if me:
            notes.append(f"{_ordinal(place)} of {len(ranking)} by net sales rung, {pct(me['share'])} of everything rung in the period. "
                         f"Average basket {money(totals['avgBasket'])} across {totals['invoices']} invoice(s).")
        if items:
            best = items[0]
            notes.append(f"Biggest item: {best['name']}, {num(best['qty'])} unit(s) for {money(best['netSales'])} "
                         f"({pct(best['shareOfPerson'])} of their item sales). Margin on everything they sold {pct(_money_pct(item_profit, item_net))}.")
            notes.append("Of their sales is the item's part of this person's sales; Of item's sales is this person's part of that item's sales across everyone.")
        notes.append(f"{len(closes)} till close(s) in this period, net variance {money(variance)}."
                     if closes else "No till closes recorded against this person in the period.")
        notes.append("Net sales in the headline are bill totals; the item figures are the lines on those bills, so the two can differ by bill-level rounding.")
        sections = [
            _section(
                "Day by day",
                _cols(
                    ("day", "Day", "left", "date", "day-detail", "day"),
                    ("invoices", "Invoices", "right", "number"),
                    ("netSales", "Net sales", "right", "money"),
                    ("avgBasket", "Avg basket", "right", "money"),
                    ("items", "Items", "right", "number"),
                    ("qty", "Units", "right", "number"),
                    ("grossProfit", "Gross profit", "right", "money"),
                ),
                list(reversed(days)), "No sales rung by this person in the period.",
            ),
            _section(
                "By category",
                _cols(
                    ("name", "Category", "left", "text", "category-detail", "name"),
                    ("items", "Items", "right", "number"),
                    ("qty", "Units", "right", "number"),
                    ("netSales", "Net sales", "right", "money"),
                    ("share", "Of their sales", "right", "percent"),
                    ("grossProfit", "Gross profit", "right", "money"),
                    ("marginPercent", "Margin", "right", "percent"),
                ),
                categories, _NO_PERSON_ITEMS if days else "No sales rung by this person in the period.",
            ),
            _section(
                "Till closes",
                _cols(
                    ("sessionNumber", "Session", "left", "mono"),
                    ("day", "Day", "left", "date", "day-detail", "day"),
                    ("openedAt", "Opened", "left", "datetime"),
                    ("closedAt", "Closed", "left", "datetime"),
                    ("openingFloat", "Float", "right", "money"),
                    ("netCash", "Expected cash", "right", "money"),
                    ("countedCash", "Counted", "right", "money"),
                    ("variance", "Variance", "right", "money"),
                ),
                closes, "No till closed by this person in the period.",
            ),
            _section(
                "Discounts on their bills",
                _cols(
                    ("invoiceNumber", "Invoice", "left", "mono"),
                    ("at", "When", "left", "datetime"),
                    ("approvedBy", "Approved by", "left", "text", "cashier-detail", "name"),
                    ("gross", "Gross", "right", "money"),
                    ("discTotal", "Discount", "right", "money"),
                    ("discPercent", "Discount %", "right", "percent"),
                ),
                rung, "No manager-approved discounts on this person's bills in the period.",
            ),
        ]
        if approved:
            sections.append(_section(
                "Discounts they approved",
                _cols(
                    ("invoiceNumber", "Invoice", "left", "mono"),
                    ("at", "When", "left", "datetime"),
                    ("cashier", "Rung by", "left", "text", "cashier-detail", "name"),
                    ("gross", "Gross", "right", "money"),
                    ("discTotal", "Discount", "right", "money"),
                    ("discPercent", "Discount %", "right", "percent"),
                ),
                approved, "None.",
            ))
        sections.append(_section(
            "Returns they took",
            _cols(
                ("at", "When", "left", "datetime"),
                ("againstInvoice", "Against invoice", "left", "mono"),
                ("productName", "Item", "left", "text", "product-detail", "sku", "productSku"),
                ("qty", "Qty", "right", "number"),
                ("refundTotal", "Refund", "right", "money"),
            ),
            returns, "No returns taken by this person in the period.",
        ))
        return KpiDetailOut(
            id=kpi_id, label=name, group="Performance", hint=head.hint, focusLabel=name,
            parentKpi="best-cashier", parentLabel="Best Salesperson",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=d["day"], value=d["netSales"]) for d in days],
            tableTitle="What they sold",
            columns=_cols(
                ("name", "Item", "left", "text", "cashier-product-detail", PERSON_ITEM),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("shareOfPerson", "Of their sales", "right", "percent"),
                ("shareOfItem", "Of item's sales", "right", "percent"),
                ("invoices", "Bills", "right", "number"),
                ("days", "Days", "right", "number"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
            ),
            rows=items,
            emptyText=_NO_PERSON_ITEMS if days else "No sales rung by this person in the period.",
            sections=sections,
            notes=notes,
        )

    if kpi_id == "cashier-product-detail":
        name = focus.get("name", "")
        sku = focus.get("sku", "")
        days, t = await ex.person_product_days(period, name, sku)
        people = await ex.product_people(period, sku)
        label = f"{t['name']} by {name}"
        head = _headline(
            kpi_id, label, "Performance", f"How much of {t['name']} {name} sold, day by day, and how that compares with everyone else who sold it.",
            money(t["netSales"]), "PKR",
            f"{num(t['qty'])} sold over {t['invoices']} bill(s) on {t['days']} day(s) · {pct(t['shareOfItem'])} of the item's sales",
            t["netSales"], as_of=as_of)
        for d in days:
            d["marginPercent"] = _money_pct(d["grossProfit"], d["netSales"])
        place = next((i for i, p in enumerate(people, 1) if p["name"] == name), None)
        notes = [
            f"{name} sold {num(t['qty'])} of the {num(sum((p['qty'] for p in people), D0))} unit(s) everyone sold "
            f"({pct(t['qtyShareOfItem'])} of units, {pct(t['shareOfItem'])} of value)"
            + (f", {_ordinal(place)} of {len(people)} people who sold it." if place else "."),
            f"This item is {pct(t['shareOfPerson'])} of {name}'s item sales in the period. "
            f"Gross profit {money(t['grossProfit'])} at {pct(t['marginPercent'])} margin.",
            f"Category {t['category'] or 'Unclassified'} · brand {t['brand'] or 'Unclassified'}.",
        ]
        return KpiDetailOut(
            id=kpi_id, label=label, group="Performance", hint=head.hint,
            focusLabel=f"{t['name']} ({sku}) sold by {name}",
            parentKpi="cashier-detail", parentLabel=name,
            trail=[Crumb(label="Best Salesperson", kpi="best-cashier"),
                   Crumb(label=name, kpi="cashier-detail", focus={"name": name})],
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=d["day"], value=d["netSales"]) for d in days],
            tableTitle="Day by day",
            columns=_cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("invoices", "Bills", "right", "number"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("marginPercent", "Margin", "right", "percent"),
            ),
            rows=list(reversed(days)),
            emptyText=f"{name} did not sell this item during the period.",
            sections=[_section(
                "Everyone who sold it",
                _cols(
                    ("name", "Salesperson", "left", "text", "cashier-product-detail", PERSON_ITEM),
                    ("qty", "Qty sold", "right", "number"),
                    ("qtyShare", "Of units", "right", "percent"),
                    ("netSales", "Net sales", "right", "money"),
                    ("share", "Of value", "right", "percent"),
                    ("invoices", "Bills", "right", "number"),
                    ("days", "Days", "right", "number"),
                ),
                [{**p, "person": p["name"]} for p in people], _NO_PERSON_ITEMS,
                action=(f"Open {t['name']}", "product-detail", {"sku": sku}),
            )],
            notes=notes,
        )

    if kpi_id == "branch-detail":
        code = (focus.get("code", "") or "").upper()
        branch = await ex.branch_by_code(code)
        # A code nobody recognizes reads as a branch that has reported nothing, the way every other
        # focused view degrades. A 404 here loses the page and the breadcrumb back up with it.
        name = branch.name if branch else (code or "Unknown branch")
        totals = await ex.totals_for(period, str(branch.id)) if branch else ex.Totals()
        rows = await ex.daily_breakdown(period, str(branch.id)) if branch else []
        notes = [
            f"Registered {branch.code} · {branch.city or 'city not set'} · status {branch.status}.",
            "Last reported " + (branch.last_seen_at.strftime("%d %b %Y %H:%M") if branch.last_seen_at else "never") + ".",
        ] if branch else [
            "Nothing is registered under this code, so there are no figures to work out.",
            "Branch Comparison, above, lists every branch head office knows about.",
        ]
        head = _headline(
            kpi_id, name, "Performance", f"{name}'s own trading, day by day.",
            money(totals.net_sales), "PKR",
            f"{totals.invoices} invoice(s) · {money(totals.gross_profit)} gross profit",
            totals.net_sales, as_of=as_of)
        return KpiDetailOut(
            id=kpi_id, label=name, group="Performance", hint=head.hint,
            focusLabel=f"{branch.name} ({branch.code})" if branch else name,
            parentKpi="branch-comparison", parentLabel="Branch Comparison",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=d["day"], value=d["netSales"]) for d in reversed(rows)],
            columns=_cols(
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("netSales", "Net sales", "right", "money"),
                ("invoices", "Invoices", "right", "number"),
                ("avgBasket", "Avg basket", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("cashCollected", "Cash taken", "right", "money"),
                ("tillVariance", "Till variance", "right", "money"),
            ),
            rows=rows,
            emptyText=f"{name} has not reported any trading in this period." if branch
                      else f"No branch is registered with the code {name}.",
            notes=notes,
        )

    if kpi_id == "day-detail":
        day = focus.get("day", "")
        detail = await ex.day_detail(day)
        if not detail:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a date: {day}")
        t = detail["totals"]
        head = _headline(
            kpi_id, day, "Sales", f"Everything that happened on {day}.",
            money(t.net_sales), "PKR", f"{t.invoices} invoice(s)", t.net_sales, as_of=as_of)
        tender_line = ", ".join(f"{x['name']} {money(x['amount'])}" for x in detail["tenders"]) or "nothing tendered"
        the_day = date.fromisoformat(day)
        sellers = await ex.top_sellers(ex.Period("day", day, the_day, the_day, the_day, the_day, "the day before"))
        for r in detail["products"]:
            top = sellers.get(r["sku"])
            r["person"] = top["name"] if top else None
            r["personShare"] = top["share"] if top else None
        on_day = await ex.people_on_day(day)
        cashier_line = ", ".join(f"{c['name']} ({c['invoices']})" for c in detail["cashiers"]) or "nobody"
        return KpiDetailOut(
            id=kpi_id, label=day, group="Sales", hint=head.hint, focusLabel=day,
            parentKpi="sales-30d", parentLabel="Sales",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Invoices by hour",
            series=[SeriesPoint(day=f"{h['hour']:02d}", value=Decimal(h["invoices"]), label=f"{h['hour']:02d}h")
                    for h in detail["hours"]],
            columns=_cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("qty", "Qty sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("person", "Sold most by", "left", "text", "cashier-product-detail", PERSON_ITEM),
                ("personShare", "Their share", "right", "percent"),
            ),
            rows=detail["products"],
            emptyText="Nothing sold on this day.",
            tableTitle="What sold",
            sections=[_section(
                "Who was on",
                _cols(
                    ("name", "Salesperson", "left", "text", "cashier-detail", "name"),
                    ("invoices", "Invoices", "right", "number"),
                    ("billTotal", "Net sales", "right", "money"),
                    ("avgBasket", "Avg basket", "right", "money"),
                    ("items", "Items", "right", "number"),
                    ("qty", "Units", "right", "number"),
                    ("grossProfit", "Gross profit", "right", "money"),
                ),
                on_day, "Nobody rang a sale on this day.",
            )],
            notes=[
                f"On duty: {cashier_line}.",
                f"Paid by: {tender_line}.",
                f"Gross profit {money(t.gross_profit)} at {pct(t.margin_percent)} margin; {t.returns_count} return(s) worth {money(t.returns_value)}.",
                (f"{len(detail['closes'])} till close(s), net variance "
                 f"{money(sum((c['variance'] for c in detail['closes']), D0))}.") if detail["closes"] else "No till closed on this day.",
            ],
        )

    if kpi_id == "supplier-detail":
        supplier_id = focus.get("id", "")
        rows, totals = await ex.supplier_receipts(supplier_id)
        head = _headline(
            kpi_id, totals["name"], "Supply", f"Everything {totals['name']} has delivered into the godown.",
            money(totals["value"]), "PKR", f"{totals['receipts']} receipt(s)", totals["value"],
            source=LIVE)
        return KpiDetailOut(
            id=kpi_id, label=totals["name"], group="Supply", hint=head.hint,
            focusLabel=f"{totals['name']} ({totals['code'] or '—'})",
            parentKpi="supplier-performance", parentLabel="Supplier Performance",
            headline=head, period=p_out, source=LIVE, asOf=None,
            columns=_cols(
                ("grnNumber", "GRN", "left", "mono"),
                ("at", "Received", "left", "datetime"),
                ("partyInvNo", "Their invoice", "left", "text"),
                ("productName", "Item", "left", "text"),
                ("qty", "Qty", "right", "number"),
                ("bonusQty", "Bonus", "right", "number"),
                ("unitPrice", "Unit price", "right", "money"),
                ("lineValue", "Line value", "right", "money"),
                ("bin", "Into", "left", "text"),
            ),
            rows=rows,
            emptyText="This supplier has not delivered anything yet.",
            notes=[
                "Every line of every goods receipt from this supplier — live, from the godown's own records.",
                "Bonus units raise stock without raising what was paid, so they are shown separately from the paid quantity.",
            ],
        )

    # ── inside one class ─────────────────────────────────────────────────────
    if kpi_id in ("abc-class", "xyz-class", "movement-band"):
        wanted = (focus.get("name") or "").strip()
        if kpi_id == "abc-class":
            rows, _ = await an.abc(period.start, period.end)
            rows = [r for r in rows if r["abcClass"] == wanted.upper()]
            parent, parent_label = "abc-analysis", "ABC Classification"
            title = f"Class {wanted.upper()}"
            cols = _cols(
                ("rank", "#", "right", "number"),
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("brand", "Brand", "left", "text", "brand-detail", "name"),
                ("qty", "Units", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("sharePct", "% of sales", "right", "percent"),
                ("cumulativePct", "Cumulative", "right", "percent"),
                ("marginPct", "Margin", "right", "percent"),
            )
            notes = [
                "Ranked by net sales across the whole period, with the running cumulative share that put each line in this class.",
                "Click an item for its day-by-day trend, or a category or brand to see the whole group.",
            ]
        elif kpi_id == "xyz-class":
            rows, cov = await an.xyz(period.start, period.end)
            if cov.reason:
                rows = []
            rows = [r for r in rows if r["xyzClass"] == wanted.upper()]
            parent, parent_label = "xyz-analysis", "XYZ Demand Pattern"
            title = f"Class {wanted.upper()}"
            cols = _cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("avgDaily", "Avg units/day", "right", "number"),
                ("cv", "Variability", "right", "number"),
                ("sellingDays", "Days it sold", "right", "number"),
                ("coveragePct", "% of days", "right", "percent"),
                ("netSales", "Net sales", "right", "money"),
            )
            notes = [
                "Variability is the coefficient of variation — standard deviation of daily demand divided by its mean. Lower is steadier.",
                "'% of days' is how much of the period this line sold on at all. A line selling on 6% of days will always look erratic, and that is a true statement about it.",
            ]
        else:
            rows, _ = await an.movement(period.start, period.end)
            rows = [r for r in rows if r["band"] == wanted.lower()]
            parent, parent_label = "movement", "Fast & Slow Moving"
            title = f"{wanted.title()} moving"
            cols = _cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("perDay", "Units/day", "right", "number"),
                ("qty", "Units", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("abcClass", "ABC", "left", "text"),
            )
            notes = [
                "Ranked by units sold per trading day.",
                "The ABC column is carried alongside on purpose: a fast mover in class C is a cheap line doing volume, which is a different conversation from a fast mover in class A.",
            ]

        total = sum((Decimal(r.get("netSales") or 0) for r in rows), D0)
        head = _headline(kpi_id, title, "Stock", f"{len(rows)} line(s) in this band.",
                         money(total), "PKR", f"{num(len(rows))} line(s)", total)
        return KpiDetailOut(
            id=kpi_id, label=title, group="Stock", hint=head.hint,
            focusLabel=title, parentKpi=parent, parentLabel=parent_label,
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            columns=cols, rows=rows[:500],
            emptyText="No lines in this band for this period.",
            notes=notes + (["Showing the first 500 lines."] if len(rows) > 500 else []),
        )

    if kpi_id == "dead-stock-band":
        wanted = (focus.get("name") or "").strip()
        rows, _ = await an.dead_stock()
        rows = [r for r in rows if r["band"] == wanted]
        title = {"never-sold": "Never sold", "dead": f"No sale in {an.DEAD_DAYS}+ days",
                 "stale": f"No sale in {an.STALE_DAYS}-{an.DEAD_DAYS} days"}.get(wanted, wanted)
        total = sum((Decimal(r["valueCost"]) for r in rows), D0)
        head = _headline(kpi_id, title, "Stock", "Stock on hand that isn't moving, valued at cost.",
                         money(total), "PKR", f"{num(len(rows))} line(s)", total)
        return KpiDetailOut(
            id=kpi_id, label=title, group="Stock", hint=head.hint,
            focusLabel=title, parentKpi="dead-stock", parentLabel="Dead Stock",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            columns=_cols(
                ("name", "Item", "left", "text"),
                ("sku", "Code", "left", "mono"),
                ("branchName", "Where", "left", "text", "branch-detail", "code", "branchCode"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("qty", "On hand", "right", "number"),
                ("valueCost", "Value at cost", "right", "money"),
                ("valueRetail", "At retail", "right", "money"),
                ("daysSinceSale", "Days since sale", "right", "number"),
            ),
            rows=rows[:500],
            emptyText="Nothing in this band.",
            notes=[
                "Biggest tied-up value first — that is the order you would work the list in.",
                "'Days since sale' is blank for lines that have never sold at all.",
                "Valued at cost. The retail column is what the shelf says, which is the number that makes a markdown decision.",
            ] + (["Showing the 500 most valuable lines."] if len(rows) > 500 else []),
        )

    if kpi_id == "godown-stock":
        rows = await ex.godown_stock_rows(limit=300)
        total = sum((r["value"] for r in rows), D0)
        head = _headline(kpi_id, "Godown stock", "Stock", "Every godown line, valued at sale price.",
                         money(total), "PKR", f"{len(rows)} line(s)", total, source=LIVE)
        return KpiDetailOut(
            id=kpi_id, label="Godown stock", group="Stock", hint=head.hint,
            parentKpi="inventory-value", parentLabel="Inventory Value",
            headline=head, period=p_out, source=LIVE, asOf=None,
            columns=_cols(
                ("name", "Item", "left", "text"), ("sku", "Code", "left", "mono"),
                ("qty", "On hand", "right", "number"), ("price", "Sale price", "right", "money"),
                ("value", "Value", "right", "money"),
            ),
            rows=rows,
            emptyText="The godown holds no stock.",
            notes=["Live, folded from the godown's movement ledger. Capped at the 300 most valuable lines."],
        )

    if kpi_id == "credit-customers":
        rows = await ex.credit_customers()
        owed = sum((r["creditBalance"] for r in rows), D0)
        head = _headline(kpi_id, "Credit customers", "Money",
                         "Who owes the business money, and against what limit.",
                         money(owed), "PKR", f"{len(rows)} account(s)", owed, as_of=as_of)
        return KpiDetailOut(
            id=kpi_id, label="Credit customers", group="Money", hint=head.hint,
            parentKpi="customer-count", parentLabel="Customer Count",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            columns=_cols(
                ("name", "Customer", "left", "text"), ("code", "Code", "left", "mono"),
                ("creditLimit", "Limit", "right", "money"),
                ("creditBalance", "Owed", "right", "money"),
                ("headroom", "Headroom", "right", "money"),
                ("usedPercent", "Used", "right", "percent"),
            ),
            rows=rows,
            emptyText="No customer is on credit terms.",
            notes=["Trade credit is how wholesale works and how shops go under; headroom is what is left before the limit bites."],
        )

    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No KPI with id {kpi_id}")
