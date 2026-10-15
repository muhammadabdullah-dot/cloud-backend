"""Purchasing & Suppliers: what the godown asked for, what arrived, and how long it took.

THE TWO FIGURES THAT ARE ONLY HERE
----------------------------------
**Lead time** -- days from raising an order to its first delivery -- and **fill rate** -- units received against
units ordered -- are the two things a buyer actually decides on, and neither is on any existing screen. The
Purchase Orders page shows "N of M received" one order at a time and the Company Dashboard's supplier tiles
deliberately show neither, because `executive_service.supplier_performance` says in its own docstring that it
will not publish an on-time figure it cannot measure. This page measures what can honestly be measured and says
plainly where the data stops.

ONE WINDOW, AND IT IS THE ORDER'S
---------------------------------
Every figure on the page is about orders RAISED inside the dates. A delivery enters only as "what arrived
against one of those orders", never on its own date, so the headline can never disagree with the chart beneath
it -- the same discipline `sales.py` keeps. The one exception is the Deliveries card, which counts arrivals in
the window and says so, and it is there for the reason in the last section below.

VALUE IS SUMMED FROM THE LINES, NOT FROM THE ORDER HEADER
---------------------------------------------------------
`warehouse_purchase_orders.total` and the sum of its lines disagree on **406 of 883 orders**, in both
directions: the header is bigger on 170 and smaller on 236, and one header (PO 1510794036) is **negative**, Rs
-34,954.74 against Rs 65,000.00 of lines. Two causes, and neither is fixable from here. Multi-Tec's own header
figure is net of things their lines do not carry, and `scripts/load_legacy_godown.py` skips any line whose
product is not in our catalogue, so an order can arrive with a complete header and incomplete lines.

The lines win, for three reasons: they are the only grain that can carry a fill rate at all, they are what makes
the headline and the charts the same arithmetic, and in aggregate the two barely differ -- Rs 93,792,477.15 from
the lines against Rs 93,584,989.26 from the headers, 0.2% apart. Anyone comparing this page with the Purchase
Orders screen will find small per-order differences, which is why the Ordered card says where its figure comes
from.

THERE IS NO PROMISE DATE, SO THERE IS NO "OVERDUE"
--------------------------------------------------
`expected_at` looks like a promised delivery date and is not one. `ops/legacy-import/map_godown.py` fills it
from `RequiredDate or DeliveryDate`, and against the live data those columns are a copy of the order's own
moment: of 883 orders, **617 have expected_at identical to raised_at to the second**, 826 are on the same day,
266 are strictly BEFORE the order was raised (down to 686 days before), and **not one is after it**.

So every order is already past its "promise" the instant it exists. An overdue count built on it reads 883 of
883, and a supplier late-delivery rate reads 789 of 789 -- 100% for every supplier, which is a number that
looks like a finding and is only a dead column. Both were asked for and neither is drawn. What replaces them is
measured from the deliveries themselves: days to first delivery, and what came short. `alerts_service` still
filters on `expected_at`, which fires on nothing today only because no order holds a status it also asks for.

WHAT "OPEN" MEANS HERE, GIVEN THAT ONLY THREE STATUSES ARE LIVE
---------------------------------------------------------------
`models/purchasing.py` documents eight statuses. The table holds three: **closed 562, received 277, draft 44**,
and all 883 rows are `source = 'legacy'` -- not one order has been raised on these screens yet. So
`purchasing_service.OPEN`, which is draft plus three statuses that do not occur, means the 44 drafts and
nothing else, and a draft has nothing received against it by definition (measured: 0 of 1,227 draft lines).

That is why drafts are held apart from the received figures rather than dragged into them. A draft's whole value
counted as "short" would have made the shortfall look Rs 7.6 million worse than it is, when nobody has placed
those orders at all.

Where the real shortfall lives is the closed orders: closed means the rest will never come, and they received
172,304 of 237,837 units ordered (72.4%), against 55,531 of 56,319 (98.6%) on the orders marked received.

The status broadly agrees with the lines and the lines are the finer truth. Of the 562 closed orders, 479 are
part received, 52 got nothing at all and 31 got everything; of the 277 marked received, 275 got everything and
**2 got nothing** -- PO 1514176036 (1,179 units, Rs 281,641.26) and PO 1513955036 (90 units, Rs 27,370.04), both
raised in the last fortnight, so it may simply be their receiving catching up. Either way the lines are what is
counted, which is why those two show as short rather than as received in full.

A PURCHASE ORDER DOES NOT NAME A BRANCH
---------------------------------------
`for_branch_id` exists on the order and on the line, and it is NULL on all 883 orders and all 17,918 lines.
Head office buys centrally, the goods land in the godown, and they reach a branch later as a transfer, so there
is nothing to read. A clicked branch therefore narrows nothing here, and rather than quietly ignoring it the
page returns empty with `NO_BRANCH` on it.

Department, category, brand and Item DO narrow everything. They are not on the order or its lines; they are on
the product, and every line has one -- measured: 17,918 of 17,918 lines join to a product and all 17,918 carry
a category and a brand. A supplier is the one thing on this page that cannot be clicked, because the five
dimensions the app can send back are fixed in `dashboards_service.DIMENSIONS` and a supplier is not one of
them.

MOST DELIVERIES NEVER WENT THROUGH AN ORDER
-------------------------------------------
**5,307 of 6,124 deliveries carry no purchase order**, which is 87%. Without that fact on the page, "ordered
against received" reads as though it covered the godown's buying, when it covers an eighth of its arrivals. It
is one card and it earns its place.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import dialect
from app.dashboards._shape import bars, card, donut, money, pct, table
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Purchasing & Suppliers",
    "blurb": "What was ordered, what arrived, how long it took, and who came short. "
             "Click a category, a brand or an Item to narrow the whole page.",
    "resource": "warehouse.purchase-orders",
    "pages": [
        {"id": "overview", "label": "Ordered & Received"},
        {"id": "suppliers", "label": "Suppliers"},
    ],
}

# Every dimension this page honours, with the column it narrows and that column's own width. The width matters:
# `dialect.mark` casts the parameter so SQL Server does not convert the column instead, and the dialect's own
# warning is that wider is safe and narrower truncates silently, so each one is the column's real type.
ITEM_DIMENSIONS = {
    "sku": ("p.sku", "varchar(60)"),
    "department": ("p.department", "varchar(80)"),
    "category": ("p.category", "varchar(80)"),
    "brand": ("p.brand", "varchar(120)"),
}

# A draft is held out of everything to do with receiving. Not a style choice: 0 of 1,227 draft lines have
# anything received against them, because `purchasing_service.receive_against` refuses an order that is not
# approved, so counting them would only make the shortfall look Rs 7.6 million worse than it is.
PLACED = "o.status <> 'draft'"

NO_BRANCH = ("Head office buys centrally: the goods land in the godown and reach a branch later as a transfer, so "
             "an order only names a branch when it was raised for one. The filter is applied -- `for_branch_id` "
             "on the order, or on any of its lines for an order split between branches -- and no order in these "
             "dates carries it. Clear the branch filter, or widen the dates.")

NO_SUPPLIER_CLICK = ("A supplier is not one of the five things the page can be narrowed by, so a supplier row "
                     "cannot be clicked.")
SEE_SUPPLIERS_TAB = " The Suppliers tab has every one of them with its fill rate and its waiting time."


# ── two fragments the shared dialect helpers do not cover ───────────────────────────────────────
def _day_of(column: str) -> str:
    """The date part of a datetime column, on either engine.

    `dialect` has `days_since`, which measures to now, and `year_month`, which keeps the month. Neither is this,
    and this lives here until a second dashboard wants it.

    Both halves are forced. On SQL Server `SUBSTRING` refuses a date outright, which is the failure
    `dialect.year_month` is written up for. On SQLite `CAST(x AS date)` is worse than an error: DATE carries no
    text affinity, so the cast is NUMERIC and '2026-10-12 13:19:52' quietly becomes 2026. These tables are read
    on both, because thirteen of the checks in `ops/checks` run against a copy of cloud.db.
    """
    if dialect.engine() == dialect.MSSQL:
        return f"CAST({column} AS date)"
    return f"substr({column}, 1, 10)"


def _days_between(start: str, end: str) -> str:
    """Whole CALENDAR days from one datetime column to another: how many midnights are between them.

    MIDNIGHTS AND NOT TWENTY-FOUR HOUR BLOCKS, which is the whole reason this is not one line. SQL Server's
    `DATEDIFF(day, a, b)` counts the day boundaries crossed, so an order raised at 16:30 and delivered at 10:00
    the next morning is 1. A plain julianday subtraction measures elapsed time, so the same pair is 0.72 days,
    and `CAST(... AS INTEGER)` truncates that to 0. Both answers are defensible and having a DIFFERENT one per
    engine is not: the lead time chart would move orders between the "Same day" and "Next day" bands depending
    on which database it was read from, with nothing on screen to say so. These shops order in the afternoon and
    take delivery the next morning often enough for it to matter -- 175 of 789 orders land in the next-day band.

    `date()` strips the time on SQLite, so both sides are midnight and the difference is an exact whole number
    of days. That is DATEDIFF's reading, on both engines.
    """
    if dialect.engine() == dialect.MSSQL:
        return f"DATEDIFF(day, {start}, {end})"
    return f"CAST(julianday(date({end})) - julianday(date({start})) AS INTEGER)"


# ── the scope, at the two grains this page reads ────────────────────────────────────────────────
def _orders() -> str:
    """The orders table with a real date column on it, as a derived table.

    WHY THE DATE IS COMPUTED RATHER THAN COMPARED AS IT IS. `raised_at` is `datetime2`; `Scope.where` binds the
    window as two `date` values. A date has lower precedence than a datetime2, so SQL Server converts the bound
    to midnight and `raised_at <= 2026-10-12` drops everything raised during the afternoon of the 12th.
    Measured, on the window this was first tried with: 99 orders instead of 100, Rs 11,423,541.92 instead of Rs
    11,498,745.92. Over the full history it loses 1 of 881. Nothing errors, the page is simply short of a day.

    Exposing `day` here means `Scope.where("o", "day", only=())` is correct rather than nearly correct, and at
    883 orders the derived table costs nothing worth measuring.
    """
    return (f"FROM (SELECT po.id, po.po_number, po.status, po.supplier_id, po.raised_at, po.for_branch_id, "
            f"             {_day_of('po.raised_at')} AS day "
            f"        FROM warehouse_purchase_orders po) o")


def _first_delivery() -> str:
    """One row per order that has been delivered against, holding the first arrival.

    The FIRST and not the last, because lead time is how long the buyer waited to see anything. Measured: 763
    of 789 delivered orders arrived in a single delivery, 24 in two and 2 in three, so the two readings differ
    on 26 orders -- but on those 26 the difference is the whole point.
    """
    return (f"SELECT g0.purchase_order_id AS purchase_order_id, MIN(g0.{dialect.quote('at')}) AS first_at "
            f"  FROM warehouse_grns g0 WHERE g0.purchase_order_id IS NOT NULL "
            f" GROUP BY g0.purchase_order_id")


def _picked_items(scope: Scope) -> tuple[list[str], list]:
    """Whatever Item dimension was clicked, as conditions on a line's product."""
    conditions, args = [], []
    for name, value in scope.picked.items():
        found = ITEM_DIMENSIONS.get(name)
        if not found:
            continue
        column, sql_type = found
        conditions.append(f"{column} = {dialect.mark(sql_type)}")
        args.append(value)
    return conditions, args


