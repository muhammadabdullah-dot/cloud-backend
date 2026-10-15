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

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from tortoise.exceptions import ValidationError
from tortoise.functions import Count, Sum

from app.core.pk_time import PKT, today_pk
from app.core import dialect
from app.models import (
    Bin,
    Branch,
    selling_branches,
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
# local day. Using the server's UTC date would roll the business date over five hours early (core/pk_time.py).


def business_today() -> date:
    return today_pk()


# ── periods ─────────────────────────────────────────────────────────────────
class ExecutiveError(Exception):
    """Something a person asked for that can't be answered as asked, said so they can fix it."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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
    # The window as moments, filled only when the caller picked a time. Whole days leave these None and every fold
    # below behaves exactly as it did before, which is what keeps this change invisible to a screen that sends
    # dates. See `totals_for` for what a time changes and `core/when.py` for why it can be answered at all.
    at_start: datetime | None = None
    at_end: datetime | None = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def timed(self) -> bool:
        """Did the caller ask for part of a day? Midnight to midnight is whole days, not a time."""
        return self.at_start is not None or self.at_end is not None


def months_back(d: date, n: int) -> date:
    """The same day n months earlier, pulled back to that month's last day when it is shorter."""
    y, m = divmod(d.month - 1 - n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _before(start: date, end: date) -> tuple[date, date]:
    """The window of the same length ending the day before `start`."""
    length = end - start
    prev_end = start - timedelta(days=1)
    return prev_end - length, prev_end


def date_range_label(start: date, end: date) -> str:
    """Dates the way people say them (1 Aug to 15 Sep 2026), with the year once when both ends share it."""
    if start == end:
        return f"{start.day} {start:%b %Y}"
    left = f"{start.day} {start:%b}" if start.year == end.year else f"{start.day} {start:%b %Y}"
    return f"{left} to {end.day} {end:%b %Y}"


def parse_day(value: str | None, which: str) -> date:
    return parse_moment(value, which)[0]


def parse_moment(value: str | date | datetime | None, which: str) -> tuple[date, datetime | None]:
    """A bound as the day it falls on, and as a moment when one was given.

    `2026-10-12` is a whole day and the second value is None. `2026-10-12T17:30` is a moment. Midnight is read as
    no time at all, the same rule `core/when.py` applies everywhere else: midnight and nothing-at-all are the same
    value on the wire, so treating one as a time would sweep a day in or out for anyone asking to stop at it.

    It takes a string or an already-parsed value, because the route declares `datetime` so the OpenAPI schema says
    a time is accepted, while plenty of callers inside the app still hand it ISO text.
    """
    if isinstance(value, datetime):
        return (value.date(), None) if value.timetz() == time(0, 0) else (value.date(), value)
    if isinstance(value, date):
        return value, None
    text = (value or "").strip()
    try:
        return date.fromisoformat(text), None
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ExecutiveError(f"The {which} date isn't a date this can read. Pick it from the calendar.") from None
    if moment.timetz() == time(0, 0):
        return moment.date(), None
    return moment.date(), moment


def resolve_period(period_id: str, today: date | None = None, start: str | datetime | None = None,
                   end: str | datetime | None = None) -> Period:
    t = today or business_today()
    if period_id == "today":
        return Period("today", "Today", t, t, t - timedelta(days=1), t - timedelta(days=1), "yesterday")
    if period_id == "yesterday":
        y = t - timedelta(days=1)
        return Period("yesterday", "Yesterday", y, y, y - timedelta(days=1), y - timedelta(days=1), "the day before")
    if period_id == "7d":
        start_d = t - timedelta(days=6)
        return Period("7d", "Last 7 days", start_d, t, start_d - timedelta(days=7), start_d - timedelta(days=1), "the previous 7 days")
    if period_id == "30d":
        start_d = t - timedelta(days=29)
        return Period("30d", "Last 30 days", start_d, t, start_d - timedelta(days=30), start_d - timedelta(days=1), "the previous 30 days")
    if period_id == "month":
        first = t.replace(day=1)
        prev_first = months_back(first, 1)
        # The same stretch of last month: 1 to 16 Sep against 1 to 16 Aug, cut at the month's end.
        prev_last = first - timedelta(days=1)
        return Period("month", "This month", first, t, prev_first, min(prev_first + (t - first), prev_last), "the same days last month")
    if period_id == "last-month":
        last = t.replace(day=1) - timedelta(days=1)
        first = last.replace(day=1)
        before_last = first - timedelta(days=1)
        return Period("last-month", "Last month", first, last, before_last.replace(day=1), before_last, "the month before")
    if period_id in ("3m", "6m", "12m"):
        n = int(period_id[:-1])
        start_d = months_back(t, n) + timedelta(days=1)
        prev_start, prev_end = _before(start_d, t)
        return Period(period_id, f"Last {n} months", start_d, t, prev_start, prev_end, f"the {n} months before")
    if period_id == "ytd":
        start_d = date(t.year, 1, 1)
        # 29 February has no counterpart in the previous year, so the comparison window ends on the
        # 28th. Without this the whole Executive module is a 500 for that one day of the year, because
        # every endpoint here resolves a period before it does anything else.
        prev_day = 28 if (t.month, t.day) == (2, 29) else t.day
        return Period("ytd", "Year to date", start_d, t, date(t.year - 1, 1, 1), date(t.year - 1, t.month, prev_day), "the same period last year")
    if period_id == "all":
        return Period("all", "All time", date(2000, 1, 1), t, date(2000, 1, 1), date(2000, 1, 1), "-")
    if period_id == "custom":
        if not start or not end:
            raise ExecutiveError("Choose both a first and a last day for your own dates.")
        first, at_first = parse_moment(start, "first")
        last, at_last = parse_moment(end, "last")
        if first > last:
            raise ExecutiveError("The first day comes after the last day. Swap them round and try again.")
        if at_first and at_last and at_first > at_last:
            raise ExecutiveError("The window starts after it ends. Swap the times round and try again.")
        prev_start, prev_end = _before(first, last)
        n = (last - first).days + 1
        label = date_range_label(first, last)
        if at_first or at_last:
            # The times belong in the label: a figure for part of a day, shown under a label that names the whole
            # day, is a figure somebody will quote at the wrong size.
            shown = f"{at_first:%H:%M}" if at_first else "the start"
            until = f"{at_last:%H:%M}" if at_last else "the end"
            label = f"{label}, {shown} to {until}"
        return Period("custom", label, first, last, prev_start, prev_end,
                      "the day before" if n == 1 else f"the {n} days before",
                      at_start=at_first, at_end=at_last)
    return resolve_period("30d", t)


# In the order the period picker offers them. "custom" is not listed: it has no fixed dates to show.
PERIOD_IDS = ["today", "yesterday", "7d", "30d", "month", "last-month", "3m", "6m", "12m", "ytd", "all"]


def _in(qs, branch_id: str | None):
    """One branch's rows, or every branch's. Every fold below takes the branch the same way."""
    return qs.filter(branch_id=branch_id) if branch_id else qs


def _scope_sql(period, branch_id: str | None, alias: str = "ps") -> tuple[str, list]:
    """The same branch-and-day filter as `_in`, for the folds that group in SQL rather than in Python."""
    where = [f'{alias}."day" >= ?', f'{alias}."day" <= ?']
    args: list = [period.start.isoformat(), period.end.isoformat()]
    if branch_id:
        where.append(f'{alias}."branch_id" = {dialect.mark()}')
        args.append(branch_id)
    return " AND ".join(where), args


def _sum(value) -> Decimal:
    """A SUM() from SQLite, as a Decimal.

    SQLite returns a float for SUM(CAST(x AS REAL)) and `_money` quantizes a Decimal, so the two have to meet
    somewhere. Here, once, rather than at every call site: str() first so the Decimal is built from the digits the
    float prints rather than from its binary expansion.
    """
    if value is None:
        return D0
    return _money(Decimal(str(value)))


async def _grouped(sql: str, args: list) -> list[dict]:
    """A GROUP BY run by the database.

    Every fold that uses this used to select whole rows and add them up in a dict. That cost nothing while a branch
    sent a few hundred Item-days a day; against 1,818,232 of them it made the Company Dashboard take 34 seconds for a
    month and 269 for a year, of which 20 seconds was Python doing arithmetic SQLite does in milliseconds.

    Raw SQL rather than `annotate(Sum(...))` because Tortoise's annotate groups by row rather than over the set,
    which this repository has been caught by before.
    """
    from tortoise import Tortoise

    from app.services import query_cache

    return await query_cache.remember(
        sql, args, lambda: Tortoise.get_connection("default").execute_query_dict(sql, args))


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

    # "day"   -- folded from BranchDailyStat, one row per branch per day, as this has always done.
    # "bills" -- folded from the documents, because the caller picked a time. `whole_day` then names the figures
    #            that could not follow, so a screen can mark them rather than quietly showing a day's worth of
    #            cash beside an hour's worth of sales.
    basis: str = "day"
    whole_day: tuple[str, ...] = ()


_SUM_FIELDS = (
    "gross_sales", "disc_total", "gst", "net_sales", "cogs", "items_sold",
    "returns_value", "cash_collected", "credit_sales", "cash_in", "cash_out", "till_variance",
)


# The figures a timed window cannot get from the documents, and why. Each was measured rather than assumed, by
# `ops/checks/executive_grain.py`, which folds the same window both ways and must find them identical.
#
#   cash_in, cash_out    a branch sums its cash movements per day before head office ever sees them. There is no
#                        movement-level row here to filter.
#   stock_value          a snapshot, stated once a day rather than accumulated through it. "Stock at 14:30" is not
#                        a number that exists.
#   cash_collected,      these ARE in the documents, and the documents are not fit to be read: 311,240 of
#   credit_sales         1,067,857 bills carry a tender row at all, and those that do total 176 times the value of
#                        the bills they sit on, because a barcode was written into `amount` (8,964,002,874.00 on a
#                        Rs 45 bill; 8964 is the GS1 prefix on their own products). Fix that import and these two
#                        move up into the timed fold with no other change here.
WHOLE_DAY_WHEN_TIMED = ("cash_in", "cash_out", "cash_collected", "credit_sales")


async def totals_for(period: Period, branch_id: str | None = None) -> Totals:
    if period.timed:
        return await _totals_from_bills(period, branch_id)
    rows = await _in(BranchDailyStat.filter(day__gte=period.start, day__lte=period.end), branch_id)

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


async def _totals_from_bills(period: Period, branch_id: str | None) -> Totals:
    """The same totals, rebuilt from the documents, because the caller asked for part of a day.

    `branch_sales.at` IS LOCAL PAKISTAN TIME, stored naive, so a bound of 17:30 compares against it directly with
    no conversion. Verified rather than assumed, 14 October 2026: bill 1201743066 is stamped `2026-10-13 01:34:21`
    and carries `day = 2026-10-12`, which is only true of a shop's own clock running past midnight -- under UTC
    that bill would have fallen on the 13th. The busiest stored hours are 18:00 to 21:00, which is evening trade
    locally and implausible as UTC.

    Where it differs from the daily fold, and by how much: a bill's `net_value` is what the customer was charged
    after rounding to the rupee, while the daily rows carry the unrounded total. So a whole-day window folded here
    can differ from the same window folded from the daily rows by at most Rs 0.50 a bill, and did differ by
    Rs 58.59 over 19,020 bills when measured. The bills are the better answer to "what did we take": they are what
    the receipts in people's hands say.
    """
    lo = period.at_start or datetime.combine(period.start, time.min)
    hi = period.at_end or datetime.combine(period.end, time.max)

    def window(alias: str, column: str = "at") -> tuple[str, list]:
        # Both the clock window AND the trading days picked. A shop selling past midnight gives the two different
        # meanings: 52 bills sat inside 12 October by the clock while belonging to trading day the 11th. Taking
        # those in would contradict the label the screen shows, which names the days; leaving the clock bound off
        # would ignore what was asked. So: the days asked for, and within them the hours asked for.
        where = (f"{alias}.{dialect.quote(column)} >= ? AND {alias}.{dialect.quote(column)} <= ?"
                 f" AND {alias}.day >= ? AND {alias}.day <= ?")
        args: list = [lo, hi, period.start, period.end]
        if branch_id:
            # `dialect.mark()`, not a bare `?`: these keys are char(36) on SQL Server and a plain parameter binds
            # as nvarchar, so the comparison converts on every row and the index this fold was given cannot be
            # seeked. Measured elsewhere in this file at nine times on a single comparison.
            where += f" AND {alias}.branch_id = {dialect.mark()}"
            args.append(branch_id)
        return where, args

    sales_where, sales_args = window("s")
    # Grouped by branch and day so trading hours and named customers keep the per-day meaning the daily rows have:
    # a customer who came on two days counts twice, and each day's open hours are measured on their own.
    by_day = await _grouped(f"""
        SELECT s.branch_id, s.day, COUNT(*) AS invoices,
               SUM(s.gross) AS gross_sales, SUM(s.disc_total) AS disc_total, SUM(s.gst) AS gst,
               SUM(s.net_value) AS net_sales, SUM(s.cogs) AS cogs, SUM(s.cash_back) AS cash_back,
               COUNT(DISTINCT s.party_code) AS named_customers,
               MIN(s.{dialect.quote('at')}) AS first_sale_at, MAX(s.{dialect.quote('at')}) AS last_sale_at
          FROM branch_sales s WHERE {sales_where}
         GROUP BY s.branch_id, s.day
    """, sales_args)

    items = await _grouped(f"""
        SELECT SUM(CASE WHEN l.is_return = 1 THEN -l.qty ELSE l.qty END) AS items_sold
          FROM branch_sale_lines l JOIN branch_sales s ON s.id = l.sale_id WHERE {sales_where}
    """, sales_args)

    returns_where, returns_args = window("r")
    returns = await _grouped(f"""
        SELECT COUNT(*) AS returns_count, SUM(r.refund_total) AS returns_value
          FROM branch_return_records r WHERE {returns_where}
    """, returns_args)

    # A drawer belongs to the window by when it was closed, which is the only moment a variance exists at.
    tills_where, tills_args = window("t", "closed_at")
    tills = await _grouped(f"""
        SELECT COUNT(*) AS tills_closed, SUM(t.variance) AS till_variance
          FROM branch_till_closes t WHERE {tills_where}
    """, tills_args)

    t = Totals(days=len({r["day"] for r in by_day}), basis="bills", whole_day=WHOLE_DAY_WHEN_TIMED)
    for r in by_day:
        t.invoices += int(r["invoices"] or 0)
        t.gross_sales += _sum(r["gross_sales"])
        t.disc_total += _sum(r["disc_total"])
        t.gst += _sum(r["gst"])
        t.net_sales += _sum(r["net_sales"])
        t.cogs += _sum(r["cogs"])
        t.named_customers += int(r["named_customers"] or 0)
        first, last = r["first_sale_at"], r["last_sale_at"]
        if first and last:
            if isinstance(first, str):
                first, last = datetime.fromisoformat(first), datetime.fromisoformat(last)
            hours = Decimal((last - first).total_seconds()) / Decimal(3600)
            t.trading_hours += max(hours, Decimal("0.5"))

    t.items_sold = _sum(items[0]["items_sold"]) if items else D0
    if returns:
        t.returns_count = int(returns[0]["returns_count"] or 0)
        t.returns_value = _sum(returns[0]["returns_value"])
    if tills:
        t.tills_closed = int(tills[0]["tills_closed"] or 0)
        t.till_variance = _sum(tills[0]["till_variance"])

    # The four that cannot follow a time are still worth answering, as the whole days the window touches, so the
    # tile shows a figure with a marker rather than a blank. `whole_day` above is what says which they are.
    day_rows = await _in(BranchDailyStat.filter(day__gte=period.start, day__lte=period.end), branch_id)
    for r in day_rows:
        t.cash_in += r.cash_in or D0
        t.cash_out += r.cash_out or D0
        t.cash_collected += r.cash_collected or D0
        t.credit_sales += r.credit_sales or D0
    return t


async def latest_stock_value(branch_id: str | None = None) -> Decimal:
    """Branch stock is a right-now figure carried only on each branch's most recent reported day,
    so summing a date range would multiply it by the number of days in the range."""
    total = D0
    for branch in await (Branch.filter(id=branch_id) if branch_id else Branch.all()):
        row = await BranchDailyStat.filter(branch=branch).order_by("-day").first()
        if row:
            total += row.stock_value or D0
    return total


async def snapshot_as_of(branch_id: str | None = None) -> datetime | None:
    """The oldest last-seen across reporting branches — the dashboard is only as fresh as its
    stalest contributor, and claiming the freshest would overstate it. One branch is as fresh as itself."""
    branches = await (Branch.filter(id=branch_id) if branch_id else Branch.all())
    seen = [b.last_seen_at for b in branches if b.last_seen_at]
    return min(seen) if seen else None


# ── live (cloud-owned) folds ────────────────────────────────────────────────
async def _products_by_id(ids: list[str]) -> dict:
    """The products behind a list of ids, fetched in chunks and keyed by id as a string.

    `id__in=[...]` names every id as a parameter and SQL Server takes 2,100 in one statement. Both callers below
    pass one id per product the godown holds, which was safe only while it held nothing: `warehouse_stock_movements`
    had no rows, the list was empty, and the limit was never near. Loading head office's stock position put 2,202
    products in it and the Stock and Supply panels of the Company Dashboard both started answering with
    "COUNT field incorrect or syntax error" instead of a figure.
    """
    out = {}
    for part in range(0, len(ids), 500):
        for product in await Product.filter(id__in=ids[part:part + 500]):
            out[str(product.id)] = product
    return out


async def godown_stock_value() -> Decimal:
    rows = (
        await StockMovement.all().annotate(total=Sum("qty"))
        .group_by("product_id").values("product_id", "total")
    )
    if not rows:
        return D0
    prices = {pid: p.price for pid, p in (await _products_by_id([r["product_id"] for r in rows])).items()}
    return sum((Decimal(str(r["total"] or 0)) * prices.get(r["product_id"], D0) for r in rows), D0)


async def godown_balances() -> list[dict]:
    rows = (
        await StockMovement.all().annotate(total=Sum("qty"))
        .group_by("product_id").values("product_id", "total")
    )
    products = await _products_by_id([r["product_id"] for r in rows])
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


async def warehouse_status(*, transfers: bool = False, stock_value: bool = True) -> dict:
    """What the godown is holding and what is moving, counted in the database rather than in Python.

    Not `.prefetch_related("lines")`. Nothing here or in any caller reads a transfer's lines: the tile counts
    transfers by status, the drill-down lists numbers, vehicles and dates, and `live_alerts` looks at
    `dispute_open`. The prefetch fetched 115,871 lines nobody asked for, and because Tortoise prefetches by naming
    the parent ids it also meant `WHERE transfer_id IN (6,332 parameters)`, which SQL Server refuses at its 2,100
    ceiling with "COUNT field incorrect or syntax error". That answered /executive/kpis with a 500.

    AND NOT `Transfer.all()` EITHER, WHICH IS WHY THE TWO FLAGS EXIST
    ----------------------------------------------------------------
    It used to build every one of the 6,393 transfers as a model object and then count them with three list
    comprehensions. Four callers share this function and only ONE of them ever reads that list -- the drill-down
    that puts it in a table. The Supply tile wants two numbers, `business_health` wants two numbers, and
    `live_alerts` wants only the handful that are in dispute. So the counting is now one GROUP BY, the list is
    fetched only when asked for, and the disputed few are fetched on their own because that is all anybody wanted
    of them.

    `stock_value` is the same story: it costs about a third of a second and neither `business_health` nor
    `live_alerts` reads it. Together these took `warehouse_status` from 1.01s to a fraction of it, and the Health
    section is the slowest thing on the dashboard on every period, because none of this ever depended on the
    period at all.
    """
    rows = await _grouped(
        f'SELECT t."status" AS status, COUNT(*) AS n, '
        f'       SUM(CASE WHEN t."dispute_open" = 1 THEN 1 ELSE 0 END) AS disputes '
        f'FROM "transfers" t GROUP BY t."status"', [])
    by_status = {str(r["status"]): int(r["n"] or 0) for r in rows}
    out = {
        "inFlight": by_status.get("dispatched", 0) + by_status.get("in_transit", 0),
        "awaitingPick": by_status.get("approved", 0),
        "disputesOpen": sum(int(r["disputes"] or 0) for r in rows),
        "pendingRequisitions": await Requisition.filter(status="pending").count(),
        # Always: it is the few that are in dispute, which is what `live_alerts` builds its notices from.
        "disputed": await Transfer.filter(dispute_open=True),
    }
    out["stockValue"] = await godown_stock_value() if stock_value else None
    out["transfers"] = await Transfer.all() if transfers else []
    return out


async def purchase_status() -> dict:
    """What has come into the godown, and what is waiting on a decision.

    This read every delivery with `GRN.all().prefetch_related("lines")` and added the lines up here. Tortoise
    prefetches by naming the parent ids, so once the 5,968 Multi-Tec deliveries landed that became
    `WHERE grn_id IN (5,968 parameters)`, SQL Server refused it at its 2,100 ceiling with "COUNT field incorrect or
    syntax error", and the Supply tiles took the whole Company Dashboard down with them: /executive/kpis answered
    500 rather than slowly.

    The tiles want four numbers, not 92,509 delivery lines. Three aggregates answer them, and nothing is carried into
    Python that is not shown on a tile.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    counts = (await conn.execute_query_dict(
        "SELECT COUNT(*) AS grns, SUM(CASE WHEN approved = 0 THEN 1 ELSE 0 END) AS unapproved FROM warehouse_grns", []))[0]
    # qty is decimal(16,3) and unit_price decimal(14,2), so the product is exact at decimal(31,5) and the sum at
    # decimal(38,5): the same arithmetic the Python loop did, to the same last digit.
    value = (await conn.execute_query_dict("SELECT SUM(qty * unit_price) AS value FROM warehouse_grn_lines", []))[0]
    reqs = {r["status"]: r["n"] for r in await conn.execute_query_dict(
        "SELECT status, COUNT(*) AS n FROM requisitions GROUP BY status", [])}
    return {
        "grnCount": int(counts["grns"] or 0),
        "receivedValue": Decimal(str(value["value"] or 0)),
        "unapproved": int(counts["unapproved"] or 0),
        "pendingRequisitions": int(reqs.get("pending", 0)),
        "approvedRequisitions": int(reqs.get("approved", 0)),
        "rejectedRequisitions": int(reqs.get("rejected", 0)),
    }


async def supplier_performance() -> list[dict]:
    """What the godown's own receiving history says about each supplier: how much has come in,
    over how many receipts, and how recently.

    Deliberately not "on-time delivery %" or a quality score — nothing in the MVP records a
    promised date or a rejection reason, so those would be invented numbers wearing a KPI label.

    Same reason as `purchase_status` for not reading the deliveries themselves: `GRN.all().prefetch_related("lines")`
    named all 5,968 of them as parameters and SQL Server refused. Two GROUP BYs instead, one per grain.

    The receipts and the last receipt date have to come from the deliveries rather than from their lines, because a
    delivery with no lines is still a receipt. That is why this is two queries and not one join.
    """
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    suppliers = {str(s.id): s for s in await Supplier.all()}
    receipts = {str(r["supplier_id"]): r for r in await conn.execute_query_dict(
        "SELECT supplier_id, COUNT(*) AS receipts, MAX(at) AS last_at FROM warehouse_grns GROUP BY supplier_id", [])}
    lines = {str(r["supplier_id"]): r for r in await conn.execute_query_dict(
        "SELECT g.supplier_id AS supplier_id, SUM(l.qty * l.unit_price) AS value, SUM(l.qty) AS units, "
        "       SUM(l.bonus_qty) AS bonus FROM warehouse_grn_lines l "
        "  JOIN warehouse_grns g ON g.id = l.grn_id GROUP BY g.supplier_id", [])}
    agg: dict[str, dict] = {}
    for sid in list(receipts) + [s for s in suppliers if s not in receipts]:
        got, line = receipts.get(sid), lines.get(sid, {})
        agg[sid] = {
            "supplierId": sid,
            "name": suppliers[sid].name if sid in suppliers else sid,
            "code": suppliers[sid].code if sid in suppliers else None,
            "receipts": int(got["receipts"]) if got else 0,
            "value": Decimal(str(line.get("value") or 0)),
            "units": Decimal(str(line.get("units") or 0)),
            "bonusUnits": Decimal(str(line.get("bonus") or 0)),
            "lastReceipt": got["last_at"] if got else None,
        }
    return sorted(agg.values(), key=lambda r: r["value"], reverse=True)


# ── breakdowns ──────────────────────────────────────────────────────────────
async def _ranked_items(period: Period, branch_id: str | None) -> list[dict]:
    """Each Item summed over the period: whole months from `branch_product_months`, the ragged ends from the days.

    WHY THIS DOES NOT GROUP THE DAYS ITSELF
    ---------------------------------------
    It used to, and over a year that is the whole of `branch_product_stats` -- 1,838,739 rows -- grouped twice, once
    for the Items and once for the categories. The Rankings section of the Company Dashboard took 21.6 seconds for
    the twelve month view against a browser that gives up at 15, so that section showed an error on that period.

    `analytics_service._ranked` already answers exactly this question off the rollup, including the part-month at
    each end and the fall back to the day table when the rollup has not been built. Calling it rather than writing a
    second reader also means the dashboard's Top Items and the Item analytics screens cannot disagree about what an
    Item sold, which two separate folds over the same rows eventually would.

    WHAT CHANGED IN THE NUMBERS, EXACTLY
    ------------------------------------
    Nothing in any total: net sales over the twelve months is 696,942,558.33 over 26,070 Items read either way, to
    the paisa. What changes is where an Item sits when its own classification moved during the period. Grouping the
    days puts each day's sales in whatever category that day carried, so one Item lands in two categories and is
    counted in both their Item counts. This path takes one category per Item, so it lands in one. Over the last
    twelve months that is 2 Items whose category changed and 5 whose brand changed, out of 26,070: MEDICINE
    (PHARMACY) reads 2,489 Items rather than 2,490, and DefaultBrand 8,896 rather than 8,901. Department was
    identical. If attribution at the time of sale is wanted instead, it is this function that changes, not the two
    callers.

    The rollup is keyed by branch code, not id, so the one lookup here turns the id the dashboard holds into the code
    the rollup wants.
    """
    from app.services.analytics_service import _ranked

    code = None
    if branch_id:
        found = await Branch.filter(id=branch_id).values_list("code", flat=True)
        code = found[0] if found else None
    return await _ranked(period.start, period.end, code)


def _classified(value) -> str:
    """A grouping label, with blank and missing both reading as Unclassified rather than as two empty groups."""
    text = (value or "").strip()
    return text or "Unclassified"


async def top_products(period: Period, limit: int = 20, branch_id: str | None = None) -> list[dict]:
    rows = sorted(await _ranked_items(period, branch_id), key=lambda r: -float(r["net_sales"] or 0))
    # The limit is applied here, not in the read, so every caller asks the same question and the read memo can answer
    # it once. The grid wants 20, a chart 12, a drill-down 50 and another 1,000; discarding rows afterwards is free.
    out = []
    for r in rows[:limit]:
        net, cogs = _sum(r["net_sales"]), _sum(r["cogs"])
        out.append({"sku": r["sku"], "name": r["name"], "department": r["department"],
                    "category": r["category"], "brand": r["brand"], "qty": _sum(r["qty"]),
                    "netSales": net, "cogs": cogs, "grossProfit": net - cogs})
    return out


# The columns a Top Categories view may group by. Named rather than interpolated, because `attr` reaches this from
# a query string, and it is now also the key read off each Item's row.
_GROUPABLE = {"category": "category", "brand": "brand", "department": "department"}


async def top_by(period: Period, attr: str, limit: int = 15, branch_id: str | None = None) -> list[dict]:
    """Top categories / departments / brands -- the same Items, folded on a different column.

    Folded here off `_ranked_items` rather than grouped over the day rows a second time. The rows are one per Item,
    which is 26,070 over a year, against another pass over 1.8 million Item-days: that second pass was 7.9 seconds of
    the Rankings section's 21.6, on top of the 5.3 that Top Items cost over the very same rows. Now 0.26s.

    `products` counts the Items in each group, which is what `COUNT(DISTINCT product_sku)` counted before. There is
    one row per Item here, so it is the number of rows. See `_ranked_items` for the one way that answer differs.
    """
    if attr not in _GROUPABLE:
        raise ValueError(f"cannot group Items by {attr!r}: only {', '.join(sorted(_GROUPABLE))}")
    groups: dict[str, dict] = {}
    for r in await _ranked_items(period, branch_id):
        name = _classified(r.get(attr))
        g = groups.get(name)
        if g is None:
            g = groups[name] = {"name": name, "qty": D0, "net": D0, "cogs": D0, "products": 0}
        g["qty"] += _sum(r["qty"])
        g["net"] += _sum(r["net_sales"])
        g["cogs"] += _sum(r["cogs"])
        g["products"] += 1
    ranked = sorted(groups.values(), key=lambda g: -g["net"])
    return [{"name": g["name"], "qty": g["qty"], "netSales": g["net"],
             "grossProfit": g["net"] - g["cogs"], "products": g["products"]} for g in ranked[:limit]]


async def cashier_ranking(period: Period, limit: int = 20, branch_id: str | None = None) -> list[dict]:
    """Who sold what, ranked. Scoped to a branch, the people are that branch's people: their bills there and nowhere
    else.

    Both folds are done by the database. They used to be done in Python over model objects, which cost nothing while
    a branch sent a few dozen cashier-days and became the single most expensive thing on the Company Dashboard once
    `branch_product_cashier_stats` held 2,251,239 rows: Tortoise built an object for every one, parsing a date and a
    UUID on the way, and the grid asks for this eighty times to draw one screen.

    `COUNT(DISTINCT product_sku)` is what `len(set(...))` was doing by hand, and `GROUP_CONCAT(DISTINCT ...)` is the
    branch list. The two halves stay two queries because a join between them would multiply each cashier-day by that
    person's Item rows and silently inflate every invoice count.
    """
    where, args = _scope_sql(period, branch_id, "cs")
    people = await _grouped(f'''
        SELECT cs."cashier_name" AS name,
               SUM(cs."invoices") AS invoices,
               SUM(CAST(cs."net_sales" AS REAL)) AS net_sales,
               COUNT(*) AS days,
               {dialect.group_concat_distinct(chr(34) + "b" + chr(34) + "." + chr(34) + "code" + chr(34))} AS branches
          FROM "branch_cashier_stats" cs
          JOIN "branches" b ON b."id" = cs."branch_id"
         WHERE {where}
         GROUP BY cs."cashier_name"
    ''', args)
    if not people:
        return []

    where, args = _scope_sql(period, branch_id, "pc")
    baskets = {r["name"]: r for r in await _grouped(f'''
        SELECT pc."cashier_name" AS name,
               SUM(CAST(pc."qty" AS REAL)) AS qty,
               SUM(CAST(pc."net_sales" AS REAL)) AS net_sales,
               SUM(CAST(pc."cogs" AS REAL)) AS cogs,
               COUNT(DISTINCT pc."product_sku") AS items
          FROM "branch_product_cashier_stats" pc
         WHERE {where}
         GROUP BY pc."cashier_name"
    ''', args)}

    out: list[dict] = []
    for r in people:
        basket = baskets.get(r["name"]) or {}
        net = _sum(r["net_sales"])
        item_net = _sum(basket.get("net_sales"))
        cogs = _sum(basket.get("cogs"))
        invoices = int(r["invoices"] or 0)
        out.append({
            "name": r["name"], "invoices": invoices, "netSales": net, "days": int(r["days"] or 0),
            # GROUP_CONCAT gives them in whatever order it met them; the old fold sorted a set.
            "branches": ", ".join(sorted((r["branches"] or "?").split(","))),
            "qty": _sum(basket.get("qty")), "itemNetSales": item_net, "cogs": cogs,
            "distinctItems": int(basket.get("items") or 0),
            "avgBasket": _money(net / invoices) if invoices else D0,
            "grossProfit": (item_net - cogs) if item_net else None,
            "marginPercent": _money((item_net - cogs) / item_net * 100) if item_net else None,
        })
    total = sum((r["netSales"] for r in out), D0)
    for r in out:
        r["share"] = _money(r["netSales"] / total * 100) if total else D0
    out.sort(key=lambda r: r["netSales"], reverse=True)
    return out[:limit]  # the SQL carries no LIMIT, so every caller shares one cached fold


async def duty_by_day(period: Period, branch_id: str | None = None) -> dict[tuple[str, date], dict]:
    """Per branch-day: how many people were put on a counter, and for how long."""
    out: dict[tuple[str, date], dict] = {}
    for r in await _in(BranchStaffDuty.filter(day__gte=period.start, day__lte=period.end), branch_id):
        row = out.setdefault((str(r.branch_id), r.day), {"people": set(), "minutes": 0, "counters": set()})
        row["people"].add(r.cashier_name)
        row["minutes"] += r.minutes
        if r.counter_name:
            row["counters"].add(r.counter_name)
    return out


async def duty_people(period: Period, day_iso: str | None = None, branch_id: str | None = None) -> list[dict]:
    """Every spell on a counter in the period — who, where, how long."""
    qs = _in(BranchStaffDuty.filter(day__gte=period.start, day__lte=period.end), branch_id)
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
            "day": r.day.isoformat(), "name": r.cashier_name, "counter": r.counter_name or "-",
            "branch": branches.get(str(r.branch_id), "?"), "spells": 0, "minutes": 0,
        })
        row["spells"] += r.spells
        row["minutes"] += r.minutes
    for row in rows.values():
        row["hours"] = _money(Decimal(row["minutes"]) / 60)
    return sorted(rows.values(), key=lambda r: (r["day"], -r["minutes"]), reverse=True)


async def staffing(period: Period, branch_id: str | None = None) -> list[dict]:
    """Per branch per day: how many people were put on a counter, how long they stood there, and how
    many of them actually rang a sale.

    Traded is measured from sales and always has been. On the floor is now a record of its own, kept by
    the branch when somebody is put on a counter — the only way a person who was there all morning and
    sold nothing can be counted at all. A branch that has not set counters up reports no duty, and the
    column says so rather than guessing.
    """
    rows = await _in(BranchDailyStat.filter(day__gte=period.start, day__lte=period.end), branch_id)
    branches = {str(b.id): b for b in await Branch.all()}
    duty = await duty_by_day(period, branch_id)
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
    """Every selling branch ranked by net sales. Head office is not ranked -- see `selling_branches`."""
    branches = await selling_branches()
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


async def daily_series(period: Period, field_name: str = "net_sales", branch_id: str | None = None) -> list[dict]:
    rows = await _in(BranchDailyStat.filter(day__gte=period.start, day__lte=period.end), branch_id)
    agg: dict[date, Decimal] = {}
    for r in rows:
        agg[r.day] = agg.get(r.day, D0) + (getattr(r, field_name) or D0)
    return [{"day": d.isoformat(), "value": v} for d, v in sorted(agg.items())]


async def stock_alerts(kind: str | None = None, limit: int = 200, branch_id: str | None = None) -> tuple[list[dict], dict[str, int]]:
    qs = _in(BranchStockAlert.all(), branch_id)
    if kind:
        qs = qs.filter(kind=kind)
    rows = await qs.limit(limit)
    branches = {str(b.id): b for b in await Branch.all()}
    totals: dict[str, int] = {}
    for r in await _in(BranchStockAlert.all(), branch_id):
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


async def business_health(period: Period, branch_id: str | None = None) -> Health:
    """A transparent composite, not a black box — every component and its weight is returned so
    the drill-down can show exactly how the number was reached. Nothing here is predictive or
    AI-derived; those need the AI engine, which does not exist.
    """
    now = await totals_for(period, branch_id)
    prev = await totals_for(Period(period.id, period.label, period.prev_start, period.prev_end,
                                   period.prev_start, period.prev_end, period.compare_label), branch_id)

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
    # Two counts, so neither the stock value nor the transfer list is asked for.
    wh = await warehouse_status(stock_value=False)
    friction = wh["disputesOpen"] * 2 + wh["pendingRequisitions"]
    components.append(HealthComponent(
        "supply", "Supply chain", _clamp(100 - friction * 12), 10,
        f"{wh['disputesOpen']} open dispute(s), {wh['pendingRequisitions']} requisition(s) waiting"))

    # Reporting: branches that have actually reported at all.
    branches = await (Branch.filter(id=branch_id) if branch_id else Branch.all())
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

    # Neither the whole transfer list nor the stock value: this builds a notice per dispute and a notice about
    # waiting requisitions, and reads nothing else.
    wh = await warehouse_status(stock_value=False)
    for t in wh["disputed"]:
        if True:
            out.append({
                "severity": "high", "kind": "dispute", "source": "live",
                "title": f"{t.transfer_number}: short-receipt dispute",
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

    # Selling branches: head office has a branches row but no branch server, so "has never reported" is not a
    # thing that can be true of it. See models.branch.selling_branches.
    for b in await selling_branches():
        if b.status == "active" and not b.last_seen_at:
            out.append({
                "severity": "medium", "kind": "sync", "source": "live",
                "title": f"{b.name} has never reported",
                "detail": "Registered, but no data has arrived from this branch yet.",
            })

    # What the branches say is broken at their end. Worth being here rather than only on its own tile: this is
    # the list somebody reads when they want to know what needs a decision, and a shop that cannot sell is the
    # most urgent thing on it. Counted, not listed one by one -- the tile's drill-down has the detail.
    from app.services import technical_reports_service as tr

    tally = await tr.failure_tally()
    for shop in tally["branches"]:
        if not shop["total"]:
            continue
        fresh = shop["lastDay"]
        out.append({
            "severity": "high" if fresh else "low", "kind": "branch-error", "source": "live",
            "title": f"{shop['name']}: {shop['total']} failure(s) reported" + (f", {fresh} today" if fresh else ""),
            "detail": "The branch is pushing, so Sync Health reads clear. Its staff are working around something."
                      if fresh else f"Nothing in the last day. The most recent was within {tally['days']} days.",
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


async def hourly_profile(period: Period, branch_id: str | None = None) -> list[dict]:
    """Invoices and sales by hour of the local trading day. This is what bills-per-hour is
    actually about — where the queue forms, and therefore where staff need to be."""
    rows = await _in(BranchHourlyStat.filter(day__gte=period.start, day__lte=period.end), branch_id)
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


async def till_closes(period: Period, cashier: str | None = None, branch_id: str | None = None) -> list[dict]:
    qs = _in(BranchTillClose.filter(day__gte=period.start, day__lte=period.end), branch_id)
    if cashier:
        qs = qs.filter(cashier_name=cashier)
    return [{
        "sessionNumber": r.session_number, "day": r.day.isoformat(), "cashier": r.cashier_name,
        "openedAt": r.opened_at, "closedAt": r.closed_at, "openingFloat": r.opening_float,
        "netCash": r.net_cash, "countedCash": r.counted_cash, "variance": r.variance,
    } for r in await qs.order_by("-closed_at")]


async def tender_mix(period: Period, branch_id: str | None = None) -> list[dict]:
    """Cash, card and credit have completely different consequences for working capital, so one
    "sales" figure hides the question that actually matters."""
    rows = await _in(BranchTenderStat.filter(day__gte=period.start, day__lte=period.end), branch_id)
    agg: dict[str, dict] = {}
    for r in rows:
        row = agg.setdefault(r.code, {"code": r.code, "name": r.name, "uses": 0, "amount": D0})
        row["uses"] += r.uses
        row["amount"] += r.amount or D0
    total = sum((r["amount"] for r in agg.values()), D0)
    for row in agg.values():
        row["share"] = _money(row["amount"] / total * 100) if total else D0
    return sorted(agg.values(), key=lambda r: r["amount"], reverse=True)


async def discount_overrides(period: Period, approver: str | None = None, branch_id: str | None = None) -> list[dict]:
    """Margin given away on a manager's signature, by name and invoice."""
    qs = _in(BranchDiscountOverride.filter(day__gte=period.start, day__lte=period.end), branch_id)
    if approver:
        qs = qs.filter(approved_by=approver)
    return [{
        "invoiceNumber": r.invoice_number, "at": r.at, "day": r.day.isoformat(),
        "cashier": r.cashier_name, "approvedBy": r.approved_by,
        "gross": r.gross, "discTotal": r.disc_total, "netValue": r.net_value,
        "discPercent": _money(r.disc_total / r.gross * 100) if r.gross else D0,
    } for r in await qs.order_by("-at")]


async def returns_detail(period: Period, branch_id: str | None = None) -> list[dict]:
    """One row per line taken back. A branch sends the whole return's refund on every line of it, so the refund sits on
    the return's first line only (the lines after it show none) and `firstLine` marks the row to count a return by.
    A return is its branch, its time, the bill it was against and who took it."""
    qs = _in(BranchReturn.filter(day__gte=period.start, day__lte=period.end), branch_id)
    seen: set[tuple] = set()
    out = []
    for r in await qs.order_by("-at"):
        key = (str(r.branch_id), r.at, r.against_invoice, r.cashier_name)
        first = key not in seen
        seen.add(key)
        out.append({
            "at": r.at, "day": r.day.isoformat(), "againstInvoice": r.against_invoice,
            "cashier": r.cashier_name, "productName": r.product_name, "productSku": r.product_sku,
            "qty": r.qty, "refundTotal": r.refund_total if first else None, "firstLine": first,
        })
    return out


async def credit_customers(branch_id: str | None = None) -> list[dict]:
    return [{
        "code": r.code, "name": r.name, "phone": r.phone, "tier": r.tier,
        "creditLimit": r.credit_limit, "creditBalance": r.credit_balance,
        "headroom": (r.credit_limit or D0) - (r.credit_balance or D0),
        "usedPercent": _money(r.credit_balance / r.credit_limit * 100) if r.credit_limit else D0,
    } for r in await _in(BranchCreditCustomer.all(), branch_id)]


# ── focused views: one category, one product, one cashier, one day, one supplier ──
async def products_where(period: Period, attr: str, value: str, limit: int = 100, branch_id: str | None = None) -> list[dict]:
    """The products inside one category / brand / department — what a Top Categories row opens."""
    column = _GROUPABLE.get(attr)
    if not column:
        raise ValueError(f"cannot pick Items by {attr!r}: only {', '.join(sorted(_GROUPABLE))}")
    where, args = _scope_sql(period, branch_id)
    rows = await _grouped(f'''
        SELECT ps."product_sku" AS sku, MAX(ps."product_name") AS name,
               MAX(ps."category") AS category, MAX(ps."brand") AS brand,
               MAX(ps."department") AS department,
               SUM(CAST(ps."qty" AS REAL)) AS qty,
               SUM(CAST(ps."net_sales" AS REAL)) AS net_sales,
               SUM(CAST(ps."cogs" AS REAL)) AS cogs
          FROM "branch_product_stats" ps
         WHERE {where}
           AND LOWER(TRIM(COALESCE(NULLIF(TRIM(COALESCE(ps."{column}", '')), ''), 'Unclassified'))) = ?
         GROUP BY ps."product_sku"
         ORDER BY SUM(CAST(ps."net_sales" AS REAL)) DESC
    ''', args + [(value or "").strip().lower()])
    out = []
    for r in rows[:limit]:
        net, cogs = _sum(r["net_sales"]), _sum(r["cogs"])
        profit = net - cogs
        out.append({"sku": r["sku"], "name": r["name"], "category": r["category"], "brand": r["brand"],
                    "department": r["department"], "qty": _sum(r["qty"]), "netSales": net, "cogs": cogs,
                    "grossProfit": profit,
                    "marginPercent": _money(profit / net * 100) if net else D0})
    return out


async def product_days(period: Period, sku: str, branch_id: str | None = None) -> tuple[list[dict], dict]:
    """One product, day by day, plus its period totals."""
    rows = await _in(BranchProductStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku), branch_id)
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


async def cashier_days(period: Period, name: str, branch_id: str | None = None) -> tuple[list[dict], dict]:
    rows = await _in(BranchCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name), branch_id)
    # One row per day, even when the same name rang at two branches that day.
    per_day: dict[date, dict] = {}
    for r in rows:
        d = per_day.setdefault(r.day, {"invoices": 0, "netSales": D0})
        d["invoices"] += r.invoices
        d["netSales"] += r.net_sales or D0
    days = []
    totals = {"invoices": 0, "netSales": D0, "days": 0}
    for day, d in sorted(per_day.items()):
        days.append({
            "day": day.isoformat(), "invoices": d["invoices"], "netSales": d["netSales"],
            "avgBasket": _money(d["netSales"] / d["invoices"]) if d["invoices"] else D0,
        })
        totals["invoices"] += d["invoices"]
        totals["netSales"] += d["netSales"]
        totals["days"] += 1
    totals["avgBasket"] = _money(totals["netSales"] / totals["invoices"]) if totals["invoices"] else D0
    by_day: dict[str, dict] = {}
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name), branch_id):
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


