"""The KPI catalogue.

Each KPI is one builder function returning a tile, plus one returning its drill-down. Keeping
both next to each other is deliberate: a tile whose headline and whose detail page are computed
in different files is how the two quietly stop agreeing.

The KPI list comes from the Executive Dashboard section of the ERP blueprint (Step #2, "MODULES →
1. Executive Dashboard"). Everything the blueprint asks for that has no data behind it in this
MVP is returned in `notBuilt` rather than silently dropped or faked.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.core.pk_time import pk_time
from app.schemas.executive import (
    Crumb,
    DetailSection,
    ChartOut,
    ChartPoint,
    KpiDelta,
    KpiDetailOut as _KpiDetailOut,
    KpiListOut as _KpiListOut,
    KpiOut,
    KpiSectionOut as _KpiSectionOut,
    NotBuiltOut,
    PeriodOut,
    SeriesPoint,
    TableColumn,
)
from app.schemas.types import Money
from app.services import analytics_service as an
from app.services import executive_service as ex
from app.services import technical_reports_service as tr

D0 = Decimal("0")

LIVE = "live"
SNAPSHOT = "branch-snapshot"
BOOKS = "books"


# ── what a view carries beyond the shared shapes ────────────────────────────
# The branch a view is limited to, the ABC against XYZ boxes, and where a figure from the books sits in
# Accounts. Declared beside the only code that fills them, and the routes answer with these.
class BranchRef(BaseModel):
    code: str
    name: str


class MatrixCell(BaseModel):
    """One of the 9 boxes, or the "too new to tell" column beside them."""
    cell: str
    abc: str
    xyz: str
    items: int
    netSales: Money
    salesPct: float
    basisPct: float
    advice: str


class BooksLink(BaseModel):
    """The Income Statement behind a figure from the books: whose books, over which dates."""
    book: str
    start: date
    end: date


class KpiListOut(_KpiListOut):
    branch: BranchRef | None = None
    branches: list[BranchRef] = []


class KpiSectionOut(_KpiSectionOut):
    branch: BranchRef | None = None
    branches: list[BranchRef] = []


class KpiDetailOut(_KpiDetailOut):
    branch: BranchRef | None = None
    branches: list[BranchRef] = []
    periods: list[PeriodOut] = []
    # Set on the views that can rank Items by sales value, gross profit or units.
    basis: str | None = None
    bases: dict[str, str] = {}
    matrix: list[MatrixCell] = []
    booksLink: BooksLink | None = None


@dataclass(frozen=True)
class Scope:
    """What every figure on a page is limited to: the dates, one branch or all of them, and what ABC ranks by."""
    period: ex.Period
    branch: ex.Branch | None = None
    basis: str = "sales"

    @property
    def branch_id(self) -> str | None:
        return str(self.branch.id) if self.branch else None

    @property
    def code(self) -> str | None:
        return self.branch.code if self.branch else None

    @property
    def book(self) -> str:
        return self.branch.code if self.branch else "ALL"

    @property
    def where(self) -> str:
        return self.branch.name if self.branch else "every branch"


async def resolve_scope(period_id: str | None, start: str | datetime | None = None, end: str | datetime | None = None,
                        branch: str | None = None, basis: str | None = None) -> Scope:
    try:
        period = ex.resolve_period(period_id or "30d", start=start, end=end)
    except ex.ExecutiveError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    found = None
    wanted = (branch or "").strip().upper()
    if wanted and wanted != "ALL":
        found = await ex.branch_by_code(wanted)
        if not found:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No branch has the code {wanted}. Choose one from the branch list.")
    return Scope(period, found, an.basis_of(basis))


def _branch_ref(branch) -> BranchRef | None:
    return BranchRef(code=branch.code, name=branch.name) if branch else None


async def _branch_refs() -> list[BranchRef]:
    return [BranchRef(**b) for b in await an.branch_options()]


def _books_link(scope: Scope) -> BooksLink:
    return BooksLink(book=scope.book, start=scope.period.start, end=scope.period.end)


# ── formatting ──────────────────────────────────────────────────────────────
def money(v: Decimal | None) -> str:
    return f"Rs {(v or D0):,.0f}"


def num(v: Decimal | int | None) -> str:
    if v is None:
        return "0"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def qty(v: Decimal | int | None) -> str:
    """A quantity, always whole.

    Their shops sell loose goods by weight, so the figures carry real fractions: 9.5 of MEDA, 1.5 of BLACK CHANA.
    Shown as they are, a day read "13,616.72 item(s) sold", which is accurate and reads like a fault on a screen
    whose other counts are whole. Rounded half up, so nothing ever shows as 0 that was actually sold.

    This is a presentation rule only. Nothing is rounded before it is added up: the totals are summed from the exact
    figures and rounded once, at the end, where they are written out.
    """
    if v is None:
        return "0"
    whole = Decimal(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{whole:,}"


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
    return PeriodOut(id=p.id, label=p.label, start=p.start, end=p.end, compareLabel=p.compare_label,
                     atStart=p.at_start, atEnd=p.at_end,
                     basis="bills" if p.timed else "day",
                     wholeDay=list(ex.WHOLE_DAY_WHEN_TIMED) if p.timed else [])


def _prev(p: ex.Period) -> ex.Period:
    """The window every delta is measured against, carrying the same time of day.

    The times have to move with it. A window of 17:00 to 21:00 compared against the whole of the day before would
    put four hours beside twenty-four and call the difference a fall, which is how a dashboard teaches people to
    stop believing it. So the bounds shift by exactly the days between the two windows and keep their clock time.
    """
    shift = p.prev_start - p.start
    return ex.Period(p.id, p.label, p.prev_start, p.prev_end, p.prev_start, p.prev_end, p.compare_label,
                     at_start=p.at_start + shift if p.at_start else None,
                     at_end=p.at_end + shift if p.at_end else None)


# What the blueprint asks for that this MVP has no data for. Each says what module would supply
# it, so the gap reads as sequencing rather than omission.
NOT_BUILT = [
    NotBuiltOut(label="Online Orders", needs="Ecommerce module."),
    NotBuiltOut(label="Delivery Status", needs="Delivery / dispatch-to-customer module."),
    NotBuiltOut(label="AI Recommendations", needs="AI engine."),
    NotBuiltOut(label="Predictive Sales", needs="AI engine. Forecasting needs a trained model, not an average."),
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


async def list_kpis(period_id: str, *, start: str | datetime | None = None, end: str | datetime | None = None,
                    branch: str | None = None, sections: str | None = None) -> KpiListOut:
    """The whole grid, or only the sections named.

    `sections` is a comma separated list of `SECTIONS` keys. Left out, every section is built, so the answer is
    exactly what it has always been and no existing caller changes.

    It exists because the one caller left is the branch page's Sales tab, which displays the Sales and Money
    groups and nothing else -- 15 tiles of the 29 this builds. On Garden Town over all time that cost 27.41
    seconds against 0.46 for the two groups it shows, and since this endpoint carries the client's ordinary 15
    second timeout, the twelve month and all time views did not merely crawl, they failed.
    """
    wanted = None
    if sections:
        names = [n.strip() for n in sections.split(",") if n.strip()]
        unknown = [n for n in names if n not in SECTIONS]
        if unknown:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"No such section: {', '.join(unknown)}. "
                                f"Choose from {', '.join(SECTIONS)}.")
        wanted = names or None
    return await _grid(await resolve_scope(period_id, start, end, branch), sections=wanted)


async def section_for(name: str, period_id: str, *, start: str | datetime | None = None, end: str | datetime | None = None,
                      branch: str | None = None) -> KpiSectionOut:
    """One section, with the same period and branch handling as the whole grid."""
    return await section(await resolve_scope(period_id, start, end, branch), name)


# How the classification tiles read. One builder each, used by the grid and by the view the tile opens,
# so the two can't say different things about the same Items.
_BUCKET = {"day": ("day", "days"), "week": ("week", "weeks"), "month": ("month", "months"), None: ("day", "days")}


def _abc_tile(rows: list[dict], basis: str) -> dict:
    a_lines = [r for r in rows if r["abcClass"] == "A"]
    a_share = sum((Decimal(str(r["sharePct"])) for r in a_lines), D0)
    what = an.BASES[basis].lower()
    return dict(
        id="abc-analysis", label="ABC Classification", group="Stock",
        hint=f"Items ranked by {what}, largest first. A while the Items above carry under 80% of the total, B under 95%, C the rest. Where buying attention is worth spending.",
        value=Decimal(len(a_lines)), display=num(len(a_lines)), unit="count",
        sub=(f"A Items = {pct(a_share)} of {what}" if a_lines else "nothing sold in this period"),
        ctaLabel="See the classification")


def _xyz_tile(rows: list[dict], cov: an.Coverage) -> dict:
    one, many = _BUCKET[cov.bucket]
    base = dict(id="xyz-analysis", label="XYZ Demand Pattern", group="Stock",
                hint=f"How steadily each Item sells, {one} by {one}. X is steady, Y goes up and down, Z sells in bursts. Every {one} counts, including the ones it sold nothing in.")
    if not rows:
        return dict(base, display="Nothing sold", unit="text", sub="no sales in this period", ctaLabel="See the classification")
    if not cov.classified:
        # Not enough history for any Item. Say that, rather than printing classes computed from noise.
        return dict(base, display="Too new to tell", unit="text",
                    sub=f"no Item has {an.MIN_BUCKETS} {many} to go on yet", severity="watch", ctaLabel="Why not")
    steady = [r for r in rows if r["xyzClass"] == "X"]
    # "0 steady out of 424" is arithmetically right and tells an executive nothing. When nothing is
    # forecastable, the finding IS that nothing is forecastable. Lead with it.
    if steady:
        return dict(base, value=Decimal(len(steady)), display=num(len(steady)), unit="count",
                    sub=f"Items steady enough to plan on, of {num(cov.classified)}", ctaLabel="See the classification")
    return dict(base, value=D0, display="None steady", unit="count",
                sub=f"all {num(cov.classified)} Items sell too unevenly to forecast", severity="watch", ctaLabel="See the classification")


def _matrix_tile(merged: list[dict]) -> dict:
    ax = [r for r in merged if r["cell"] == "AX"]
    return dict(
        id="abc-xyz", label="ABC and XYZ Together", group="Stock",
        hint="Every Item in one of 9 boxes: how much it brings in (A, B, C) against how steadily it sells (X, Y, Z), with what to do about each box.",
        value=Decimal(len(ax)), display=num(len(ax)), unit="count",
        sub=(f"steady best sellers (AX) of {num(len(merged))} Items" if merged else "nothing sold in this period"),
        ctaLabel="See the 9 boxes")


def _sold_least_tile(unsold: int, value: Decimal) -> dict:
    return dict(
        id="sold-least", label="Sold Least", group="Stock",
        hint="The bottom of the list: the Items that sold least in the period, and the Items with stock on the shelf that did not sell at all.",
        value=Decimal(unsold), display=num(unsold), unit="count",
        sub=f"Items with stock that didn't sell · {money(value)} at cost",
        severity="watch" if unsold else "good", ctaLabel="See what sold least")


async def _statement(scope: Scope, start: date, end: date) -> dict:
    """The income statement for the scope's books: every book added together, or one branch's own."""
    from app.services import accounts_reports_service as books

    wanted, together = await books.resolve_books(scope.book)
    return await books.income_statement(wanted, together, start, end)


async def _statement_and_before(scope: Scope, start: date, end: date, prev_start: date, prev_end: date) -> tuple[dict, dict]:
    """Both income statements a "and what was it last period" tile needs.

    Two `_statement` calls read the chart of accounts twice for the same answer. See
    `accounts_reports_service.income_statement_pair`, which also records what was tried on the ledger itself and why
    it was not kept.
    """
    from app.services import accounts_reports_service as books

    wanted, together = await books.resolve_books(scope.book)
    return await books.income_statement_pair(wanted, together, start, end, prev_start, prev_end)


async def _grid(scope: Scope, *, include_charted: bool = False,
                sections: list[str] | None = None) -> KpiListOut:
    """The whole grid, every section, in one answer.

    Kept because `kpi_detail` needs it and because an existing caller should not break, but it is no longer what the
    dashboard asks for: see `section()` below and the note on `SECTIONS`.

    `include_charted` returns the chart-backed KPIs too, because `kpi_detail` needs them to build a headline for a
    drill-down that is no longer reachable from a tile, and skips the charts, which a drill-down never shows.
    """
    ctx = _Ctx(scope)
    items: list[KpiOut] = []
    for name, build in SECTIONS.items():
        if sections is not None and name not in sections:
            continue
        items += await build(ctx)
    return KpiListOut(
        period=_period_out(ctx.period),
        periods=[_period_out(ex.resolve_period(p)) for p in ex.PERIOD_IDS],
        businessDate=ex.business_today(),
        asOf=await ctx.as_of(),
        items=items if include_charted else [k for k in items if k.id not in CHART_BACKED],
        # The charts are narrowed with the tiles. Leaving them whole would have a caller asking for two sections
        # still pay for six sections' worth of ranked pictures, which it never displays.
        charts=[] if include_charted else await _build_charts(scope, only=set(sections) if sections else None),
        notBuilt=NOT_BUILT,
        branch=_branch_ref(scope.branch),
        branches=await _branch_refs(),
    )


async def section(scope: Scope, name: str) -> KpiSectionOut:
    """One section of the grid, on its own.

    WHY THE DASHBOARD ASKS SECTION BY SECTION
    -----------------------------------------
    Every tile used to be built by one call behind one loader, so the page showed nothing until the slowest tile in
    the slowest section finished, and a single failure took the whole dashboard with it. That was survivable while
    the books held 76,819 lines. Their real ledger is 5,343,432, and the money section went from under a second to
    over two minutes the day it arrived, taking Sales, Stock and everything else down with it.

    So each section is its own request with its own loader. Sales appears while Money is still counting, and a
    section that fails fails alone and says so in its own corner of the screen.

    It costs a little more in total: the period totals behind Sales and Money are read once per section that wants
    them rather than once for the page. `_Ctx` caches them within a request, and the figure that matters is how long
    before something is on screen, not how much work the server does in all.
    """
    wanted = name.strip().lower()
    build = SECTIONS.get(wanted)
    if build is None:
        raise ValueError(f"no section called {name!r}: it is one of {', '.join(SECTIONS)}")
    ctx = _Ctx(scope)
    items = await build(ctx)
    return KpiSectionOut(
        section=wanted,
        label=SECTION_LABELS[wanted],
        period=_period_out(ctx.period),
        periods=[_period_out(ex.resolve_period(p)) for p in ex.PERIOD_IDS],
        businessDate=ex.business_today(),
        asOf=await ctx.as_of(),
        notBuilt=NOT_BUILT,
        branches=await _branch_refs(),
        items=[k for k in items if k.id not in CHART_BACKED],
        # Only this section's charts, and only this section's queries to build them. Rankings is nothing but
        # charts: its five tiles are all chart-backed, so without these it would render as an empty heading.
        charts=await _build_charts(scope, only={wanted}),
        branch=_branch_ref(scope.branch),
    )


class _Ctx:
    """What more than one section needs, read once per request rather than once per section.

    The period totals are three queries and two of the six sections want them, so they are fetched when first asked
    for and not before: Supply and Health read neither, and used to pay for both.
    """

    def __init__(self, scope: Scope, only: set[str] | None = None):
        # `only` narrows the section to the tiles named, for a drill-down that wants exactly one of them. The
        # dashboard itself never passes it, so a section asked for in full is built in full.
        #
        # WHY THIS EXISTS. A drill-down resolves its headline by building the section that owns the tile
        # (`_headline_for`), which was already a large saving over building all thirty-six. It is not enough on the
        # Stock section, because that section is the dashboard's most expensive and its tiles are unrelated
        # questions: opening Stock Health, which shows Inventory Value and five rows, cost 3.24 seconds, of which
        # 0.67 was Inventory Value and 2.20 was nine tiles the page then threw away -- XYZ analysis alone was 1.27.
        #
        # The filter has to be read BEFORE the query, not when the tile is appended. `c.tile` runs after the work
        # is done, so skipping the append saves nothing; `wants`/`wants_any` are checked around the awaits.
        self.only = only
        self.scope = scope
        self.period = scope.period
        self.prev = _prev(scope.period)
        self.bid = scope.branch_id
        self.code = scope.code
        self.across = f"at {scope.branch.name}" if scope.branch else "across every branch that has reported"
        self._now = None
        self._before = None
        self._as_of = _UNSET

    async def now(self):
        if self._now is None:
            self._now = await ex.totals_for(self.period, self.bid)
        return self._now

    async def before(self):
        if self._before is None:
            self._before = await ex.totals_for(self.prev, self.bid)
        return self._before

    async def as_of(self):
        if self._as_of is _UNSET:
            self._as_of = await ex.snapshot_as_of(self.bid)
        return self._as_of

    def wants(self, kpi_id: str) -> bool:
        """Is this tile being asked for. True for everything when no filter was given."""
        return self.only is None or kpi_id in self.only

    def wants_any(self, *kpi_ids: str) -> bool:
        """For work several tiles share: do it if any one of them is wanted."""
        return self.only is None or any(i in self.only for i in kpi_ids)

    async def tile(self, items: list, **kw) -> None:
        kw.setdefault("source", SNAPSHOT)
        if "asOf" not in kw:
            kw["asOf"] = await self.as_of()
        kw.setdefault("ctaLabel", "See the detail")
        items.append(KpiOut(**kw))


_UNSET = object()


# ── the sections, one builder each ──────────────────────────────────────────

async def _sales_section(c: _Ctx) -> list[KpiOut]:
    items: list[KpiOut] = []
    now, before, period = await c.now(), await c.before(), c.period

    # The period the person actually picked, first, because the fixed tiles below cannot answer it. Pick three days,
    # or yesterday, or a week in March, and every other tile here still reads today, this week, this month, this
    # year. This one follows the picker.
    #
    # It also covers for Today's Sales, which reads zero for most of the working day: their shops send their figures
    # once the day is closed, so until that arrives today is genuinely empty and a dashboard whose headline is
    # today's sales shows a zero that is true and useless. Deliberately not in `_SALES_KPIS`, because the tiles in
    # there pin their drill-down to one fixed window and the whole point of this one is that it does not.
    if period.id != "today":
        await c.tile(items, id=PICKED_SALES, label=f"Sales, {period.label.lower()}", group="Sales",
                     hint=f"Net value of everything sold over {period.label.lower()}, {c.across}. "
                          f"This is the period chosen at the top of the page, not a fixed one.",
                     value=now.net_sales, display=money(now.net_sales), unit="PKR",
                     sub=f"{now.invoices} invoice(s) · {qty(now.items_sold)} item(s) sold",
                     delta=_delta(now.net_sales, before.net_sales, period.compare_label))

    today = ex.resolve_period("today")
    today_t = await ex.totals_for(today, c.bid)
    yday_t = await ex.totals_for(_prev(today), c.bid)
    await c.tile(items, id="sales-today", label="Today's Sales", group="Sales",
                 hint=f"Net value of everything sold today, {c.across}.",
                 value=today_t.net_sales, display=money(today_t.net_sales), unit="PKR",
                 sub=f"{today_t.invoices} invoice(s)",
                 delta=_delta(today_t.net_sales, yday_t.net_sales, "yesterday"))

    for pid, label in (("7d", "This Week"), ("30d", "This Month"), ("ytd", "This Year")):
        p = ex.resolve_period(pid)
        t = await ex.totals_for(p, c.bid)
        b = await ex.totals_for(_prev(p), c.bid)
        await c.tile(items, id=f"sales-{pid}", label=label, group="Sales",
                     hint=f"Net sales over {p.label.lower()}, compared with {p.compare_label}.",
                     value=t.net_sales, display=money(t.net_sales), unit="PKR",
                     sub=f"{t.invoices} invoice(s)", delta=_delta(t.net_sales, b.net_sales, p.compare_label))

    await c.tile(items, id="gross-profit", label="Gross Profit", group="Sales",
                 hint="Net sales minus the cost of what was sold. Expenses come off in Net Profit, from the books.",
                 value=now.gross_profit, display=money(now.gross_profit), unit="PKR",
                 sub=f"{pct(now.margin_percent)} margin",
                 delta=_delta(now.gross_profit, before.gross_profit, period.compare_label))

    # Net profit comes from the books (head office's and every branch's that has synced, or one branch's
    # own), not from the snapshot.
    statement, earlier = await _statement_and_before(c.scope, period.start, period.end, c.prev.start, c.prev.end)
    net_profit = Decimal(statement["netProfit"])
    expenses = Decimal(statement["operatingExpenses"]) + Decimal(statement["financialExpenses"])
    await c.tile(items, id="net-profit", label="Net Profit", group="Sales",
                 hint=(f"From {c.scope.branch.name}'s own books: its net sales, less the cost of what was sold, less every expense posted to them."
                       if c.scope.branch else
                       "From the books: net sales, less the cost of what was sold, less every expense posted, across head office and every branch whose books have synced."),
                 value=net_profit, display=money(net_profit), unit="PKR", source=BOOKS, asOf=None,
                 sub=f"{money(expenses)} expenses",
                 delta=_delta(net_profit, Decimal(earlier["netProfit"]), period.compare_label),
                 severity="bad" if net_profit < D0 else None, ctaLabel="See what's behind it")

    await c.tile(items, id="avg-basket", label="Average Basket", group="Sales",
                 hint="Net sales divided by the number of invoices: what a typical customer spends per visit.",
                 value=now.avg_basket, display=money(now.avg_basket), unit="PKR",
                 sub=f"across {now.invoices} invoice(s)",
                 delta=_delta(now.avg_basket, before.avg_basket, period.compare_label))

    await c.tile(items, id="customer-count", label="Customer Count", group="Sales",
                 hint="Bills rung in the period. Bills to named customers are counted separately below.",
                 value=Decimal(now.invoices), display=num(now.invoices), unit="count",
                 sub=f"{now.named_customers} to named customers",
                 delta=_delta(Decimal(now.invoices), Decimal(before.invoices), period.compare_label))

    await c.tile(items, id="bills-per-hour", label="Bills Per Hour", group="Sales",
                 hint="Invoices divided by measured trading hours (the gap between each branch's first and last sale, not an assumed shift length).",
                 value=now.bills_per_hour, display=f"{float(now.bills_per_hour):.1f}", unit="rate",
                 sub=f"{float(now.trading_hours):.0f} trading hour(s)",
                 delta=_delta(now.bills_per_hour, before.bills_per_hour, period.compare_label))

    # Returns are a Sales tile although their figures sit beside the money ones: a return comes straight off net
    # sales, so it is read next to sales rather than next to the drawer.
    await c.tile(items, id="returns", label="Returns", group="Sales",
                 hint="Goods brought back and refunded. Returns come straight off net sales, so a rising figure is margin leaving the business.",
                 value=now.returns_value, display=money(now.returns_value), unit="PKR",
                 sub=f"{now.returns_count} return(s)",
                 delta=_delta(now.returns_value, before.returns_value, period.compare_label, higher_is_better=False),
                 severity="watch" if now.returns_value > D0 else "good")

    staffing = await ex.staffing(period, c.bid)
    latest = staffing[0] if staffing else None
    # What the branch says it put on the counters, when it says anything; otherwise the old measure,
    # which can only see people who rang something.
    latest_staff = (latest["onDuty"] if latest and latest["onDuty"] is not None else (latest["traded"] if latest else 0)) or 0
    # Sits with Sales rather than in a Performance group of its own. Staff on Duty belongs next to Customer Count
    # and Bills Per Hour: they are all "what did the trading day look like".
    await c.tile(items, id="staffing", label="Staff on Duty", group="Sales",
                 hint="How many people were on a counter, per branch per day, and how many of them rang a sale.",
                 value=Decimal(latest_staff), display=num(latest_staff), unit="count",
                 sub=(f"on {date.fromisoformat(latest['day']):%d %b %Y} · {num(latest['traded'])} rang a sale" if latest and latest["onDuty"] is not None
                      else f"rang a sale on {date.fromisoformat(latest['day']):%d %b %Y}" if latest else "no trading days in this period"))
    return items


async def _money_section(c: _Ctx) -> list[KpiOut]:
    items: list[KpiOut] = []
    now, before, period = await c.now(), await c.before(), c.period

    await c.tile(items, id="cash-position", label="Cash Position", group="Money",
                 hint="Cash taken, plus drawer top-ups, less payouts. Credit sales are excluded because they never entered a drawer.",
                 value=now.net_cash, display=money(now.net_cash), unit="PKR",
                 sub=f"{money(now.credit_sales)} on credit",
                 delta=_delta(now.net_cash, before.net_cash, period.compare_label))

    overrides = await ex.discount_overrides(period, branch_id=c.bid)
    override_total = sum((o["discTotal"] for o in overrides), D0)
    approvers = {o["approvedBy"] for o in overrides if o["approvedBy"]}
    await c.tile(items, id="discount-overrides", label="Discount Overrides", group="Money",
                 hint="Discounts given above a cashier's own authority, and the manager who signed each one off. Margin given away on someone's signature.",
                 value=override_total, display=money(override_total), unit="PKR",
                 sub=f"{len(overrides)} sale(s) · {len(approvers)} approver(s)" if overrides else "none in this period",
                 severity="watch" if overrides else "good")

    variance_bad = abs(now.till_variance) > D0
    await c.tile(items, id="till-variance", label="Till Variance", group="Money",
                 hint="Counted cash minus what the system expected, across every till close in the period.",
                 value=now.till_variance, display=money(now.till_variance), unit="PKR",
                 sub=f"{now.tills_closed} till close(s)",
                 severity="bad" if variance_bad else "good")
    return items


async def _stock_section(c: _Ctx) -> list[KpiOut]:
    items: list[KpiOut] = []
    period, scope = c.period, c.scope

    if c.wants("inventory-value"):
        branch_stock = await ex.latest_stock_value(c.bid)
        if scope.branch:
            # One branch's floor. The godown is head office's, not the branch's, so it stays out.
            await c.tile(items, id="inventory-value", label="Inventory Value", group="Stock",
                         hint=f"{scope.branch.name}'s floor, valued at sale price, as last reported.",
                         value=branch_stock, display=money(branch_stock), unit="PKR", sub="branch floor only")
        else:
            godown_stock = await ex.godown_stock_value()
            await c.tile(items, id="inventory-value", label="Inventory Value", group="Stock",
                         hint="Branch floors plus the godown, valued at sale price. The godown half is live; the branch half is as last reported.",
                         value=branch_stock + godown_stock, display=money(branch_stock + godown_stock), unit="PKR",
                         sub=f"godown {money(godown_stock)}", source="mixed")

    # One fetch feeds three tiles, so it is skipped only when none of the three is wanted. When one is,
    # all three are built: they are free once the alerts are in hand, and `only` exists to skip QUERIES.
    if c.wants_any("out-of-stock", "low-stock", "expiring"):
        _, alert_totals = await ex.stock_alerts(branch_id=c.bid)
        godown = await ex.godown_alerts() if not scope.branch else {"outOfStock": [], "lowStock": []}
        oos = alert_totals.get("out-of-stock", 0) + len(godown["outOfStock"])
        low = alert_totals.get("low-stock", 0) + len(godown["lowStock"])
        expiring = alert_totals.get("near-expiry", 0) + alert_totals.get("expired", 0)
        stock_source = SNAPSHOT if scope.branch else "mixed"

        await c.tile(items, id="out-of-stock", label="Out of Stock", group="Stock",
                     hint="Lines showing zero or negative stock" + (f" at {scope.branch.name}." if scope.branch else ", across branch floors and the godown."),
                     value=Decimal(oos), display=num(oos), unit="count",
                     sub="on the branch floor" if scope.branch else f"{len(godown['outOfStock'])} in the godown",
                     severity="bad" if oos else "good", source=stock_source)

        await c.tile(items, id="low-stock", label="Low Stock", group="Stock",
                     hint=f"Lines below {ex.LOW_STOCK_THRESHOLD} units.",
                     value=Decimal(low), display=num(low), unit="count",
                     sub="on the branch floor" if scope.branch else f"{len(godown['lowStock'])} in the godown",
                     severity="watch" if low else "good", source=stock_source)

        await c.tile(items, id="expiring", label="Expiring & Expired", group="Stock",
                     hint="Batches already expired or within 30 days of expiry. For groceries and pharmacy this is where value is actually lost.",
                     value=Decimal(expiring), display=num(expiring), unit="count",
                     sub=f"{alert_totals.get('expired', 0)} already expired",
                     severity="bad" if alert_totals.get("expired") else ("watch" if expiring else "good"))

    # Different questions about the same shelf, kept apart on purpose: which Items earn the money (ABC),
    # which sell steadily (XYZ), both at once (the 9 boxes), which sold least or not at all, which have
    # stopped selling (dead stock), and which move fastest (velocity). Collapsing them into one
    # "product score" is how a flag becomes impossible to explain.
    abc_rows: list = []
    if c.wants_any("abc-analysis", "abc-xyz"):
        abc_rows, _ = await an.abc(period.start, period.end, c.code)
        if c.wants("abc-analysis"):
            await c.tile(items, **_abc_tile(abc_rows, "sales"))

    if c.wants_any("xyz-analysis", "abc-xyz"):
        xyz_rows, xyz_cov = await an.xyz(period.start, period.end, c.code)
        if c.wants("xyz-analysis"):
            await c.tile(items, **_xyz_tile(xyz_rows, xyz_cov))
        if c.wants("abc-xyz"):
            await c.tile(items, **_matrix_tile(an.merge_classes(abc_rows, xyz_rows)))

    if c.wants("sold-least"):
        await c.tile(items, **_sold_least_tile(*await an.unsold_on_shelf(period.start, period.end, c.code)))

    # Summary only: the tile needs three numbers, not 47,000 rows. See dead_stock_summary().
    if c.wants("dead-stock"):
        dead_summary = await an.dead_stock_summary(c.code)
        dead_value = sum((Decimal(v["value"]) for v in dead_summary.values()), D0)
        dead_lines = sum(v["lines"] for v in dead_summary.values())
        await c.tile(items, id="dead-stock", label="Dead Stock", group="Stock",
                     hint="Stock on the shelf that nothing is pulling through: never sold, or not sold in 90+ days. Valued at cost, because that is the money actually tied up.",
                     value=dead_value, display=money(dead_value), unit="PKR",
                     sub=f"{num(dead_lines)} line(s) not moving",
                     severity="bad" if dead_lines else "good", ctaLabel="See what's stuck")

    if c.wants("movement"):
        move_rows, _ = await an.movement(period.start, period.end, c.code)
        fast = [r for r in move_rows if r["band"] == "fast"]
        await c.tile(items, id="movement", label="Fast & Slow Moving", group="Stock",
                     hint="Units sold per trading day, banded. A shelf-space question, not a money question: a cheap line can be the fastest thing in the shop and still be C-class.",
                     value=Decimal(len(fast)), display=num(len(fast)), unit="count",
                     sub=(f"fast lines of {num(len(move_rows))}" if move_rows else "nothing sold in this period"),
                     ctaLabel="See the bands")
    return items


async def _performance_section(c: _Ctx) -> list[KpiOut]:
    items: list[KpiOut] = []
    period = c.period

    products = await ex.top_products(period, limit=1, branch_id=c.bid)
    await c.tile(items, id="top-products", label="Top Products", group="Performance",
                 hint="Ranked by net sales in the period.",
                 display=products[0]["name"] if products else "-", unit="text",
                 sub=money(products[0]["netSales"]) if products else "no sales in this period")

    cats = await ex.top_by(period, "category", limit=1, branch_id=c.bid)
    await c.tile(items, id="top-categories", label="Top Categories", group="Performance",
                 hint="Product categories ranked by net sales, using the catalog's own taxonomy.",
                 display=cats[0]["name"] if cats else "-", unit="text",
                 sub=money(cats[0]["netSales"]) if cats else "no sales in this period")

    brands = await ex.top_by(period, "brand", limit=1, branch_id=c.bid)
    await c.tile(items, id="top-brands", label="Top Brands", group="Performance",
                 hint="Brands ranked by net sales.",
                 display=brands[0]["name"] if brands else "-", unit="text",
                 sub=money(brands[0]["netSales"]) if brands else "no sales in this period")

    cashiers = await ex.cashier_ranking(period, limit=1, branch_id=c.bid)
    await c.tile(items, id="best-cashier", label="Best Cashier", group="Performance",
                 hint="Ranked by net sales rung in the period. The person who rings the sale is the only sales attribution that exists. There is no separate commission-carrying salesperson.",
                 display=cashiers[0]["name"] if cashiers else "-", unit="text",
                 sub=f"{money(cashiers[0]['netSales'])} · {cashiers[0]['invoices']} invoice(s)" if cashiers else "no sales in this period")

    comparison = await ex.branch_comparison(period)
    reporting = [b for b in comparison if b["reporting"]]
    await c.tile(items, id="branch-comparison", label="Branch Comparison", group="Performance",
                 hint="Every registered branch ranked by net sales. A branch that has never reported shows as such rather than as zero.",
                 display=comparison[0]["name"] if reporting else "-", unit="text",
                 sub=f"{len(reporting)} of {len(comparison)} branch(es) reporting")
    return items


async def _supply_section(c: _Ctx) -> list[KpiOut]:
    items: list[KpiOut] = []
    # The godown is head office's own, so these stay whole-company whichever branch is picked.
    wh = await ex.warehouse_status()
    await c.tile(items, id="warehouse-status", label="Warehouse Status", group="Supply",
                 hint="The godown right now: stock value, what is in flight, and what is stuck.",
                 value=wh["stockValue"], display=money(wh["stockValue"]), unit="PKR",
                 sub=f"{wh['inFlight']} in flight · {wh['disputesOpen']} dispute(s)",
                 severity="bad" if wh["disputesOpen"] else "good", source=LIVE, asOf=None)

    ps = await ex.purchase_status()
    await c.tile(items, id="purchase-status", label="Purchase Status", group="Supply",
                 hint="Goods received into the godown, and requisitions waiting on a decision.",
                 value=ps["receivedValue"], display=money(ps["receivedValue"]), unit="PKR",
                 sub=f"{ps['grnCount']} GRN(s) · {ps['pendingRequisitions']} requisition(s) pending",
                 severity="watch" if ps["pendingRequisitions"] else "good", source=LIVE, asOf=None)

    suppliers = await ex.supplier_performance()
    active_suppliers = [s for s in suppliers if s["receipts"]]
    await c.tile(items, id="supplier-performance", label="Supplier Performance", group="Supply",
                 hint="Receipts into the godown by supplier. On-time and quality scoring need a promised date and a rejection reason, neither of which is recorded yet.",
                 display=active_suppliers[0]["name"] if active_suppliers else "-", unit="text",
                 sub=f"{len(active_suppliers)} of {len(suppliers)} supplier(s) have delivered",
                 source=LIVE, asOf=None)
    return items


async def _health_section(c: _Ctx) -> list[KpiOut]:
    # Guarded tile by tile, because two of the four are expensive and a drill-down asks for one: the health
    # score is a weighted composite of six things and Live Alerts reads the warehouse, the godown and every
    # branch's stock alerts. Opening either one used to build all of them.
    items: list[KpiOut] = []
    if c.wants("health-score"):
        health = await ex.business_health(c.period, c.bid)
        await c.tile(items, id="health-score", label="Business Health Score", group="Health",
                     hint="A weighted composite of sales trend, margin, till accuracy, stock availability, supply friction and branch reporting. Nothing here is predicted. Every component is measured.",
                     value=Decimal(health.score), display=f"{health.score}", unit="score", sub=health.band,
                     severity="good" if health.score >= 80 else ("watch" if health.score >= 60 else "bad"),
                     source="mixed")

    if c.wants("live-alerts"):
        alerts = await ex.live_alerts()
        high = [a for a in alerts if a["severity"] == "high"]
        await c.tile(items, id="live-alerts", label="Live Alerts", group="Health",
                     hint="Everything currently asking for a decision, most serious first.",
                     value=Decimal(len(alerts)), display=num(len(alerts)), unit="count",
                     sub=f"{len(high)} need attention now" if high else "nothing urgent",
                     severity="bad" if high else ("watch" if alerts else "good"), source="mixed")

    if c.wants("sync-health"):
        # Selling branches only. With head office in here this read "5/5, all branches reporting", because the
        # nightly Multi-Tec run stamps last_seen_at on every book it feeds and head office is one -- so a row
        # that has never reported anything counted as reporting. See models.branch.selling_branches.
        branches = await ex.selling_branches()
        never = [b for b in branches if b.status == "active" and not b.last_seen_at]
        await c.tile(items, id="sync-health", label="Sync Health", group="Health",
                     hint="Which branches have reported, and how recently. A branch sends its own figures: events every couple of minutes, its whole picture every two hours.",
                     value=Decimal(len(branches) - len(never)), display=f"{len(branches) - len(never)}/{len(branches)}", unit="text",
                     sub=f"{len(never)} never reported" if never else "all branches reporting",
                     severity="watch" if never else "good", source=LIVE, asOf=None)

    if c.wants("branch-errors"):
        # This tile exists because the one above it gives false comfort. Sync Health answers "is the branch
        # pushing", which is the thing most likely to still be working when everything else is broken: a shop
        # whose sales screen threw an error all morning pushes its outbox perfectly and reads 4/4 here. The
        # branch now sends its own failures up the same road (see models.activity.failed_at_branch), so the
        # two tiles sit together and say different things.
        tally = await tr.failure_tally()
        failing = [b for b in tally["branches"] if b["total"]]
        worst = failing[0] if failing else None
        if tally["lastDay"]:
            sub = f"{num(tally['lastDay'])} in the last day"
            if worst:
                sub += f", worst {worst['name']}"
        elif worst:
            sub = f"none today; {worst['name']} last reported one"
        else:
            sub = f"nothing reported in {tally['days']} days"
        await c.tile(items, id="branch-errors", label="Branch Errors", group="Health",
                     hint=f"Failures the branches have reported in the last {tally['days']} days: a till or a report that "
                          f"would not answer, a screen that broke in the browser, and the work a branch does on a timer "
                          f"with nobody watching. Each carries the reference the person there was shown.",
                     value=Decimal(tally["total"]), display=num(tally["total"]), unit="count", sub=sub,
                     severity="bad" if tally["lastDay"] else ("watch" if tally["total"] else "good"),
                     source=LIVE, asOf=None)
    return items


# The order the dashboard shows them in, and the only place that order is written down. A tile belongs to the
# section its `group` names, which is not always the block of code that computes it: Returns and Staff on Duty are
# read next to Sales though their figures come from the money and performance queries.
SECTIONS = {
    "sales": _sales_section,
    "money": _money_section,
    "stock": _stock_section,
    "performance": _performance_section,
    "supply": _supply_section,
    "health": _health_section,
}

SECTION_LABELS = {
    "sales": "Sales",
    "money": "Money",
    "stock": "Stock",
    "performance": "Rankings & Distribution",
    "supply": "Supply",
    "health": "Health",
}

# ── drill-downs ─────────────────────────────────────────────────────────────
# Status words the tables show, never the stored codes ("in_transit", "active").
TRANSFER_STATUS_WORDS = {
    "requested": "Waiting for an answer", "approved": "Agreed, not sent yet", "dispatched": "On its way", "in_transit": "On its way",
    "received": "Received", "received_short": "Received short", "cancelled": "Cancelled",
}


def _branch_state(row: dict) -> str:
    if row.get("status") != "active":
        return "Switched off"
    return "Reporting" if row.get("reporting", row.get("lastSeenAt") is not None) else "Never reported"


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


async def _build_charts(scope: Scope, only: set[str] | None = None) -> list[ChartOut]:
    """The ranked pictures that sit under the tile grid.

    A tile answers "what is the number"; these answer "and then what" — second, third, fourth, all
    the way down the chart. Top Products as a tile can only ever name the winner, which tells an
    executive nothing about whether the winner is miles ahead or a nose in front. The bars make the
    shape of the distribution visible, which is the actual question.

    Every bar carries its own destination, so a chart is as navigable as a table: clicking the
    tallest bar and clicking the top row of the drill-down land in the same place.
    """
    def wanted(section: str) -> bool:
        """Whether this section's charts are being asked for. `only=None` is the whole page,
        which is what the old single request wanted."""
        return only is None or section in only

    charts: list[ChartOut] = []
    period, bid, code = scope.period, scope.branch_id, scope.code

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
    if wanted("sales"):
        hours = await ex.hourly_profile(period, bid)
        if hours:
            busiest = max(hours, key=lambda h: h["netSales"])["hour"]
            charts.append(ChartOut(
                id="sales-by-hour", title="Sales through the day",
                subtitle="Total taken in each hour of the day, across the period",
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
    if wanted("performance"):
        charts.append(bars(
            "chart-top-products", "Top products by value", await ex.top_products(period, limit=12, branch_id=bid),
            label_key="name", value_key="netSales", sub_key="category",
            link_to="product-detail", link_param="sku", link_key="sku",
            cta_kpi="top-products", cta_label="See all 100",
            empty="No sales in this period.",
            note="Click a bar for who sold it and its day-by-day trend.",
        ))
        charts.append(bars(
            "chart-top-categories", "Top categories", await ex.top_by(period, "category", limit=10, branch_id=bid),
            label_key="name", value_key="netSales",
            link_to="category-detail", link_param="name",
            cta_kpi="top-categories", cta_label="See every category",
            empty="No sales in this period.",
        ))
        charts.append(bars(
            "chart-top-brands", "Top brands", await ex.top_by(period, "brand", limit=10, branch_id=bid),
            label_key="name", value_key="netSales",
            link_to="brand-detail", link_param="name",
            cta_kpi="top-brands", cta_label="See every brand",
            empty="No sales in this period.",
        ))

    # ── who is performing ───────────────────────────────────────────────────
    if wanted("performance"):
        branches = await ex.branch_comparison(period)
        chart = bars(
            "chart-branches", "Branch comparison", branches,
            label_key="name", value_key="netSales", sub_key="code",
            link_to="branch-detail", link_param="code", link_key="code",
            cta_kpi="branch-comparison", cta_label="See the full comparison",
            empty="No branch has reported yet.",
            note="A branch that has never reported shows zero rather than being hidden, because silence is a finding.",
        )
        # Every branch stays on the comparison; the one picked stands out, so it is still read against the rest.
        for point, row in zip(chart.points, branches):
            if code and row["code"] == code:
                point.tone = "accent"
        charts.append(chart)
        charts.append(bars(
            "chart-staff", "Cashier performance", await ex.cashier_ranking(period, limit=12, branch_id=bid),
            label_key="name", value_key="netSales", sub_key="invoices",
            link_to="cashier-detail", link_param="name",
            cta_kpi="best-cashier", cta_label="See every cashier",
            empty="Nobody has rung a sale in this period.",
            note="Ranked by net sales rung. Click a bar for everything that person sold, their days and their till closes.",
        ))

    # ── how the catalog is shaped ───────────────────────────────────────────
    if wanted("stock"):
        abc_rows, _ = await an.abc(period.start, period.end, code)
        if abc_rows:
            summary = _band_summary(abc_rows, "abcClass", ("A", "B", "C"), {})
            charts.append(bars(
                "chart-abc", "ABC: where the value sits", summary,
                label_key="klass", value_key="netSales",
                link_to="abc-class", link_param="name", link_key="raw",
                subtitle="Net sales by class",
                cta_kpi="abc-xyz", cta_label="See the 9 boxes",
                empty="Nothing sold in this period.",
                note="Walking the Items from the biggest seller down: A while the Items above carry under 80% of sales, B under 95%, C the rest. Click a bar for its Items.",
            ))

        xyz_rows, xyz_cov = await an.xyz(period.start, period.end, code)
        one, many = _BUCKET[xyz_cov.bucket]
        if xyz_rows:
            summary = _band_summary(xyz_rows, "xyzClass", ("X", "Y", "Z", an.TOO_NEW), {}, label_map=XYZ_LABEL)
            charts.append(bars(
                "chart-xyz", "XYZ: how steadily Items sell", summary,
                label_key="klass", value_key="lines",
                link_to="xyz-class", link_param="name", link_key="raw",
                unit="count", fmt=lambda v: num(v),
                subtitle="Items by demand pattern",
                cta_kpi="xyz-analysis", cta_label="See the classification",
                empty="Nothing sold in this period.",
                note=f"Counted {one} by {one}, including the {many} an Item sold nothing in. X steady, Y up and down, Z in bursts. An Item needs {an.MIN_BUCKETS} {many} to go on.",
            ))

    return charts
    return charts


def _band_summary(rows: list[dict], key: str, order: tuple[str, ...], meanings: dict[str, str],
                  label_map: dict[str, str] | None = None) -> list[dict]:
    """Roll a classified product list up into one row per class.

    Both a share of *lines* and a share of *sales* are reported, because the gap between them is
    the finding. "12% of the lines earn 80% of the money" is the sentence somebody acts on; either
    number alone is just a count.
    """
    total_sales = sum((Decimal(r.get("netSales") or 0) for r in rows), D0)
    total_basis = sum((max(Decimal(r.get("basisValue") or 0), D0) for r in rows), D0)
    total_lines = len(rows) or 1
    out = []
    for klass in order:
        members = [r for r in rows if r.get(key) == klass]
        if not members:
            continue
        sales = sum((Decimal(r.get("netSales") or 0) for r in members), D0)
        profit = sum((Decimal(r.get("grossProfit") or 0) for r in members), D0)
        basis = sum((max(Decimal(r.get("basisValue") or 0), D0) for r in members), D0)
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
            # What the class carries on the basis it was ranked by, when that is not sales value.
            "basisValue": str(basis),
            "basisPct": float(basis / total_basis * 100) if total_basis else 0.0,
        })
    return out


ABC_MEANING = {"A": "The Items carrying the business", "B": "Solid middle", "C": "The long tail"}
XYZ_LABEL = {"X": "X", "Y": "Y", "Z": "Z", an.TOO_NEW: "Too new to tell"}


def _basis_cols(basis: str, value_key: str = "basisValue", share_key: str = "basisPct") -> list[tuple]:
    """The columns for what Items were ranked by, when that is not sales value (which every table shows anyway)."""
    if basis == "sales":
        return []
    what = an.BASES[basis]
    share = (share_key, f"% of {what.lower()}", "right", "percent")
    # Gross profit already has a column of its own in every table that ranks Items.
    return [share] if basis == "profit" else [(value_key, what, "right", "number"), share]


def _matrix_section(cells: list[dict]) -> DetailSection:
    """The boxes as a small table under ABC or XYZ: Items in each, each count opening its box."""
    rows = []
    for a in ("A", "B", "C"):
        row: dict = {"klass": f"Class {a}"}
        for c in (c for c in cells if c["abc"] == a):
            row[c["xyz"]] = c["items"]
            row[f"{c['xyz']}Cell"] = c["cell"]
        rows.append(row)
    return _section(
        "ABC against XYZ",
        _cols(
            ("klass", "Class", "left", "text"),
            ("X", "X steady", "right", "number", "matrix-cell", "name", "XCell"),
            ("Y", "Y up and down", "right", "number", "matrix-cell", "name", "YCell"),
            ("Z", "Z in bursts", "right", "number", "matrix-cell", "name", "ZCell"),
            (an.TOO_NEW, "Too new to tell", "right", "number", "matrix-cell", "name", f"{an.TOO_NEW}Cell"),
        ),
        rows, "Nothing sold in this period.", subtitle="Items in each box. Click a number for its Items.",
        action=("See the 9 boxes", "abc-xyz", {}),
    )


def _bucket_rule(kind: str | None) -> str:
    return {"day": "up to a month, so day by day",
            "week": "over a month and up to six months, so week by week",
            "month": "over six months, so month by month"}.get(kind or "day", "day by day")


def _cell_title(cell: str) -> str:
    return f"{cell[0]}, too new to tell" if cell.endswith("N") else f"Box {cell}"


def _xyz_meaning(many: str) -> dict[str, str]:
    return {"X": "Steady: plan on it", "Y": "Goes up and down: seasons or offers",
            "Z": "Sells in bursts: hard to forecast", an.TOO_NEW: f"Fewer than {an.MIN_BUCKETS} {many} to go on"}


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
    "abc-class", "xyz-class", "matrix-cell", "dead-stock-band", "movement-band",
}
# Views behind a tile that build their own headline, because it depends on what the Items are ranked by.
OWN_HEADLINE = {"abc-analysis", "xyz-analysis", "abc-xyz", "sold-least"}

_SALES_KPIS = {"sales-today": "today", "sales-7d": "7d", "sales-30d": "30d", "sales-ytd": "ytd"}
# The sales tile that follows the period picker instead of naming its own window. Kept out of `_SALES_KPIS` on
# purpose: everything in there resolves a fixed period, and resolving one for this would pin the drill-down to a
# window the person did not ask for. It reads the scope's period instead, and otherwise opens the same breakdown.
PICKED_SALES = "sales-period"


async def kpi_detail(kpi_id: str, period_id: str, focus: dict[str, str] | None = None, *,
                     start: str | datetime | None = None, end: str | datetime | None = None,
                     branch: str | None = None, basis: str | None = None) -> KpiDetailOut:
    focus = {k: v for k, v in (focus or {}).items() if v}
    if kpi_id == "branch-detail":
        # The branch this view is about is its scope, so every day, Item and person it opens stays at that
        # branch. A code nobody recognizes is not refused here: the view says there is no such branch.
        base_scope = await resolve_scope(period_id, start, end, None, basis)
        wanted = focus.get("code") or branch
        scope = Scope(base_scope.period, await ex.branch_by_code(wanted) if wanted else None, base_scope.basis)
    else:
        scope = await resolve_scope(period_id, start, end, branch, basis)
    as_of = await ex.snapshot_as_of(scope.branch_id)

    if kpi_id in FOCUSED or kpi_id in OWN_HEADLINE:
        out = await _focused(kpi_id, scope, focus, as_of)
    else:
        out = await _board_detail(kpi_id, scope)
    return await _finish(out, scope)


async def _finish(out: KpiDetailOut, scope: Scope) -> KpiDetailOut:
    """What every view carries, whatever it is: its branch, the pickers' choices, the way back up, and for a
    figure from the books, where it sits in Accounts."""
    out.branch = _branch_ref(scope.branch)
    out.branches = await _branch_refs()
    out.periods = [_period_out(ex.resolve_period(p)) for p in ex.PERIOD_IDS]
    if out.source == BOOKS and out.booksLink is None:
        out.booksLink = _books_link(scope)
    if scope.branch and out.id != "branch-detail":
        # Limited to a branch, the way back up starts at that branch.
        back = list(out.trail) or ([Crumb(label=out.parentLabel or out.parentKpi, kpi=out.parentKpi)] if out.parentKpi else [])
        out.trail = [Crumb(label=scope.branch.name, kpi="branch-detail", focus={"code": scope.branch.code})] + back
        if out.source == LIVE and out.group == "Supply":
            out.notes.insert(0, f"These are the godown's own records, for the whole company. {scope.branch.name}'s own buying "
                                "is in its books: open Accounts and choose its books.")
    return out


# Which section builds which tile. Generated by walking every section builder once and recording what it
# returned; `ops/checks/kpi_owners.py` rebuilds it and fails if it has drifted, so it cannot quietly go stale.
#
# WHY IT EXISTS
# -------------
# Opening any drill-down used to call `_grid(scope, include_charted=True)` purely to find ONE headline tile, and
# `_grid` builds the whole Company Dashboard: all six sections, all 36 tiles. The Branch Ranking page shows five
# rows that cost 0.33 seconds to work out and took 6.2 seconds to answer, because 6 of those seconds were
# building a dashboard nobody asked for and throwing 35 tiles away.
_KPI_OWNER = {
    "abc-analysis": "stock", "abc-xyz": "stock", "avg-basket": "sales", "best-cashier": "performance",
    "bills-per-hour": "sales", "branch-comparison": "performance", "cash-position": "money",
    "customer-count": "sales", "dead-stock": "stock", "discount-overrides": "money", "expiring": "stock",
    "gross-profit": "sales", "health-score": "health", "inventory-value": "stock", "live-alerts": "health",
    "low-stock": "stock", "movement": "stock", "net-profit": "sales", "out-of-stock": "stock",
    "purchase-status": "supply", "returns": "sales", "sales-30d": "sales", "sales-7d": "sales",
    "sales-period": "sales", "sales-today": "sales", "sales-ytd": "sales", "sold-least": "stock",
    "staffing": "sales", "supplier-performance": "supply", "sync-health": "health", "branch-errors": "health",
    "till-variance": "money",
    "top-brands": "performance", "top-categories": "performance", "top-products": "performance",
    "warehouse-status": "supply", "xyz-analysis": "stock",
}


async def _headline_for(kpi_id: str, scope: Scope):
    """The one tile a drill-down puts at the top of itself, built without building the other thirty-five.

    The section builders return their chart-backed tiles too -- it is `section()` that filters those out for the
    dashboard -- so asking the owning builder directly still finds the drill-downs the charts link to.

    An id that is not in the map falls back to the whole grid rather than failing. A new tile therefore works
    before anybody remembers this map exists; it is simply slow until `ops/checks/kpi_owners.py` is run and the
    map updated, and that check says exactly which id is missing.
    """
    owner = _KPI_OWNER.get(kpi_id)
    if owner is not None and owner in SECTIONS:
        # `only` so the section skips the queries behind the tiles this drill-down will not show. Opening Stock
        # Health built all ten Stock tiles to display one of them; XYZ analysis alone was 1.27s of that.
        items = await SECTIONS[owner](_Ctx(scope, only={kpi_id}))
        found = next((k for k in items if k.id == kpi_id), None)
        if found is not None:
            return found
        # The map named a section that no longer builds this tile. Fall through rather than answer 404: a stale
        # map should cost a slow page, never a broken one. ops/checks/kpi_owners.py exists to catch this.
    grid = await _grid(scope, include_charted=True)
    return next((k for k in grid.items if k.id == kpi_id), None)


async def _board_detail(kpi_id: str, scope: Scope) -> KpiDetailOut:
    period, bid = scope.period, scope.branch_id
    headline = await _headline_for(kpi_id, scope)
    if not headline:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No KPI with id {kpi_id}")
    # Period totals, for the drill-downs whose notes quote the surrounding figures.
    now = await ex.totals_for(period, bid)
    base = dict(id=kpi_id, label=headline.label, group=headline.group, hint=headline.hint,
                headline=headline, period=_period_out(period), source=headline.source, asOf=headline.asOf)

    # ── sales, by day — each day opens into its own hour-by-hour picture ──────
    if kpi_id in _SALES_KPIS or kpi_id == PICKED_SALES:
        # The fixed tiles each open their own window whatever the picker says; `sales-period` IS the picker, so its
        # window is the scope's own. That is the only difference between them, which is why it shares this builder
        # rather than having one of its own that could drift from it.
        window = period if kpi_id == PICKED_SALES else ex.resolve_period(_SALES_KPIS[kpi_id])
        series = await ex.daily_series(window, "net_sales", bid)
        rows = await ex.daily_breakdown(window, bid)
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
                "Expenses are not taken off here. Net Profit, from the books, does that.",
            ],
        )

    # ── gross profit — the question is which categories earn it ──────────────
    if kpi_id == "gross-profit":
        rows = await ex.top_by(period, "category", limit=40, branch_id=bid)
        for r in rows:
            r["marginPercent"] = (r["grossProfit"] / r["netSales"] * 100) if r["netSales"] else D0
        statement = await _statement(scope, period.start, period.end)
        sections, link = [], None
        if Decimal(statement["netSales"]) or Decimal(statement["costOfSales"]):
            link = _books_link(scope)
            sections.append(_section(
                "In the books",
                _cols(("line", "Line", "left", "text"), ("amount", "Amount", "right", "money")),
                [{"line": "Sales", "amount": statement["netSales"]},
                 {"line": "Cost of sales", "amount": statement["costOfSales"]},
                 {"line": "Gross profit", "amount": statement["grossProfit"]}],
                "Nothing is posted to the books for these dates.",
                subtitle=f"{statement['grossMargin']}% margin, from the posted vouchers",
            ))
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
            sections=sections,
            booksLink=link,
            notes=[
                "Ranked by gross profit contribution. Click a category to see the products inside it.",
                "Margin is gross profit as a share of that category's own net sales. A big earner on thin margin and a small one on fat margin are different problems.",
                "Cost comes from each product's weighted-average cost; operating expenses are not deducted.",
            ] + (["In the books is the same figure from the posted vouchers. It can differ from the branch figures above "
                  "until every day's sales are posted."] if sections else []),
        )

    # ── average basket — whose baskets, and how they differ ───────────────────
    if kpi_id == "avg-basket":
        rows = await ex.cashier_ranking(period, limit=50, branch_id=bid)
        days = await ex.daily_breakdown(period, bid)
        return KpiDetailOut(
            **base,
            seriesLabel="Average basket by day",
            series=[SeriesPoint(day=d["day"], value=d["avgBasket"]) for d in reversed(days)],
            columns=_cols(
                ("name", "Cashier", "left", "text", "cashier-detail", "name"),
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
        rows = await ex.credit_customers(bid)
        days = await ex.daily_breakdown(period, bid)
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
                "Credit accounts lists the customers who carry credit, the ones whose behaviour actually costs money if it changes.",
            ],
        )

    # ── bills per hour — the whole point is which hour ────────────────────────
    if kpi_id == "bills-per-hour":
        rows = await ex.hourly_profile(period, bid)
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
                "Bills/hour divides that hour's invoices by the number of days the branch actually traded in it, not by the whole period.",
            ],
        )

    # ── cash position — how customers actually paid ───────────────────────────
    if kpi_id == "cash-position":
        rows = await ex.tender_mix(period, bid)
        series = await ex.daily_series(period, "cash_collected", bid)
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
                f"Cash taken {money(now.cash_collected)}, drawer top-ups {money(now.cash_in)}, payouts {money(now.cash_out)}. Net cash {money(now.net_cash)}.",
                f"Credit sales of {money(now.credit_sales)} are excluded from cash: goods left the shop but nothing entered a drawer.",
                "Cash, card and credit have different consequences for working capital, which a single sales figure hides.",
            ],
        )

    # ── till variance — which drawer, whose shift ─────────────────────────────
    if kpi_id == "till-variance":
        rows = await ex.till_closes(period, branch_id=bid)
        worst = max((abs(r["variance"]) for r in rows), default=D0)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("sessionNumber", "Session", "left", "mono"),
                ("day", "Day", "left", "date", "day-detail", "day"),
                ("cashier", "Cashier", "left", "text", "cashier-detail", "name", "cashier"),
                ("openingFloat", "Float", "right", "money"),
                ("netCash", "Expected", "right", "money"),
                ("countedCash", "Counted", "right", "money"),
                ("variance", "Variance", "right", "money"),
            ),
            rows=rows,
            emptyText="No till has closed in this period.",
            notes=[
                "Every close, not a total, because a net-zero variance can hide a large over and a large under on the same day.",
                f"Largest single variance in this period: {money(worst)}.",
                "Click a cashier to see their whole record, or a day to see that day's trading.",
            ],
        )

    # ── discount overrides — margin given away, by name ───────────────────────
    if kpi_id == "discount-overrides":
        rows = await ex.discount_overrides(period, branch_id=bid)
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
                "The approver's name is recorded on the sale, not the cashier's, which is the point of the control.",
            ],
        )

    # ── returns ──────────────────────────────────────────────────────────────
    if kpi_id == "returns":
        rows = await ex.returns_detail(period, bid)
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
        branch_rows = [r for r in await ex.branch_comparison(period) if not scope.branch or r["code"] == scope.code]
        # Each row says where it drills to: a branch floor opens that branch's trading, the godown
        # opens its own ledger line by line.
        rows = [{"name": r["name"], "code": r["code"], "kind": "Branch floor",
                 "value": r["stockValue"], "target": "branch-detail", "targetValue": r["code"]}
                for r in branch_rows]
        if not scope.branch:
            rows.append({"name": "Godown (Warehouse)", "code": "-", "kind": "Godown",
                         "value": await ex.godown_stock_value(), "target": "godown-stock", "targetValue": "godown"})
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
                "The godown figure is live and can be broken down line by line (see Warehouse Status). Branch floors report a single total, so there is no per-item breakdown for them until sync carries it.",
            ],
        )

    # ── stock exceptions ─────────────────────────────────────────────────────
    if kpi_id in ("out-of-stock", "low-stock", "expiring"):
        kinds = {"out-of-stock": ["out-of-stock"], "low-stock": ["low-stock"],
                 "expiring": ["expired", "near-expiry"]}[kpi_id]
        rows: list[dict] = []
        for kind in kinds:
            items, _ = await ex.stock_alerts(kind, limit=300, branch_id=bid)
            rows.extend(items)
        if kpi_id in ("out-of-stock", "low-stock") and not scope.branch:
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
            emptyText="Nothing flagged. Stock is healthy on this measure.",
            notes=[
                "Branch rows are as last reported; godown rows are live.",
                "Capped at the most urgent 300 lines. The tile's count is the true total.",
            ],
        )

    # ── classification ───────────────────────────────────────────────────────
    if kpi_id == "dead-stock":
        summary = await an.dead_stock_summary(scope.code)
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
                "'Last sold' comes from each branch's full sales history, not from the window of days the Cloud holds. An item last sold eighteen months ago is invisible to a 30-day dataset and obvious to the branch, so the branch computes it.",
                "Only lines with stock actually on hand are counted. A discontinued line at zero stock is not dead money.",
                "Click a band to see the lines in it, biggest value first.",
            ],
        )

    if kpi_id == "movement":
        rows, cov = await an.movement(period.start, period.end, scope.code)
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
                "A velocity question, not a value one. A cheap line can be the fastest mover in the shop and still sit in ABC class C, which is exactly why these are separate tiles.",
                "Lines that sold nothing at all are not ranked here; they are in Dead Stock.",
            ],
        )

    # ── performance ──────────────────────────────────────────────────────────
    if kpi_id == "top-products":
        rows = await ex.top_products(period, limit=100, branch_id=bid)
        sellers = await ex.top_sellers(period, bid)
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
        rows = await ex.top_by(period, attr, limit=40, branch_id=bid)
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
                "Items with nothing recorded in this field group as Unclassified rather than being dropped, because hiding them would understate the total.",
            ],
        )

    if kpi_id == "best-cashier":
        rows = await ex.cashier_ranking(period, limit=50, branch_id=bid)
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Cashier", "left", "text", "cashier-detail", "name"),
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
                "Ranked by net sales rung. Average basket sits alongside because volume and value reward different behaviour. The top seller and the best upseller are often not the same person.",
                "Click a person for everything they sold, item by item and category by category, with their days, till closes, discounts and returns.",
                "Items, units and gross profit come from the lines on their bills; net sales and share come from the bill totals.",
                "This MVP has no salesperson separate from the cashier, so this is the only sales attribution available.",
            ],
        )

    if kpi_id == "branch-comparison":
        rows = [{**r, "status": _branch_state(r)} for r in await ex.branch_comparison(period)]
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
                "Every registered branch appears, including ones that have never reported. Those show zeros with a status rather than being hidden, because an absent branch is a fact, not a blank.",
                "Click a branch for its own day-by-day trading.",
            ],
        )

    if kpi_id == "staffing":
        rows = await ex.staffing(period, bid)
        spells = await ex.duty_people(period, branch_id=bid)
        known = any(r["onDuty"] is not None for r in rows)
        notes = [
            "On the floor is who a branch put on a counter. It is the branch's own record, kept when a manager assigns "
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
        # The one caller that really does want all of them: this is the table.
        wh = await ex.warehouse_status(transfers=True)
        rows = [{
            "transferNumber": t.transfer_number, "status": TRANSFER_STATUS_WORDS.get(t.status, t.status.replace("_", " ").capitalize()),
            "vehicle": t.vehicle or "-", "driver": t.driver or "-",
            "dispute": "Open" if t.dispute_open else "-",
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
                "Live: the Cloud's own data, not a branch report.",
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
                "Live, from the godown's own receiving records.",
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
                "Bonus units are free stock. They raise what is on the shelf without raising what was paid, which is why they are counted separately.",
            ],
        )

    # ── health ───────────────────────────────────────────────────────────────
    if kpi_id == "health-score":
        health = await ex.business_health(period, bid)
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
                f"Overall {health.score}/100 ({health.band}). Each component scores 0 to 100 and contributes at its stated weight.",
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
        # Selling branches, like the tile. These disagreed: the tile read 4/4 and this listed five, head office
        # among them, labelled "Never reported" -- which reads as a shop that has gone quiet rather than a row
        # that was never a shop. See models.branch.selling_branches.
        rows = [{"name": b.name, "code": b.code,
                 "status": _branch_state({"status": b.status, "lastSeenAt": b.last_seen_at}),
                 "lastSeenAt": b.last_seen_at, "syncUrl": b.sync_url or "not set"}
                for b in await ex.selling_branches()]
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("name", "Branch", "left", "text", "branch-detail", "code", "code"),
                ("code", "Code", "left", "mono"), ("status", "Status", "left", "text"),
                ("lastSeenAt", "Last reported", "left", "datetime"),
                ("syncUrl", "Sync address", "left", "mono"),
            ),
            rows=rows,
            notes=[
                "'Last reported' is when that branch last reached head office. A branch pushes its events every couple of "
                "minutes and its whole picture every two hours, and keeps trading meanwhile. Silence here means the link is "
                "down or the branch server is off, not that the shop has stopped.",
                "The godown is not listed. It is this same Cloud service, so it is always current.",
            ],
        )

    if kpi_id == "branch-errors":
        report = await tr.branch_errors()
        rows = [{
            "branch": b["name"], "code": b["code"], "what": s["action"],
            "where": s["path"], "count": s["count"], "status": s["status"],
            "lastAt": s["newestAt"], "reference": s["errorId"] or "-",
            "message": s["note"] or ("no detail was sent with it"),
            "person": s["person"] or ("nobody was watching" if s["background"] else "-"),
        } for b in report["branches"] for s in b["screens"]]
        rows.sort(key=lambda r: (-r["count"], r["branch"]))
        return KpiDetailOut(
            **base,
            columns=_cols(
                ("branch", "Branch", "left", "text", "branch-detail", "code", "code"),
                ("what", "What broke", "left", "text"), ("where", "Where", "left", "mono"),
                ("count", "Times", "right", "count"), ("lastAt", "Most recent", "left", "datetime"),
                ("person", "Who was there", "left", "text"), ("reference", "Reference", "left", "mono"),
                ("message", "What it said", "left", "text"),
            ),
            rows=rows,
            emptyText=f"No branch has reported a failure in the last {report['days']} days.",
            notes=[
                "A branch sends these itself, with the rest of its activity. There are three kinds: a request its "
                "server could not answer, a screen that broke in the browser, and the work it does on a timer with "
                "nobody watching -- that last kind has no person against it, which is exactly why it needs saying.",
                "The reference is the one the person at the branch was shown on screen, and the same one in that "
                "branch's own log file. It is what turns 'it said 7KQ2-M9XD' on the phone into the entry that "
                "explains it.",
                "Repeats are counted, not listed: the same thing breaking the same way is one line. The branch also "
                "holds its own repeats back, so 'Times' counts what it sent, not every attempt -- the message says "
                "when it stood in for more.",
                "This is not the same question as Sync Health. A branch can be pushing perfectly and be unusable.",
            ],
        )

    if kpi_id == "net-profit":
        from app.services import accounts_reports_service as books

        prev = _prev(period)
        now_st, before_st = await _statement_and_before(scope, period.start, period.end, prev.start, prev.end)
        lines = (("netSales", "Sales"), ("costOfSales", "Cost of sales"), ("grossProfit", "Gross profit"),
                 ("otherIncome", "Other income"), ("operatingExpenses", "Operating expenses"),
                 ("financialExpenses", "Financial expenses"), ("netProfit", "Net profit"))
        rows = [{"line": label, "now": now_st[key], "before": before_st[key],
                 "change": Decimal(now_st[key]) - Decimal(before_st[key])} for key, label in lines]
        sections = []
        if not scope.branch:
            names = {b.code: b.name for b in await ex.Branch.all()}
            by_book = []
            # Measured, 1 October 2026: do NOT collapse this into one grouped query. Grouping by book in a single
            # pass sounds obviously cheaper and is slower at every window, because each per-book call seeks cleanly
            # on idx_acc_vouchers_posted (status, book, date) while the grouped version reads every book's rows and
            # then groups them. Best of two, interleaved, figures identical either way:
            #     one month    loop 1.7s   one pass 1.7s
            #     one year     loop 13.5s  one pass 16.8s
            #     everything   loop 23.8s  one pass 28.8s
            for book in await books.all_books():
                st = await books.income_statement([book], False, period.start, period.end)
                by_book.append({
                    "book": book, "name": "Head office" if book == books.HEAD_OFFICE_BOOK else names.get(book, book),
                    # Only a branch's own books open as a branch; head office's are not a branch.
                    "branchCode": book if book in names else None,
                    "netSales": st["netSales"], "grossProfit": st["grossProfit"],
                    "expenses": Decimal(st["operatingExpenses"]) + Decimal(st["financialExpenses"]),
                    "netProfit": st["netProfit"],
                })
            sections.append(_section(
                "Book by book",
                _cols(
                    ("name", "Books", "left", "text", "net-profit", {"branch": "branchCode"}),
                    ("book", "Code", "left", "mono"),
                    ("netSales", "Sales", "right", "money"),
                    ("grossProfit", "Gross profit", "right", "money"),
                    ("expenses", "Expenses", "right", "money"),
                    ("netProfit", "Net profit", "right", "money"),
                ),
                by_book, "No books have anything posted for these dates.",
                subtitle="Click a branch for its own books",
            ))
        return KpiDetailOut(
            **base,
            tableTitle="From the books",
            columns=_cols(
                ("line", "Line", "left", "text"),
                ("now", period.label, "right", "money"),
                ("before", ex.date_range_label(prev.start, prev.end) if period.id != "all" else "Before", "right", "money"),
                ("change", "Change", "right", "money"),
            ),
            rows=rows,
            sections=sections,
            notes=[
                f"Worked out from the posted vouchers of {scope.branch.name}'s own books." if scope.branch else
                "Worked out from the posted vouchers of head office and every branch whose books have reached head office.",
                f"The second column is {period.compare_label}, for comparison." if period.id != "all" else
                "All time has no period before it, so Before is empty.",
                "Open in Accounts shows every line behind these figures on the Income Statement, for the same books and dates.",
            ],
        )
    return KpiDetailOut(**base, notes=["No detailed breakdown has been built for this KPI yet."])


async def _grn_rows():
    """Goods receipts as table rows, newest first."""
    from tortoise import Tortoise

    # `prefetch_related("lines")` names every receipt's id as a query parameter. That was fine while the godown
    # was empty and became a 500 the moment the legacy load landed: 5,968 receipts against SQL Server's ceiling
    # of 2,100 parameters, which is the same overflow that took out the dashboard, the purchase summary and the
    # reorder suggestions. The three figures this table shows are sums, so the database works them out and 92,509
    # line objects never cross into Python.
    totals = {
        str(r["grn_id"]): (int(r["lines"] or 0),
                           Decimal(str(r["units"] or 0)), Decimal(str(r["value"] or 0)))
        for r in await Tortoise.get_connection("default").execute_query_dict(
            "SELECT grn_id, COUNT(*) AS lines, "
            "       SUM(COALESCE(qty, 0) + COALESCE(bonus_qty, 0)) AS units, "
            "       SUM(COALESCE(qty, 0) * COALESCE(unit_price, 0)) AS value "
            "FROM warehouse_grn_lines GROUP BY grn_id")
    }
    grns = await ex.GRN.all()
    suppliers = {str(s.id): s for s in await ex.Supplier.all()}
    bins = {str(b.id): b for b in await ex.Bin.all()}
    rows = []
    for g in grns:
        line_count, units, value = totals.get(str(g.id), (0, D0, D0))
        rows.append({
            "grnNumber": g.grn_number, "supplierId": str(g.supplier_id),
            "supplierName": suppliers[str(g.supplier_id)].name if str(g.supplier_id) in suppliers else str(g.supplier_id),
            "partyInvNo": g.party_inv_no or "-",
            "bin": bins[str(g.bin_id)].label if str(g.bin_id) in bins else str(g.bin_id),
            "lines": line_count, "units": units, "value": value,
            "approved": "Yes" if g.approved else "Pending", "at": g.at,
        })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows, sum((r["value"] for r in rows), D0)


# ── focused (second-level) views ─────────────────────────────────────────────
async def _focused(kpi_id: str, scope: Scope, focus: dict, as_of) -> KpiDetailOut:
    period, bid, code = scope.period, scope.branch_id, scope.code
    p_out = _period_out(period)

    if kpi_id in ("category-detail", "brand-detail"):
        attr = "category" if kpi_id == "category-detail" else "brand"
        name = focus.get("name", "")
        rows = await ex.products_where(period, attr, name, limit=200, branch_id=bid)
        sellers = await ex.top_sellers(period, bid)
        for r in rows:
            top = sellers.get(r["sku"])
            r["person"] = top["name"] if top else None
            r["personShare"] = top["share"] if top else None
        people = await ex.people_where(period, attr, name, bid)
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
                    ("name", "Cashier", "left", "text", "cashier-detail", "name"),
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
        days, totals = await ex.product_days(period, sku, bid)
        people = await ex.product_people(period, sku, bid)
        sold_by = await ex.product_day_people(period, sku, bid)
        for d in days:
            d["soldBy"] = sold_by.get(d["day"])
            d["marginPercent"] = _money_pct(d["grossProfit"], d["netSales"])
        branches = await ex.product_branches(period, sku, bid)
        people_net = sum((p["netSales"] for p in people), D0)
        lead = people[0] if people else None
        sub = f"{qty(totals['qty'])} sold · {pct(totals['marginPercent'])} margin"
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
            notes.append(f"{lead['name']} sold the most: {qty(lead['qty'])} of {qty(totals['qty'])} units, "
                         f"{pct(lead['share'])} of the value.")
        if people and abs(people_net - totals["netSales"]) >= Decimal("1"):
            notes.append(f"The people add up to {money(people_net)} against the item's {money(totals['netSales'])}: "
                         "a branch that has not yet sent who-sold-what is missing from the people.")
        notes.append("Only days on which it actually sold appear, so a gap is a day with no sale, not missing data.")
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
        if branches and not scope.branch:
            sections.append(_section(
                "By branch",
                _cols(
                    # Into this same Item at that branch alone: its days and its people there.
                    ("branch", "Branch", "left", "text", "product-detail", {"sku": "sku", "branch": "code"}),
                    ("code", "Code", "left", "mono"),
                    ("qty", "Qty sold", "right", "number"),
                    ("netSales", "Net sales", "right", "money"),
                    ("grossProfit", "Gross profit", "right", "money"),
                    ("marginPercent", "Margin", "right", "percent"),
                ),
                branches, "No branch sold it.",
                subtitle="Click a branch for this Item there alone",
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
                ("name", "Cashier", "left", "text", "cashier-product-detail", PERSON_ITEM),
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
        days, totals = await ex.cashier_days(period, name, bid)
        closes = await ex.till_closes(period, cashier=name, branch_id=bid)
        variance = sum((c["variance"] for c in closes), D0)
        items = await ex.person_products(period, name, bid)
        categories = await ex.person_categories(period, name, bid)
        rung = await ex.overrides_rung(period, name, bid)
        approved = await ex.discount_overrides(period, approver=name, branch_id=bid)
        returns = await ex.returns_handled(period, name, bid)
        ranking = await ex.cashier_ranking(period, limit=1000, branch_id=bid)
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
            notes.append(f"Biggest item: {best['name']}, {qty(best['qty'])} unit(s) for {money(best['netSales'])} "
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
            parentKpi="best-cashier", parentLabel="Best Cashier",
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
        days, t = await ex.person_product_days(period, name, sku, bid)
        people = await ex.product_people(period, sku, bid)
        label = f"{t['name']} by {name}"
        head = _headline(
            kpi_id, label, "Performance", f"How much of {t['name']} {name} sold, day by day, and how that compares with everyone else who sold it.",
            money(t["netSales"]), "PKR",
            f"{qty(t['qty'])} sold over {t['invoices']} bill(s) on {t['days']} day(s) · {pct(t['shareOfItem'])} of the item's sales",
            t["netSales"], as_of=as_of)
        for d in days:
            d["marginPercent"] = _money_pct(d["grossProfit"], d["netSales"])
        place = next((i for i, p in enumerate(people, 1) if p["name"] == name), None)
        notes = [
            f"{name} sold {qty(t['qty'])} of the {qty(sum((p['qty'] for p in people), D0))} unit(s) everyone sold "
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
            trail=[Crumb(label="Best Cashier", kpi="best-cashier"),
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
                    ("name", "Cashier", "left", "text", "cashier-product-detail", PERSON_ITEM),
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
        code_asked = (focus.get("code", "") or "").upper()
        branch = scope.branch
        # A code nobody recognizes reads as a branch that has reported nothing, the way every other
        # focused view degrades. A 404 here loses the page and the breadcrumb back up with it.
        name = branch.name if branch else (code_asked or "Unknown branch")
        totals = await ex.totals_for(period, bid) if branch else ex.Totals()
        rows = await ex.daily_breakdown(period, bid) if branch else []
        notes = [
            f"Registered {branch.code} · {branch.city or 'city not set'} · status {branch.status}.",
            "Last reported " + (pk_time(branch.last_seen_at).strftime("%d %b %Y %H:%M") if branch.last_seen_at else "never") + ".",
            "Everything this page opens stays at this branch: its days, its Items, its people and its classes. "
            "Choose All branches above to go back to the whole company.",
        ] if branch else [
            "Nothing is registered under this code, so there are no figures to work out.",
            "Branch Comparison, above, lists every branch head office knows about.",
        ]
        sections = []
        if branch:
            items = await ex.top_products(period, limit=15, branch_id=bid)
            people = await ex.cashier_ranking(period, limit=30, branch_id=bid)
            categories = await ex.top_by(period, "category", limit=15, branch_id=bid)
            abc_rows, _ = await an.abc(period.start, period.end, branch.code)
            least = await an.sold_least(period.start, period.end, branch.code, limit=10)
            sections = [
                _section(
                    "Items", _cols(
                        ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                        ("category", "Category", "left", "text", "category-detail", "name"),
                        ("qty", "Qty sold", "right", "number"),
                        ("netSales", "Net sales", "right", "money"),
                        ("grossProfit", "Gross profit", "right", "money"),
                    ), items, "Nothing sold here in this period.",
                    subtitle="The 15 biggest sellers", action=("See every Item", "top-products", {})),
                _section(
                    "People", _cols(
                        ("name", "Cashier", "left", "text", "cashier-detail", "name"),
                        ("invoices", "Invoices", "right", "number"),
                        ("netSales", "Net sales", "right", "money"),
                        ("share", "Share", "right", "percent"),
                        ("avgBasket", "Avg basket", "right", "money"),
                        ("grossProfit", "Gross profit", "right", "money"),
                        ("days", "Days worked", "right", "number"),
                    ), people, "Nobody rang a sale here in this period.",
                    subtitle="Their sales at this branch only", action=("See everyone", "best-cashier", {})),
                _section(
                    "Categories", _cols(
                        ("name", "Category", "left", "text", "category-detail", "name"),
                        ("products", "Items", "right", "number"),
                        ("netSales", "Net sales", "right", "money"),
                        ("grossProfit", "Gross profit", "right", "money"),
                    ), categories, "Nothing sold here in this period.",
                    action=("See every category", "top-categories", {})),
                _section(
                    "ABC: where the value sits", _cols(
                        ("klass", "Class", "left", "text", "abc-class", "name", "raw"),
                        ("meaning", "What it means", "left", "text"),
                        ("lines", "Items", "right", "number"),
                        ("netSales", "Net sales", "right", "money"),
                        ("salesPct", "% of sales", "right", "percent"),
                    ), _band_summary(abc_rows, "abcClass", ("A", "B", "C"), ABC_MEANING), "Nothing sold here in this period.",
                    action=("See the 9 boxes", "abc-xyz", {})),
                _section(
                    "Sold least", _cols(
                        ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                        ("qty", "Qty sold", "right", "number"),
                        ("netSales", "Net sales", "right", "money"),
                        ("onHand", "On hand", "right", "number"),
                        ("cell", "Box", "left", "text", "matrix-cell", "name"),
                    ), least["sold"], "Nothing sold here in this period.",
                    subtitle=f"{num(least['unsoldCount'])} more Item(s) with stock did not sell at all",
                    action=("See what sold least", "sold-least", {})),
            ]
        head = _headline(
            kpi_id, name, "Performance", f"{name}'s own trading: its days, Items, people and classes.",
            money(totals.net_sales), "PKR",
            f"{totals.invoices} invoice(s) · {money(totals.gross_profit)} gross profit",
            totals.net_sales, as_of=as_of)
        return KpiDetailOut(
            id=kpi_id, label=name, group="Performance", hint=head.hint,
            focusLabel=f"{branch.name} ({branch.code})" if branch else name,
            parentKpi="branch-comparison", parentLabel="Branch Comparison",
            # Back up to the comparison of every branch, not this one alone.
            trail=[Crumb(label="Branch Comparison", kpi="branch-comparison", focus={"branch": ""})],
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            seriesLabel="Net sales by day",
            series=[SeriesPoint(day=d["day"], value=d["netSales"]) for d in reversed(rows)],
            tableTitle="Day by day",
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
            sections=sections,
            notes=notes,
        )

    if kpi_id == "day-detail":
        day = focus.get("day", "")
        detail = await ex.day_detail(day, bid)
        if not detail:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a date: {day}")
        t = detail["totals"]
        head = _headline(
            kpi_id, day, "Sales", f"Everything that happened on {day}" + (f" at {scope.branch.name}." if scope.branch else "."),
            money(t.net_sales), "PKR",
            # How much went out of the door, not only how many times the till opened. Their own sale report is
            # a list of quantities, so a day that reads 1,514 invoices and nothing else cannot be checked
            # against it without adding the lines up by hand.
            f"{t.invoices} invoice(s) · {qty(t.items_sold)} item(s) sold", t.net_sales, as_of=as_of)
        tender_line = ", ".join(f"{x['name']} {money(x['amount'])}" for x in detail["tenders"]) or "nothing tendered"
        the_day = date.fromisoformat(day)
        sellers = await ex.top_sellers(ex.Period("day", day, the_day, the_day, the_day, the_day, "the day before"), bid)
        for r in detail["products"]:
            top = sellers.get(r["sku"])
            r["person"] = top["name"] if top else None
            r["personShare"] = top["share"] if top else None
        on_day = await ex.people_on_day(day, bid)
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
                    ("name", "Cashier", "left", "text", "cashier-detail", "name"),
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
            focusLabel=f"{totals['name']} ({totals['code'] or '-'})",
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
                "Every line of every goods receipt from this supplier, live from the godown's own records.",
                "Bonus units raise stock without raising what was paid, so they are shown separately from the paid quantity.",
            ],
        )

    # ── ABC, XYZ, the 9 boxes and Sold least ─────────────────────────────────
    # These build their own headline because it depends on what the Items are ranked by, and each carries
    # `basis` so the page can offer sales value, gross profit or units.
    ranked = dict(basis=scope.basis, bases=an.BASES)
    what = an.BASES[scope.basis].lower()
    item_note = f"Items ranked by {what}, largest first. Walking down the list, an Item is A while the Items above it carry less than 80% of the total, B while they carry less than 95%, and C after that. The Item that takes the total past 80% is still A: it is the one that got it there."

    if kpi_id == "abc-analysis":
        rows, cov = await an.abc(period.start, period.end, code, scope.basis)
        xyz_rows, _ = await an.xyz(period.start, period.end, code)
        cells = an.matrix_cells(an.merge_classes(rows, xyz_rows))
        head = KpiOut(**_abc_tile(rows, scope.basis), source=SNAPSHOT, asOf=as_of)
        return KpiDetailOut(
            id=kpi_id, label=head.label, group=head.group, hint=head.hint, headline=head, period=p_out,
            source=SNAPSHOT, asOf=as_of, **ranked,
            columns=_cols(
                ("klass", "Class", "left", "text", "abc-class", "name", "raw"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Items", "right", "number"),
                ("linesPct", "% of Items", "right", "percent"),
                *_basis_cols(scope.basis),
                ("netSales", "Net sales", "right", "money"),
                ("salesPct", "% of sales", "right", "percent"),
                ("grossProfit", "Gross profit", "right", "money"),
            ),
            rows=_band_summary(rows, "abcClass", ("A", "B", "C"), ABC_MEANING),
            emptyText="Nothing sold in this period, so there is nothing to rank.",
            sections=[_matrix_section(cells)] if rows else [],
            notes=[
                item_note,
                "Rank by sales value, gross profit or units with the switch above. By value, a pharmacy line selling four boxes at Rs 3,000 outranks a sweet selling four hundred at Rs 5; by units it is the other way round.",
                f"Covers {num(cov.products)} Item(s) that sold across {num(cov.trading_days)} trading day(s). Items that sold nothing in the period are not ranked: Sold Least lists them.",
                "Click a class to see every Item in it.",
            ] + ([cov.reason] if cov.reason and rows else []),
        )

    if kpi_id == "xyz-analysis":
        rows, cov = await an.xyz(period.start, period.end, code)
        abc_rows, _ = await an.abc(period.start, period.end, code, scope.basis)
        cells = an.matrix_cells(an.merge_classes(abc_rows, rows))
        one, many = _BUCKET[cov.bucket]
        head = KpiOut(**_xyz_tile(rows, cov), source=SNAPSHOT, asOf=as_of)
        return KpiDetailOut(
            id=kpi_id, label=head.label, group=head.group, hint=head.hint, headline=head, period=p_out,
            source=SNAPSHOT, asOf=as_of, **ranked,
            columns=_cols(
                ("klass", "Class", "left", "text", "xyz-class", "name", "raw"),
                ("meaning", "What it means", "left", "text"),
                ("lines", "Items", "right", "number"),
                ("linesPct", "% of Items", "right", "percent"),
                ("netSales", "Net sales", "right", "money"),
                ("salesPct", "% of sales", "right", "percent"),
            ),
            rows=_band_summary(rows, "xyzClass", ("X", "Y", "Z", an.TOO_NEW), _xyz_meaning(many), label_map=XYZ_LABEL),
            emptyText="Nothing sold in this period.",
            sections=[_matrix_section(cells)] if rows else [],
            notes=[
                f"Sales are counted {one} by {one}: this period is {_bucket_rule(cov.bucket)}. Weeks and months are counted back from the last day of the period, so the latest one is always whole.",
                f"Each Item is measured from the later of the first day of the period and its own first sale, and every {one} counts, including the {many} it sold nothing in. An Item that sells forty in one {one} and nothing in the other twelve sells in bursts, and leaving out the empty {many} would call it steady.",
                f"Variability is how far each {one}'s sales swing around the Item's own average (the standard deviation divided by the average). X is 0.5 or less, Y up to 1.0, Z above that.",
                f"An Item with fewer than {an.MIN_BUCKETS} {many} to go on is too new to tell, rather than being called erratic on a handful of points.",
                "Read it alongside ABC: an A Item that is also Z (depended on and hard to forecast) is the one worth a conversation.",
            ],
        )

    if kpi_id in ("abc-xyz", "matrix-cell"):
        merged, cov, xyz_cov = await an.combined(period.start, period.end, code, scope.basis)
        cells = an.matrix_cells(merged)
        one, many = _BUCKET[xyz_cov.bucket]
        cols = _cols(
            ("rank", "#", "right", "number"),
            ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
            ("sku", "Code", "left", "mono"),
            ("category", "Category", "left", "text", "category-detail", "name"),
            ("cell", "Box", "left", "text", "matrix-cell", "name"),
            ("qty", "Units", "right", "number"),
            ("netSales", "Net sales", "right", "money"),
            ("grossProfit", "Gross profit", "right", "money"),
            ("sharePct", f"% of {what}", "right", "percent"),
            ("cv", "Variability", "right", "number"),
            ("bucketsSold", f"{many.title()} it sold", "right", "number"),
            ("buckets", f"{many.title()} counted", "right", "number"),
        )
        box_notes = [
            f"Each box crosses how much an Item brings in (A, B, C, by {what}) with how steadily it sells (X, Y, Z, counted {one} by {one}). Too new to tell is an Item with fewer than {an.MIN_BUCKETS} {many} to go on.",
            item_note,
            f"Steadiness is counted {one} by {one} because this period is {_bucket_rule(xyz_cov.bucket)}. X varies by 0.5 or less around its own average, Y up to 1.0, Z more.",
            "Share of sales is the box's part of net sales in the period, whatever the Items are ranked by.",
        ]
        if kpi_id == "abc-xyz":
            head = KpiOut(**_matrix_tile(merged), source=SNAPSHOT, asOf=as_of)
            return KpiDetailOut(
                id=kpi_id, label=head.label, group=head.group, hint=head.hint, headline=head, period=p_out,
                source=SNAPSHOT, asOf=as_of, **ranked,
                parentKpi="abc-analysis", parentLabel="ABC Classification",
                matrix=[MatrixCell(**c) for c in cells] if merged else [],
                tableTitle="Every Item and its box",
                columns=cols, rows=merged[:500],
                emptyText="Nothing sold in this period, so there is nothing to sort into boxes.",
                notes=box_notes + ["Click a box for its Items, or an Item for who sold it and how it sold day by day."]
                      + (["Showing the first 500 Items."] if len(merged) > 500 else []),
            )
        wanted = (focus.get("name") or "").strip().upper()
        cell = next((c for c in cells if c["cell"] == wanted), None)
        rows = [r for r in merged if r["cell"] == wanted]
        total = sum((Decimal(r["netSales"]) for r in rows), D0)
        title = _cell_title(wanted) if cell else (wanted or "No box")
        head = _headline(kpi_id, title, "Stock", cell["advice"] if cell else "There is no box with that name.",
                         money(total), "PKR",
                         f"{num(len(rows))} Item(s) · {pct(cell['salesPct'] if cell else 0)} of sales", total, as_of=as_of)
        return KpiDetailOut(
            id=kpi_id, label=title, group="Stock", hint=head.hint, focusLabel=title,
            parentKpi="abc-xyz", parentLabel="ABC and XYZ Together",
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of, **ranked,
            tableTitle="Items in this box",
            columns=cols, rows=rows[:500],
            emptyText="No Items in this box for this period.",
            notes=([f"What to do: {cell['advice']}"] if cell else []) + box_notes[:1]
                  + (["Showing the first 500 Items."] if len(rows) > 500 else []),
        )

    if kpi_id == "sold-least":
        data = await an.sold_least(period.start, period.end, code, scope.basis)
        head = KpiOut(**_sold_least_tile(data["unsoldCount"], Decimal(data["unsoldValue"])), source=SNAPSHOT, asOf=as_of)
        unsold_cols = [
            ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
            ("sku", "Code", "left", "mono"),
            ("category", "Category", "left", "text", "category-detail", "name"),
            ("onHand", "On hand", "right", "number"),
            ("valueCost", "At cost", "right", "money"),
            ("valueRetail", "At retail", "right", "money"),
            ("lastSoldAt", "Last sold", "left", "datetime"),
        ] + ([] if scope.branch else [("branches", "Where", "left", "mono")])
        return KpiDetailOut(
            id=kpi_id, label=head.label, group=head.group, hint=head.hint, headline=head, period=p_out,
            source=SNAPSHOT, asOf=as_of, **ranked,
            tableTitle=f"Sold, but least ({what}, smallest first)",
            columns=_cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("qty", "Units sold", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
                ("grossProfit", "Gross profit", "right", "money"),
                ("sellingDays", "Days it sold", "right", "number"),
                ("onHand", "On hand", "right", "number"),
                ("cell", "Box", "left", "text", "matrix-cell", "name"),
            ),
            rows=data["sold"],
            emptyText="Nothing sold in this period.",
            sections=[_section(
                "On the shelf, not sold in this period", _cols(*unsold_cols), data["unsold"],
                "Every Item holding stock sold at least once in this period.",
                subtitle=f"{num(data['unsoldCount'])} Item(s) · {money(Decimal(data['unsoldValue']))} at cost · most money first",
            )],
            notes=[
                f"The top table is every Item that sold in these dates, the smallest {what} first, with what is still on the shelf. It is the other end of Top Products.",
                "The second table is Items with stock that did not sell at all in these dates, the most money at cost first. They never appear in a sales figure, so no view that starts from sales can find them.",
                "On hand is as each branch last reported it, not as it stood on the last day of the period. Last sold comes from the branch's whole history, so it can be from before these dates.",
                "Box is the Item's place in the 9 boxes of ABC against XYZ. Click it for the other Items in the same box.",
            ] + (["Each table shows its first 500 Items."] if max(data["soldCount"], data["unsoldCount"]) > 500 else []),
        )

    # ── inside one class ─────────────────────────────────────────────────────
    if kpi_id in ("abc-class", "xyz-class", "movement-band"):
        wanted = (focus.get("name") or "").strip()
        if kpi_id == "abc-class":
            rows, _ = await an.abc(period.start, period.end, code, scope.basis)
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
                ("grossProfit", "Gross profit", "right", "money"),
                ("sharePct", f"% of {what}", "right", "percent"),
                ("beforePct", "Above it", "right", "percent"),
                ("cumulativePct", "Running total", "right", "percent"),
                ("marginPct", "Margin", "right", "percent"),
            )
            notes = [
                item_note,
                "Above it is the share of the total carried by every Item ranked higher, which is what decides the class. Running total includes the Item itself.",
                "Click an item for its day-by-day trend, or a category or brand to see the whole group.",
            ]
        elif kpi_id == "xyz-class":
            rows, cov = await an.xyz(period.start, period.end, code)
            klass = an.TOO_NEW if wanted.lower() == an.TOO_NEW else wanted.upper()
            rows = [r for r in rows if r["xyzClass"] == klass]
            one, many = _BUCKET[cov.bucket]
            parent, parent_label = "xyz-analysis", "XYZ Demand Pattern"
            title = XYZ_LABEL.get(klass, klass) if klass == an.TOO_NEW else f"Class {klass}"
            cols = _cols(
                ("name", "Item", "left", "text", "product-detail", "sku", "sku"),
                ("sku", "Code", "left", "mono"),
                ("category", "Category", "left", "text", "category-detail", "name"),
                ("since", "Counted from", "left", "date"),
                ("buckets", f"{many.title()} counted", "right", "number"),
                ("bucketsSold", f"{many.title()} it sold", "right", "number"),
                ("coveragePct", f"% of {many}", "right", "percent"),
                ("avgPerBucket", f"Avg units a {one}", "right", "number"),
                ("cv", "Variability", "right", "number"),
                ("netSales", "Net sales", "right", "money"),
            )
            notes = [
                f"Counted {one} by {one} from the later of the period's first day and the Item's own first sale. Variability is the standard deviation of its {one}ly sales divided by their average: lower is steadier.",
                f"'% of {many}' is how much of that time the Item sold at all. An Item selling in one {one} out of ten will always look uneven, and that is a true statement about it.",
            ]
        else:
            rows, _ = await an.movement(period.start, period.end, code)
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
        head = _headline(kpi_id, title, "Stock", f"{len(rows)} Item(s) in this band.",
                         money(total), "PKR", f"{num(len(rows))} Item(s)", total)
        return KpiDetailOut(
            id=kpi_id, label=title, group="Stock", hint=head.hint,
            focusLabel=title, parentKpi=parent, parentLabel=parent_label,
            headline=head, period=p_out, source=SNAPSHOT, asOf=as_of,
            **(ranked if kpi_id == "abc-class" else {}),
            columns=cols, rows=rows[:500],
            emptyText="No Items in this band for this period.",
            notes=notes + (["Showing the first 500 Items."] if len(rows) > 500 else []),
        )

    if kpi_id == "dead-stock-band":
        wanted = (focus.get("name") or "").strip()
        rows, _ = await an.dead_stock(code)
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
                "Biggest tied-up value first, because that is the order you would work the list in.",
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
        rows = await ex.credit_customers(bid)
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