def _picked_branch(scope: Scope, alias: str = "o") -> tuple[list[str], list]:
    """A clicked branch, as a condition on an order.

    TWO COLUMNS, BOTH OF THEM, because an order can say it two ways. `for_branch` on the header is for an order
    raised wholly for one branch; an order split between branches carries the branch on each LINE instead, and
    `models/purchasing.py` says exactly that where the two columns are declared: "A single branch on the ORDER
    cannot say that, which is why the split lives here on the lines". Reading only the header would miss every
    split order, and reading only the lines would miss every whole one.

    It applies to an empty column today -- 0 of 883 orders and 0 of 17,918 lines carry a branch, checked on 15
    October 2026, because head office buys centrally and the goods reach a branch as a transfer. The filter is
    still applied rather than refused: the page then says truthfully that nothing in these dates was raised for
    that branch, and the day the pharmacy short road starts filling the column this needs no change at all.
    """
    value = scope.picked.get("branch")
    if not value:
        return [], []
    mark = dialect.mark()
    return ([f"({alias}.for_branch_id = {mark} OR EXISTS ("
             f"   SELECT 1 FROM warehouse_purchase_order_lines lb"
             f"    WHERE lb.purchase_order_id = {alias}.id AND lb.for_branch_id = {mark}))"], [value, value])