async def product_day_people(period: Period, sku: str, branch_id: str | None = None) -> dict[str, str]:
    """For each day an item sold, who sold it and how many: "Hina Malik 12 · Ali Raza 4"."""
    per: dict[str, dict[str, Decimal]] = {}
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku), branch_id):
        people = per.setdefault(r.day.isoformat(), {})
        people[r.cashier_name] = people.get(r.cashier_name, D0) + (r.qty or D0)
    out = {}
    for day, people in per.items():
        ranked = sorted(people.items(), key=lambda kv: kv[1], reverse=True)
        out[day] = " · ".join(f"{n} ({q.normalize():f})" for n, q in ranked)
    return out


# One day's drill-down lists what sold, and "what sold" has to mean all of it. It was capped at 20, which on
# 1 October showed 20 Items of the 2,182 that sold while the day's total above it was the whole 2.4 million: the
# list looked like the answer and was a twentieth of it. The fold is already done by the time the limit is applied,
# so asking for every Item costs nothing measurable (0.02s against 0.03s). The cap that remains is a ceiling on the
# size of one response, not a view: no day has come near it, and if one ever does the count on the card says so.
DAY_ITEMS = 5000
DAY_PEOPLE = 200


async def day_detail(day_iso: str, branch_id: str | None = None) -> dict:
    """Everything about one trading day — what a row in the daily breakdown opens into."""
    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        return {}
    one = Period("day", day_iso, day, day, day, day, "the day before")
    return {
        "day": day_iso,
        "hours": await hourly_profile(one, branch_id),
        "cashiers": await cashier_ranking(one, limit=DAY_PEOPLE, branch_id=branch_id),
        "products": await top_products(one, limit=DAY_ITEMS, branch_id=branch_id),
        "tenders": await tender_mix(one, branch_id),
        "closes": await till_closes(one, branch_id=branch_id),
        "totals": await totals_for(one, branch_id),
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
    # One row per sku, not one per Item-day: this read every one of them to keep the last, which for a year is
    # 1.8 million rows fetched to build a dictionary of about 68,000.
    rows = await _grouped('''
        SELECT ps."product_sku" AS sku, MAX(ps."product_name") AS name,
               MAX(ps."category") AS category, MAX(ps."brand") AS brand, MAX(ps."department") AS department
          FROM "branch_product_stats" ps
         WHERE ps."day" >= ? AND ps."day" <= ?
         GROUP BY ps."product_sku"
    ''', [period.start.isoformat(), period.end.isoformat()])
    return {r["sku"]: {"name": r["name"], "category": r["category"] or "Unclassified",
                       "brand": r["brand"] or "Unclassified",
                       "department": r["department"] or "Unclassified"} for r in rows}


def _profit(row: dict) -> dict:
    row["grossProfit"] = row["netSales"] - row["cogs"]
    row["marginPercent"] = _money(row["grossProfit"] / row["netSales"] * 100) if row["netSales"] else D0
    return row


async def person_items_known(period: Period, branch_id: str | None = None) -> bool:
    """Whether any branch has sent who-sold-what for this period (an older branch version doesn't)."""
    return await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end), branch_id).exists()