def _lines(scope: Scope) -> tuple[str, str, list]:
    """The FROM and WHERE for everything measured per line: value, units, fill rate.

    `products` is joined INNER rather than LEFT because no line is lost by it -- 0 of 17,918 are orphaned -- and
    an inner join is what lets a category narrow the same rows the value is summed from.
    """
    where, args = scope.where("o", "day", only=())
    conditions, dim_args = _picked_items(scope)
    branch, branch_args = _picked_branch(scope)
    # The order the values are appended in is the order the placeholders appear in the joined string. Getting
    # that wrong binds a branch id to a category and fails silently with an empty page, so the two stay together.
    return (f"{_orders()} "
            f"  JOIN warehouse_purchase_order_lines l ON l.purchase_order_id = o.id "
            f"  JOIN products p ON p.id = l.product_id",
            " AND ".join([where] + conditions + branch), args + dim_args + branch_args)


def _order_grain(scope: Scope) -> tuple[str, str, list]:
    """The FROM and WHERE for everything measured per order: how many, and how long they waited.

    A clicked category reaches an order through an EXISTS over its lines, so this counts "orders that contained
    that category". That is the same honest reading `dashboards_service.invoices` gives for a bill, and it is
    the only one available: an order has no category of its own, its lines do.
    """
    where, args = scope.where("o", "day", only=())
    conditions = [where]
    picked, dim_args = _picked_items(scope)
    if picked:
        conditions.append(
            "EXISTS (SELECT 1 FROM warehouse_purchase_order_lines lx "
            "          JOIN products p ON p.id = lx.product_id "
            f"         WHERE lx.purchase_order_id = o.id AND {' AND '.join(picked)})")
    branch, branch_args = _picked_branch(scope)
    return _orders(), " AND ".join(conditions + branch), args + dim_args + branch_args


# ── the folds ───────────────────────────────────────────────────────────────────────────────────
async def totals(scope: Scope) -> dict:
    """The strip across the top, from the same lines every chart beneath it reads.

    Placed and draft are summed separately in one pass rather than in two queries, because the cards need both
    and the difference between them is the whole reason the strip is honest.
    """
    frm, where, args = _lines(scope)
    row = (await ds._rows(f"""
        SELECT COUNT(DISTINCT o.id) AS orders,
               COUNT(DISTINCT CASE WHEN o.status = 'draft' THEN o.id END) AS drafts,
               COUNT(DISTINCT o.supplier_id) AS suppliers,
               COUNT(DISTINCT o.day) AS days,
               COUNT(*) AS n_lines,
               COUNT(DISTINCT p.sku) AS items,
               COUNT(DISTINCT p.category) AS categories,
               COUNT(DISTINCT p.brand) AS brands,
               SUM(l.qty) AS qty,
               SUM(l.received_qty) AS received_qty,
               SUM(l.qty * l.unit_cost) AS value,
               SUM(CASE WHEN o.status = 'draft' THEN l.qty * l.unit_cost ELSE 0 END) AS draft_value,
               SUM(CASE WHEN {PLACED} THEN l.qty * l.unit_cost ELSE 0 END) AS placed_value,
               SUM(CASE WHEN {PLACED} THEN l.received_qty * l.unit_cost ELSE 0 END) AS received_value,
               SUM(CASE WHEN {PLACED} THEN l.qty ELSE 0 END) AS placed_qty,
               SUM(CASE WHEN {PLACED} THEN l.received_qty ELSE 0 END) AS received_placed_qty,
               SUM(CASE WHEN {PLACED} THEN 1 ELSE 0 END) AS placed_lines,
               SUM(CASE WHEN {PLACED} AND l.received_qty < l.qty THEN 1 ELSE 0 END) AS short_lines,
               SUM(CASE WHEN {PLACED} AND l.received_qty = 0 THEN 1 ELSE 0 END) AS nothing_lines,
               SUM(CASE WHEN l.received_qty > l.qty THEN 1 ELSE 0 END) AS over_lines
          {frm} WHERE {where}
    """, args))[0]
    return row


async def spread(scope: Scope) -> list[dict]:
    """How many orders waited how many days for a first delivery, as a distribution and not a list.

    One row per distinct day count -- 27 rows over the whole history -- which is enough to band, to average and
    to take an exact median from, and it is a GROUP BY rather than 789 rows carried into Python.

    DERIVED TABLES AND NOT A COMMON TABLE EXPRESSION. This wants the day count both grouped and counted, which
    reads naturally as `WITH waits AS (...)`, and SQL Server accepts `WITH` only at the start of a statement:
    the moment it is wrapped it answers "Incorrect syntax near the keyword 'WITH'". The same wall `abc.py`
    documents. Nested subqueries nest on both engines.
    """
    frm, where, args = _order_grain(scope)
    return await ds._rows(f"""
        SELECT x.days AS days, COUNT(*) AS orders
          FROM (SELECT {_days_between('o.raised_at', 'g.first_at')} AS days
                  {frm} JOIN ({_first_delivery()}) g ON g.purchase_order_id = o.id
                 WHERE {where}) x
         GROUP BY x.days ORDER BY x.days
    """, args)


async def by_status(scope: Scope) -> list[dict]:
    """Ordered value by where the order stands. Three bars, because three statuses occur."""
    frm, where, args = _lines(scope)
    return await ds._rows(f"""
        SELECT o.status AS key_, SUM(l.qty * l.unit_cost) AS net,
               SUM(l.received_qty * l.unit_cost) AS received_value,
               SUM(l.qty) AS qty, COUNT(DISTINCT o.id) AS orders
          {frm} WHERE {where}
         GROUP BY o.status ORDER BY SUM(l.qty * l.unit_cost) DESC
    """, args)


async def by_item_dimension(scope: Scope, dimension: str, limit: int = 12) -> list[dict]:
    """One clickable Item dimension, biggest spend first."""
    found = ITEM_DIMENSIONS.get(dimension)
    if not found:
        return []
    column = found[0]
    frm, where, args = _lines(scope)
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} {column} AS key_, {column} AS label,
               SUM(l.qty * l.unit_cost) AS net,
               SUM(CASE WHEN {PLACED} THEN l.received_qty * l.unit_cost ELSE 0 END) AS received_value,
               SUM(l.qty) AS qty
          {frm} WHERE {where} AND {column} IS NOT NULL AND {column} <> ''
         GROUP BY {column} ORDER BY SUM(l.qty * l.unit_cost) DESC {dialect.limit(limit)}
    """, args)


async def by_supplier(scope: Scope, limit: int = 40) -> list[dict]:
    """Each supplier at both grains in one statement: value and fill rate per line, waiting time per order.

    TWO DERIVED TABLES AND NOT ONE JOIN. A lead time belongs to an order and a fill rate belongs to a line, and
    an order carries about twenty lines, so averaging the waiting time over the joined rows would weight each
    supplier's lead time by how many Items it happened to send on an order. The two grains are therefore grouped
    separately and joined supplier to supplier, which is the shape `executive_service.supplier_performance`
    reaches with two round trips; one statement does it here because nested derived tables nest.

    LEFT JOIN on the second: a supplier with orders and no delivery at all is exactly who this table is for, and
    an inner join would hide them.
    """
    frm, where, args = _lines(scope)
    ofrm, owhere, oargs = _order_grain(scope)
    days = _days_between("o.raised_at", "g.first_at")
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} a.*, b.delivered, b.wait_total, b.slowest
          FROM (SELECT o.supplier_id AS key_, MIN(s.name) AS label, MIN(s.code) AS code,
                       COUNT(DISTINCT o.id) AS orders,
                       COUNT(DISTINCT CASE WHEN o.status = 'draft' THEN o.id END) AS drafts,
                       COUNT(*) AS n_lines, COUNT(DISTINCT p.sku) AS items,
                       SUM(l.qty) AS qty,
                       SUM(l.qty * l.unit_cost) AS net,
                       SUM(CASE WHEN {PLACED} THEN l.qty ELSE 0 END) AS placed_qty,
                       SUM(CASE WHEN {PLACED} THEN l.received_qty ELSE 0 END) AS received_qty,
                       SUM(CASE WHEN {PLACED} THEN l.qty * l.unit_cost ELSE 0 END) AS placed_value,
                       SUM(CASE WHEN {PLACED} THEN l.received_qty * l.unit_cost ELSE 0 END) AS received_value
                  {frm} JOIN suppliers s ON s.id = o.supplier_id
                 WHERE {where}
                 GROUP BY o.supplier_id) a
          LEFT JOIN (SELECT o.supplier_id AS key_, COUNT(*) AS delivered,
                            SUM({days}) AS wait_total, MAX({days}) AS slowest
                       {ofrm} JOIN ({_first_delivery()}) g ON g.purchase_order_id = o.id
                      WHERE {owhere}
                      GROUP BY o.supplier_id) b ON b.key_ = a.key_
         ORDER BY a.net DESC {dialect.limit(limit)}
    """, args + oargs)