async def product_people(period: Period, sku: str, branch_id: str | None = None) -> list[dict]:
    """Everyone who sold one item: how many, for how much, over how many bills, and their share of the item."""
    branches = await _branch_codes()
    agg: dict[str, dict] = {}
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku), branch_id):
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


async def product_branches(period: Period, sku: str, branch_id: str | None = None) -> list[dict]:
    branches = {str(b.id): b for b in await Branch.all()}
    agg: dict[str, dict] = {}
    for r in await _in(BranchProductStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku), branch_id):
        b = branches.get(str(r.branch_id))
        code = b.code if b else "?"
        row = agg.setdefault(code, {"code": code, "branch": b.name if b else code, "sku": sku, "qty": D0, "netSales": D0, "cogs": D0})
        row["qty"] += r.qty or D0
        row["netSales"] += r.net_sales or D0
        row["cogs"] += r.cogs or D0
    return sorted((_profit(r) for r in agg.values()), key=lambda r: r["netSales"], reverse=True)


async def person_products(period: Period, name: str, branch_id: str | None = None) -> list[dict]:
    """Everything one person sold: each item with units, value, profit, bills, days, the share of this person's own
    sales it makes up, and the share of that item's sales this person made."""
    info = await _item_info(period)
    mine = await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name), branch_id)
    skus = {r.product_sku for r in mine}
    item_totals: dict[str, Decimal] = {}
    if skus:
        for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku__in=list(skus)), branch_id):
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