async def short_items(scope: Scope, limit: int = 60) -> list[dict]:
    """The Items that did not turn up, worst gap in money first.

    Drafts are out by the `PLACED` rule above: nothing has been received against them because nobody has placed
    them, so they would fill this table with the whole order book. The HAVING is what makes it a list of
    shortfalls rather than a list of Items -- an Item that arrived in full has no business here.
    """
    frm, where, args = _lines(scope)
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} p.sku AS key_, MIN(p.name) AS label,
               MIN(p.category) AS category, MIN(p.brand) AS brand,
               COUNT(DISTINCT o.id) AS orders, COUNT(DISTINCT o.supplier_id) AS suppliers,
               SUM(l.qty) AS qty, SUM(l.received_qty) AS received_qty,
               SUM(l.qty * l.unit_cost) AS net,
               SUM(l.received_qty * l.unit_cost) AS received_value
          {frm} WHERE {where} AND {PLACED}
         GROUP BY p.sku
        HAVING SUM(l.qty) > SUM(l.received_qty)
         ORDER BY SUM(l.qty * l.unit_cost) - SUM(l.received_qty * l.unit_cost) DESC {dialect.limit(limit)}
    """, args)


async def drafts(scope: Scope, limit: int = 60) -> list[dict]:
    """The orders nobody has placed yet, biggest first. This is what "open" means on this data."""
    frm, where, args = _lines(scope)
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} o.id AS key_, MIN(o.po_number) AS po_number,
               MIN(s.name) AS label, MIN(s.code) AS code, MIN(o.day) AS day,
               COUNT(*) AS n_lines, SUM(l.qty) AS qty, SUM(l.qty * l.unit_cost) AS net
          {frm} JOIN suppliers s ON s.id = o.supplier_id
         WHERE {where} AND o.status = 'draft'
         GROUP BY o.id ORDER BY SUM(l.qty * l.unit_cost) DESC {dialect.limit(limit)}
    """, args)


async def trend(scope: Scope, by_month: bool) -> list[dict]:
    """Ordered value over time, by day for a short window and by month for a long one.

    The chart is 1,000 units wide and spaces its points evenly, so a two year window by day is 1,016 points at
    one unit each and nothing can be read off it. Switching at a quarter turns that into 34 months. The cut is
    arbitrary in the way any cut is; what is not arbitrary is that the label says which grain is being drawn.
    """
    frm, where, args = _lines(scope)
    label = dialect.year_month("o.raised_at") if by_month else "o.day"
    return await ds._rows(f"""
        SELECT {label} AS label, SUM(l.qty * l.unit_cost) AS net,
               SUM(CASE WHEN {PLACED} THEN l.received_qty * l.unit_cost ELSE 0 END) AS received_value,
               SUM(l.qty) AS qty, COUNT(DISTINCT o.id) AS orders
          {frm} WHERE {where}
         GROUP BY {label} ORDER BY label
    """, args)


async def deliveries(scope: Scope) -> dict:
    """Arrivals in the window, and how many of them came against no order at all.

    The only fold on the page read on the DELIVERY's own date rather than the order's, which is why it is one
    card and not a chart: mixing two date grains into the same picture is how a page comes to disagree with
    itself. It is here because 5,307 of 6,124 deliveries carry no purchase order, so a page about ordering
    covers an eighth of what arrives, and nobody should have to know that to read it correctly.
    """
    where, args = scope.where("g", "day", only=())
    conditions = [where]
    picked, dim_args = _picked_items(scope)
    if picked:
        # A delivery has no Item either; its lines do. Same reading as the orders above.
        conditions.append(
            "EXISTS (SELECT 1 FROM warehouse_grn_lines gl JOIN products p ON p.id = gl.product_id "
            f"        WHERE gl.grn_id = g.id AND {' AND '.join(picked)})")
    # A delivery has no branch either, and unlike an Item it has no line that carries one: it reaches a branch
    # only through the order it came against. A delivery against no order at all therefore drops out of a branch
    # window, which is right -- 5,307 of 6,124 arrive against no order, and none of them is for anybody's branch.
    branch, branch_args = _picked_branch(scope, "po")
    if branch:
        conditions.append(
            "EXISTS (SELECT 1 FROM warehouse_purchase_orders po "
            f"        WHERE po.id = g.purchase_order_id AND {' AND '.join(branch)})")
    dim_args = dim_args + branch_args
    return (await ds._rows(f"""
        SELECT COUNT(*) AS deliveries,
               SUM(CASE WHEN g.purchase_order_id IS NULL THEN 1 ELSE 0 END) AS no_order,
               COUNT(DISTINCT g.supplier_id) AS suppliers
          FROM (SELECT wg.id, wg.purchase_order_id, wg.supplier_id,
                       {_day_of('wg.' + dialect.quote('at'))} AS day
                  FROM warehouse_grns wg) g
         WHERE {' AND '.join(conditions)}
    """, args + dim_args))[0]


async def order_span() -> dict:
    """The first and last day any order was raised, for the sentence an empty window gets.

    Only asked on the empty path. "There is nothing here" is a worse answer than "there is nothing here, and
    here is where the orders actually are", and on 883 rows spread over two years that is the likely mistake.
    """
    return (await ds._rows(
        f"SELECT MIN({_day_of('po.raised_at')}) AS first_day, MAX({_day_of('po.raised_at')}) AS last_day "
        f"  FROM warehouse_purchase_orders po", []))[0]


# ── reading the folds ───────────────────────────────────────────────────────────────────────────
# Bands rather than a bar per day count: 27 distinct day counts over the whole history, most of them holding one
# or two orders, is a chart of noise. The first band only ever appears when it has something in it, and today it
# holds exactly one order whose delivery is dated the day before the order itself -- a real row in their data,
# shown rather than tidied away.
LEAD_BANDS = (
    ("Dated before the order", None, -1),
    ("Same day", 0, 0),
    ("Next day", 1, 1),
    ("2 to 3 days", 2, 3),
    ("4 to 7 days", 4, 7),
    ("8 to 14 days", 8, 14),
    ("More than two weeks", 15, None),
)


def _banded(rows: list[dict]) -> list[dict]:
    out = []
    for label, low, high in LEAD_BANDS:
        orders = sum(int(r["orders"] or 0) for r in rows
                     if (low is None or int(r["days"]) >= low) and (high is None or int(r["days"]) <= high))
        if orders:
            out.append({"key_": label, "label": label, "net": orders})
    return out


def _median_days(rows: list[dict]) -> int | None:
    """The middle order's wait, taken from the distribution rather than from a sorted list of orders.

    The median and not the average, because the average is not robust here: one order of 789 waited 216 days,
    and it drags the all-time average to 2.6 days against a median of 1. Both are shown, the typical one as the
    figure and the average beside it, so neither hides the other.
    """
    total = sum(int(r["orders"] or 0) for r in rows)
    if not total:
        return None
    seen, half = 0, (total + 1) // 2
    for row in sorted(rows, key=lambda r: int(r["days"])):
        seen += int(row["orders"] or 0)
        if seen >= half:
            return int(row["days"])
    return None


def _rs(value) -> str:
    """Rupees with separators, for the line under a card.

    The app formats a card's VALUE and prints its `sub` exactly as given, so a figure in a sub has to arrive
    readable or it reads as 7540055.61.
    """
    return f"Rs {ds._d(value).quantize(Decimal('1')):,}"


STATUS_WORDS = {
    "draft": "Still a draft",
    "received": "Received in full",
    "closed": "Closed, whatever is left will not come",
    "pending_approval": "Waiting for approval",
    "approved": "Approved, nothing received yet",
    "partially_received": "Part received",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
}


def _empty(page_id: str, title: str, note: str) -> dict:
    """A page that cannot answer, saying so once rather than drawing zeroes.

    No cards at all, deliberately. A strip of Rs 0 under a note explaining that nothing was counted reads as a
    catastrophic month to anyone who skims, and the whole point of the note is that nothing was counted. 883
    orders spread over two years is few enough that an ordinary thirty day window can be genuinely empty, so
    this is a normal answer and not an error.

    `covers` is empty rather than a row of zeroes, and it is returned rather than left to the controller, which
    would otherwise read 1.87 million product-days of sales to put a sales count above a purchasing page that
    counted nothing. See `build` for the full reason.
    """
    return {
        "covers": [],
        "cards": [],
        "visuals": [bars(f"{page_id}_empty", title, None, [], note=note)],
    }