async def person_categories(period: Period, name: str, branch_id: str | None = None) -> list[dict]:
    agg: dict[str, dict] = {}
    for item in await person_products(period, name, branch_id):
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


async def person_product_days(period: Period, name: str, sku: str, branch_id: str | None = None) -> tuple[list[dict], dict]:
    """One person and one item, day by day, with where that sits against the item's and the person's whole period."""
    info = (await _item_info(period)).get(sku, {})
    days: dict[date, dict] = {}
    item_net = item_qty = person_net = D0
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku=sku), branch_id):
        item_net += r.net_sales or D0
        item_qty += r.qty or D0
        if r.cashier_name != name:
            continue
        d = days.setdefault(r.day, {"day": r.day.isoformat(), "invoices": 0, "qty": D0, "netSales": D0, "cogs": D0})
        d["invoices"] += r.invoices
        d["qty"] += r.qty or D0
        d["netSales"] += r.net_sales or D0
        d["cogs"] += r.cogs or D0
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, cashier_name=name), branch_id):
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


async def people_where(period: Period, attr: str, value: str, branch_id: str | None = None) -> list[dict]:
    """Who sells a category or a brand, and how much of it each."""
    wanted = (value or "").strip().lower()
    skus = {sku for sku, meta in (await _item_info(period)).items() if meta.get(attr, "Unclassified").strip().lower() == wanted}
    agg: dict[str, dict] = {}
    if skus:
        for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end, product_sku__in=list(skus)), branch_id):
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


async def top_sellers(period: Period, branch_id: str | None = None) -> dict[str, dict]:
    """For each item, the person who sold the most of it, their share, and how many people sold it at all."""
    per: dict[str, dict[str, Decimal]] = {}
    for r in await _in(BranchProductCashierStat.filter(day__gte=period.start, day__lte=period.end), branch_id):
        people = per.setdefault(r.product_sku, {})
        people[r.cashier_name] = people.get(r.cashier_name, D0) + (r.net_sales or D0)
    out = {}
    for sku, people in per.items():
        name, amount = max(people.items(), key=lambda kv: kv[1])
        total = sum(people.values(), D0)
        out[sku] = {"name": name, "share": _money(amount / total * 100) if total else D0, "people": len(people)}
    return out


async def people_on_day(day_iso: str, branch_id: str | None = None) -> list[dict]:
    """Everyone who sold on one day: their bills and takings, and what those bills were made of."""
    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        return []
    agg: dict[str, dict] = {}
    for r in await _in(BranchCashierStat.filter(day=day), branch_id):
        row = agg.setdefault(r.cashier_name, {"name": r.cashier_name, "invoices": 0, "billTotal": D0, "qty": D0, "netSales": D0, "cogs": D0, "itemSet": set()})
        row["invoices"] += r.invoices
        row["billTotal"] += r.net_sales or D0
    for r in await _in(BranchProductCashierStat.filter(day=day), branch_id):
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


async def returns_handled(period: Period, name: str, branch_id: str | None = None) -> list[dict]:
    return [r for r in await returns_detail(period, branch_id) if r["cashier"] == name]


async def overrides_rung(period: Period, name: str, branch_id: str | None = None) -> list[dict]:
    return [r for r in await discount_overrides(period, branch_id=branch_id) if r["cashier"] == name]