async def build(page_id: str, scope: Scope) -> dict:
    if page_id not in {"overview", "suppliers"}:
        raise KeyError(page_id)

    head = await totals(scope)
    orders = int(head["orders"] or 0)
    if not orders:
        dates = f"between {scope.start:%d %b %Y} and {scope.end:%d %b %Y}"
        # A branch gets its own sentence ahead of the general one, because the cure is different again: it is
        # not that the dates are wrong or that nothing matched the click, it is that central buying does not
        # mark an order for a branch at all yet, and no amount of widening will change that.
        if "branch" in scope.picked:
            return _empty(page_id, "No order in these dates was raised for that branch", NO_BRANCH)
        # Which of the two emptied the page matters, because the cures are opposite: widen the dates, or drop
        # the filter. Saying both in one sentence is how a reader ends up doing neither.
        if scope.picked:
            return _empty(page_id, "Nothing was ordered that matched",
                          f"No purchase order raised {dates} contained what you clicked. "
                          f"Clear that filter, or widen the dates.")
        span = await order_span()
        first, last = span.get("first_day"), span.get("last_day")
        where = (f" Orders run from {str(first)[:10]} to {str(last)[:10]}." if first and last
                 else " There are no purchase orders at all yet.")
        return _empty(page_id, "No orders in these dates",
                      f"No purchase order was raised {dates}.{where} Widen the dates to see the page.")

    value = ds._d(head["value"])
    placed_value = ds._d(head["placed_value"])
    received_value = ds._d(head["received_value"])
    draft_value = ds._d(head["draft_value"])
    placed_qty = ds._d(head["placed_qty"])
    received_placed_qty = ds._d(head["received_placed_qty"])
    n_drafts = int(head["drafts"] or 0)
    placed_lines = int(head["placed_lines"] or 0)
    short_lines = int(head["short_lines"] or 0)

    waits = await spread(scope)
    delivered = sum(int(r["orders"] or 0) for r in waits)
    typical = _median_days(waits)
    average = (sum(int(r["days"]) * int(r["orders"] or 0) for r in waits) / delivered) if delivered else 0.0
    slowest = max((int(r["days"]) for r in waits), default=0)
    arrivals = await deliveries(scope)

    # WHAT THIS PAGE COVERS, SAID IN ITS OWN WORDS, and returned rather than left to the controller. Left out,
    # the controller falls back to `dashboards_service.headline`, which reads 1.87 million product-days of
    # SALES: it would print "68,030 Items, 4 branches" above a purchasing page that touched 6,915 Items and no
    # branch at all, and it would read that whole table to do it.
    #
    # Suppliers lead, because that is the count a buyer looks for and it is the one dimension here that no
    # sales dashboard has. Branches are left out entirely rather than reported as 0, which is what the older
    # five-key shape forced: the number is not zero so much as not a thing this data records. See NO_BRANCH.
    covers = [
        {"label": "suppliers", "value": int(head["suppliers"] or 0)},
        {"label": "Items", "value": int(head["items"] or 0)},
        {"label": "categories", "value": int(head["categories"] or 0)},
        {"label": "brands", "value": int(head["brands"] or 0)},
        {"label": "days with an order", "value": int(head["days"] or 0)},
    ]

    cards = [
        card("Orders", f"{orders}", kind="number",
             sub=f"from {int(head['suppliers'] or 0)} supplier(s), "
                 f"{int(head['items'] or 0):,} Item(s) on {int(head['n_lines'] or 0):,} line(s)"),
        card("Ordered", money(value), sub="summed from the order lines, not the order header"),
        card("Received", money(received_value),
             sub=f"{pct(received_value, placed_value)}% of the {_rs(placed_value)} actually placed"),
        card("Short", money(placed_value - received_value),
             sub=f"on {short_lines:,} of {placed_lines:,} placed line(s), "
                 f"{int(head['nothing_lines'] or 0):,} of which got nothing"),
        card("Still a Draft", f"{n_drafts}", kind="number",
             sub=(f"{_rs(draft_value)} that nobody has placed, so nothing can have arrived" if n_drafts
                  else "every order in this window was submitted")),
        card("Units Ordered", money(head["qty"]), kind="number",
             sub=f"{pct(received_placed_qty, placed_qty)}% of placed units arrived"),
        card("Days to First Delivery", "not yet" if typical is None else f"{typical}", kind="number",
             sub=(f"typical of {delivered} delivered order(s); average {average:.1f}, slowest {slowest}"
                  if delivered else "no order in this window has been delivered against")),
        card("Deliveries", f"{int(arrivals['deliveries'] or 0)}", kind="number",
             sub=f"arrived in these dates, {int(arrivals['no_order'] or 0):,} of them against no order"),
    ]

    visuals: list[dict] = []

    if page_id == "overview":
        # A quarter. See `trend` for why the grain switches and not just the labels.
        by_month = (scope.end - scope.start).days > 92
        points = await trend(scope, by_month)
        visuals.append({
            "id": "trend", "kind": "line", "dimension": None,
            "title": f"What was ordered, by {'month' if by_month else 'day'}",
            "note": None if points else "Nothing was ordered in this window.",
            "rows": [{"label": str(r["label"])[:10], "value": money(r["net"]),
                      "received": money(r["received_value"]), "qty": money(r["qty"]),
                      "orders": str(int(r["orders"] or 0))} for r in points],
        })

        suppliers = await by_supplier(scope, 12)
        visuals.append(bars("suppliers", "Ordered, by supplier", None,
                            [{"key_": r["key_"], "label": r["label"] or r["code"] or r["key_"],
                              "net": r["net"], "qty": r["qty"]} for r in suppliers],
                            note=NO_SUPPLIER_CLICK + SEE_SUPPLIERS_TAB))
        # Sorted on the gap in money rather than on the fill rate, because a supplier 40% short on Rs 2,000 is
        # not the one to telephone. Suppliers whose placed orders came in full drop out entirely.
        # No `qty` on these rows on purpose: the supplier's ordered units would sit beside a money figure that
        # is a shortfall, which is two different questions in one row.
        gaps = sorted(
            ({"key_": r["key_"], "label": r["label"] or r["code"] or r["key_"],
              "net": ds._d(r["placed_value"]) - ds._d(r["received_value"])}
             for r in suppliers), key=lambda r: r["net"], reverse=True)
        visuals.append(bars("shortfall", "What a supplier did not deliver, in money", None,
                            [g for g in gaps if g["net"] > 0][:12],
                            note="Ordered less received, on the orders that were actually placed. "
                                 "A supplier missing from this list delivered everything it was sent. "
                                 + NO_SUPPLIER_CLICK + SEE_SUPPLIERS_TAB))

        status_rows = await by_status(scope)
        visuals.append(donut(
            "status", "Where those orders stand", None,
            [{"key_": r["key_"], "label": STATUS_WORDS.get(str(r["key_"]), str(r["key_"])),
              "net": r["net"], "qty": r["qty"]} for r in status_rows],
            # Deliberately not "three bars": narrowed to a category the page can come back with two, and a note
            # asserting a count that the chart beside it contradicts is worse than no note.
            note="Only three of the eight statuses in the order lifecycle occur on this data, and a narrowing "
                 "can leave fewer. Every order here came out of Multi-Tec; none has been raised on these "
                 "screens yet."))

        # Drill down rather than collapse. Pinned to category and brand, narrowing to one of them drew a chart
        # with a single bar in it; `ds.unpinned` hands back the two finest levels still open instead, so inside
        # a category this becomes brand and Item.
        for level in ds.unpinned(scope, ("category", "brand"), 2):
            visuals.append(bars(level, f"Ordered, by {ds.LEVEL_WORD[level]}", level,
                                await by_item_dimension(scope, level, 12)))

        visuals.append(bars(
            "lead", "How long the first delivery took", None, _banded(waits),
            note="Orders, not money. Counted from the day the order was raised to the day its first delivery "
                 "was booked in. Orders with no delivery yet are not in these bars; the Short card above is "
                 "where they show up."))

        draft_rows = await drafts(scope, 60)
        visuals.append(table(
            "drafts", "Orders still sitting as drafts", None,
            [{"key": "po_number", "label": "Order"},
             {"key": "label", "label": "Supplier"},
             {"key": "day", "label": "Raised"},
             {"key": "lines", "label": "Lines", "align": "right"},
             {"key": "qty", "label": "Units", "align": "right"},
             {"key": "value", "label": "Ordered", "align": "right"}],
            [{"key": str(r["key_"]), "po_number": str(r["po_number"] or ""),
              "label": str(r["label"] or r["code"] or ""), "day": str(r["day"])[:10],
              "lines": str(int(r["n_lines"] or 0)), "qty": money(r["qty"]), "value": money(r["net"])}
             for r in draft_rows],
            note="A draft has never been submitted, so nothing can have been received against it. Not "
                 "clickable: an order is not one of the five things the page narrows by."))
        return {"covers": covers, "cards": cards, "visuals": visuals}

    # page_id == "suppliers"
    suppliers = await by_supplier(scope, 60)

    def wait_of(row: dict) -> str:
        count = int(row["delivered"] or 0)
        return f"{ds._d(row['wait_total']) / count:.1f}" if count else "no delivery"

    def fill_of(row: dict) -> str:
        """Units received against units ordered, or "--" when nothing was placed to measure against.

        Units and not money, because a buyer chasing a supplier is chasing cases and the unit figure is the one
        the supplier will argue about. Not clamped at 100 either: 868 of 17,918 lines were delivered OVER, Rs
        3,166,346 worth, which is a real thing suppliers do and belongs on the screen rather than rounded away.
        It is also why Short goes negative on some rows.

        The guard is not theoretical. `pct` answers "0.0" when the whole is zero, and a supplier whose orders in
        the window are all drafts has no placed units at all -- 5 such suppliers in the full history, the
        largest of them THE ORDINARY LAHORE with one draft of Rs 405,200. A fill rate of 0.0% would read as a
        supplier that delivered nothing, when the truth is that nothing was ever asked of them.
        """
        return pct(ds._d(row["received_qty"]), ds._d(row["placed_qty"])) if ds._d(row["placed_qty"]) else "--"

    visuals.append(table(
        "suppliers", "Every supplier in this window", None,
        [{"key": "label", "label": "Supplier"},
         {"key": "code", "label": "Code"},
         {"key": "orders", "label": "Orders", "align": "right"},
         {"key": "drafts", "label": "Of those, drafts", "align": "right"},
         {"key": "delivered", "label": "Delivered against", "align": "right"},
         {"key": "value", "label": "Placed", "align": "right"},
         {"key": "received", "label": "Received", "align": "right"},
         {"key": "short", "label": "Short", "align": "right"},
         {"key": "fill", "label": "Fill rate %", "align": "right"},
         {"key": "wait", "label": "Avg days to first delivery", "align": "right"},
         {"key": "slowest", "label": "Slowest", "align": "right"}],
        [{
            "key": str(r["key_"]), "label": str(r["label"] or r["key_"]), "code": str(r["code"] or ""),
            "orders": str(int(r["orders"] or 0)),
            "drafts": str(int(r["drafts"] or 0)),
            "delivered": str(int(r["delivered"] or 0)),
            # PLACED and not everything ordered, which is what this column first showed and had to be changed.
            # The two differ by the drafts, and reading them together was nonsense: LOCAL PURCHASE had Ordered
            # Rs 2,939,467.87, Received Rs 23,318.80 and Short Rs 6,127.20, because five of its six orders are
            # drafts worth Rs 2,910,021.87 that no shortfall can be measured against. With the placed figure
            # here, Placed less Received is Short on every row, and the Drafts column beside it says why the
            # bars above are bigger.
            "value": money(r["placed_value"]),
            "received": money(r["received_value"]),
            "short": money(ds._d(r["placed_value"]) - ds._d(r["received_value"])),
            "fill": fill_of(r),
            "wait": wait_of(r),
            "slowest": str(int(r["slowest"])) if r["slowest"] is not None else "--",
        } for r in suppliers],
        note="Placed is what was ordered on orders that were actually submitted, so Placed less Received is "
             "Short on every row and the drafts are counted beside it rather than folded in. A negative Short "
             "and a fill rate over 100 both mean the supplier sent more than was asked for. Sorted by the "
             "biggest order book, drafts included, so the order matches the bars on the other tab. "
             + NO_SUPPLIER_CLICK))

    # At least three delivered orders before a supplier is called slow. One order that took a fortnight is a
    # story about that order; three is a story about the supplier. Measured without the floor, the slowest bar
    # was a supplier with a single 216 day order, which told the reader nothing they could act on.
    slow = sorted(({"key_": r["key_"], "label": r["label"] or r["code"] or r["key_"],
                    "net": ds._d(r["wait_total"]) / int(r["delivered"]),
                    "qty": int(r["delivered"])}
                   for r in suppliers if int(r["delivered"] or 0) >= 3),
                  key=lambda r: r["net"], reverse=True)
    visuals.append(bars(
        "slowest", "Average days to first delivery, slowest first", None, slow[:15],
        note="Suppliers with at least three delivered orders in this window, so one unusual order cannot put a "
             "supplier at the top. " + NO_SUPPLIER_CLICK))

    short = await short_items(scope, 60)
    visuals.append(table(
        "short_items", "What came short", "sku",
        [{"key": "label", "label": "Item"},
         {"key": "category", "label": "Category"},
         {"key": "brand", "label": "Brand"},
         {"key": "orders", "label": "Orders", "align": "right"},
         {"key": "qty", "label": "Ordered", "align": "right"},
         {"key": "received", "label": "Received", "align": "right"},
         {"key": "fill", "label": "Fill rate %", "align": "right"},
         {"key": "value", "label": "Short", "align": "right"}],
        [{
            "key": str(r["key_"]), "label": str(r["label"] or r["key_"]),
            "category": str(r["category"] or ""), "brand": str(r["brand"] or ""),
            "orders": str(int(r["orders"] or 0)),
            "qty": money(r["qty"]), "received": money(r["received_qty"]),
            "fill": pct(ds._d(r["received_qty"]), ds._d(r["qty"])),
            "value": money(ds._d(r["net"]) - ds._d(r["received_value"])),
        } for r in short],
        note="Only Items that got less than was ordered, worst gap in money first, and only off orders that "
             "were actually placed. Click one to narrow the whole page to it."))
    return {"covers": covers, "cards": cards, "visuals": visuals}
