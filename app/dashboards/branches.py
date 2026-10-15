"""Branch Performance: every branch ranked, and ranked fairly.

THE ONE IDEA THIS PAGE IS BUILT ROUND
-------------------------------------
A branch that opened three weeks ago is not a worse branch than one that has been trading two years, but it loses
every ranking that adds a period up and sorts on the total. So every figure here that can be is divided by **that
branch's own trading days** -- the days it has a row of its own inside the window -- and the `Days` column sits
beside them so a reader can see the divisor rather than take it on trust.

How far apart those divisors get is not theoretical, and it changes the answer. Over the 30 days to 12 October
2026 three branches traded all 30 and Gulgasht traded 28, which is 7% and easy to miss. Over the whole history
Garden Town has 770 trading days against Model Town's 615, because Model Town's first reported day is 2025-01-31
against Garden Town's 2024-08-28 -- so an all-time total ranks Model Town against five months it did not exist
for. And it reorders the table: all time, Gulgasht takes Rs 117,265,503 against Fort Colony's Rs 116,810,918 and
so places third on the total, but over 745 days against 637 that is Rs 157,403 a day against Rs 183,377, which is
fourth. Two branches swap places purely on how long each has been open.

The legacy screen this detail is taken from showed the same thing as a branch age. Here it is three plain columns
-- how many days inside these dates, how many days in all, and the first day the branch ever reported -- rather
than an invented shorthand, because a reader who has to decode "7M, 2D" is a reader who will not check it.

WHAT NARROWS WHAT, AND WHY HALF THE PAGE GOES QUIET WHEN AN ITEM IS CLICKED
---------------------------------------------------------------------------
`branch_daily_stats`, `branch_cashier_stats` and `branch_till_closes` carry a branch and a day and nothing else:
no department, no category, no brand, no Item. So a bill count, a basket, a till session and a cashier's takings
cannot follow a clicked category, and this says so in plain words rather than drawing a picture of a filter it
never applied. What CAN follow one is revenue, profit and units, because those are also kept per Item in
`branch_product_stats`, so the ranking and the per-day figures stay live under any filter and only the per-bill
half goes quiet.

THE TWO FIGURES IN HERE THAT ARE NOT WORTH TRUSTING YET, MEASURED
----------------------------------------------------------------
  * **till variance is zero everywhere.** 0.00 on all 6,585 rows of `branch_till_closes`, all 6,105 of
    `branch_cashier_stats` and all 2,767 of `branch_daily_stats`. That is not four honest branches, it is a
    column the Multi-Tec loader does not fill: it sends the opening float and the counted cash (both alive) and
    no expected-cash figure to subtract them from. The column is still shown, because it starts meaning
    something the day the loader fills it, and the note says it reads zero and why rather than letting a reader
    conclude the tills are perfect.
  * **net cash on a till close is zero too**, on all 6,585. Counted cash is the live one, and it does not add up
    to the day's takings either: Garden Town counted Rs 16,325,296 across 134 closes in the test window against
    Rs 30,518,886 of cash collected on its daily rows, because cash is lifted off a till mid-shift and only
    Rs 1,493,070 of that is recorded as cash out. Subtracting the two to manufacture a variance would be
    inventing a number, so it is not done.

GULGASHT IS NOT A SHOP LIKE THE OTHER THREE, AND THE PAGE SHOULD NOT BE READ AS IF IT WERE
------------------------------------------------------------------------------------------
Every ratio on this page says the same thing about it, so it is worth stating once rather than being rediscovered
as a bug. Over the test month Gulgasht took Rs 4,703,904 on 121 bills: an average basket of Rs 38,875 against
Garden Town's Rs 1,326, 1.6 items a bill against 10.9, 199 units sold in all across 151 Items, one cashier, one
till, and the best margin of the four at 31.1% against 13.0%. All time the shape holds -- Rs 117,265,503 on 3,802
bills. That is a wholesale counter writing a few large orders, not a till ringing shoppers, and it is not a
broken feed: the bills, the hours and the till sessions are all there and all consistent with each other. It does
mean the per-bill columns compare two different trades, which is exactly why Days and the per-day columns, which
do not, are the ones this page ranks on.

STOCK ON HAND IS NEVER SUMMED
-----------------------------
`branch_daily_stats.stock_value` is carried on each branch's most recent reported day only -- one row per branch
in the whole table, measured -- so adding it over a date range multiplies it by the number of days in the range.
It is read here exactly the way `executive_service.latest_stock_value` reads it, from each branch's own latest
day whatever the dates above say, and the column says so.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.dashboards._shape import bars, card, donut, line, money, pct, table
from app.models import selling_branches
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Branch Performance",
    "blurb": "Every branch ranked, and every per-day figure divided by the days that branch actually traded. "
             "Click a branch to narrow the whole page.",
    "resource": "executive.branches",
    "pages": [
        {"id": "overview", "label": "Branches"},
        {"id": "people", "label": "People & Tills"},
    ],
}

D0 = Decimal("0")

# The dimensions that live on an Item and therefore cannot reach a branch-and-day rollup. Named once because four
# different folds have to ask the same question.
ITEM_PICKS = ("department", "category", "brand", "sku")

BILLS_NOT_DIMENSIONAL = (
    "Bills, basket and items per bill are counted per branch per day with no Item on the row, so they cannot "
    "follow a department, a category, a brand or an Item. Revenue, profit, units and the days traded do follow "
    "it, and are what you are looking at. Clear that filter to see the rest.")

PEOPLE_NOT_DIMENSIONAL = (
    "Who rang what is kept per person per day, and a till session per close, with no Item on either row. So this "
    "cannot follow a department, a category, a brand or an Item. Clear that filter to see it.")

NO_TRADING = "No branch in this narrowing has a trading day inside these dates, so there is nothing to rank."

VARIANCE_IS_DEAD = (
    "Till variance reads zero on every row we hold -- all 6,585 closes and all 6,105 cashier days -- because the "
    "nightly load sends the opening float and the counted cash but no expected-cash figure to compare them "
    "against. The column is kept so it starts answering the day that arrives; today it is not a finding.")


# ── small coercions, because the two engines hand dates back differently ────────────────────────
def _day(value) -> date | None:
    """A day column as a date. SQL Server returns a date; SQLite keeps it as text through Tortoise."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _at(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _said(value: date | None) -> str:
    """A date the way people say it (28 Aug 2024), the same shape `executive_service.date_range_label` uses."""
    return f"{value.day} {value:%b %Y}" if value else "--"


def _per(total: Decimal, days: int) -> Decimal:
    """Anything over the branch's own trading days. Zero days is a branch that did not trade, not a divide by one."""
    return total / days if days else D0


# ── the folds ───────────────────────────────────────────────────────────────────────────────────
async def _selling(scope: Scope) -> list:
    """The branches a ranking is about: every one except head office.

    `selling_branches()` rather than `Branch.all()`, and it is not a style preference. Head office has a branches
    row because it owns the HO book and the godown, but it has no till and no bills, and it has never reported a
    trading day: 0 rows in `branch_daily_stats`, `branch_cashier_stats`, `branch_till_closes` and
    `branch_product_stats`, measured. A ranking built from `Branch.all()` therefore grew a fifth row of zeros
    labelled "Never reported", which reads as a branch that has gone quiet rather than as a thing that was never
    a branch. That exact bug is written up on the helper.

    The list, not a join, is what makes a branch with nothing in the window still appear: it shows as a branch
    that did not trade in these dates, which is the answer, where an inner join would simply lose it.
    """
    branches = await selling_branches()
    picked = scope.picked.get("branch")
    # A picked branch narrows the list as well as the figures. Without this the other three come back as rows of
    # nothing, and a page that answers "Garden Town" with three empty branches beneath it is a page nobody reads.
    return [b for b in branches if not picked or str(b.id) == picked]


async def _window(scope: Scope) -> dict[str, dict]:
    """The window's figures per branch, from the one row a branch reports per day.

    `only=("branch",)` because that is every dimension this table has. Passing the rest would ask SQL Server for a
    column named `category` on a table of daily totals, which is what `Scope.where` exists to prevent.

    `days` is COUNT(DISTINCT day) and not the window's length: it is the divisor every per-day figure on this page
    uses, and it has to be the branch's own.
    """
    where, args = scope.where("d", only=("branch",))
    rows = await ds._rows(f"""
        SELECT d.branch_id AS branch_id, COUNT(DISTINCT d.day) AS days,
               SUM(d.net_sales) AS net, SUM(d.gross_sales) AS gross, SUM(d.cogs) AS cogs,
               SUM(d.items_sold) AS items, SUM(d.invoices) AS invoices,
               SUM(d.disc_total) AS disc, SUM(d.returns_value) AS returns_value,
               SUM(d.returns_count) AS returns_count, SUM(d.cash_collected) AS cash,
               SUM(d.credit_sales) AS credit, SUM(d.till_variance) AS variance,
               SUM(d.tills_closed) AS closes, SUM(d.staff_on_duty) AS staff_days,
               SUM(d.named_customers) AS named
          FROM branch_daily_stats d WHERE {where}
         GROUP BY d.branch_id
    """, args)
    return {str(r["branch_id"]): r for r in rows}


async def _by_item(scope: Scope) -> dict[str, dict]:
    """The same revenue, from the Item rows, so the ranking can follow a clicked category.

    `scope.where()` with no `only` here, because `branch_product_stats` really does carry all five dimensions.
    Over the test window it answers Rs 52,806,008.84 against the daily rows' Rs 52,837,005.00 -- a Rs 30,996
    gap on Rs 52.8M, which is the per-bill rounding `executive_service._totals_from_bills` writes up, not a
    disagreement about what sold.
    """
    where, args = scope.where()
    rows = await ds._rows(f"""
        SELECT s.branch_id AS branch_id, SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs,
               SUM(s.qty) AS qty
          FROM branch_product_stats s WHERE {where}
         GROUP BY s.branch_id
    """, args)
    return {str(r["branch_id"]): r for r in rows}


async def _history() -> dict[str, dict]:
    """How long each branch has been trading, over its whole history and not the window.

    Deliberately unbounded by the dates. It is what makes a short `Days` readable: Gulgasht's 28 days in the test
    window is a branch that was shut for two days, while Model Town's 615 days against Garden Town's 770 is a
    branch that opened five months later. Without this the two look the same.

    `last_ever` is here for the other direction: a branch whose last reported day is weeks behind the others has
    gone quiet, and that is a fact about the feed rather than about its trade. It is the honest version of the
    "Never reported" label this page used to put on head office.
    """
    rows = await ds._rows("""
        SELECT a.branch_id AS branch_id, COUNT(DISTINCT a.day) AS ever_days,
               MIN(a.day) AS first_ever, MAX(a.day) AS last_ever
          FROM branch_daily_stats a GROUP BY a.branch_id
    """, [])
    return {str(r["branch_id"]): r for r in rows}


async def _latest_stock() -> dict[str, dict]:
    """Each branch's stock value from its own most recent reported day.

    NOT SUMMED, AND NOT BOUNDED BY THE DATES ABOVE. The column is a right-now figure carried on one row per
    branch in the whole table -- four rows, all dated 2026-10-13, measured -- so SUM() over a 30 day window would
    answer thirty times the truth, and bounding it by a window that ended in March would show March's shelf as
    though it were today's. `executive_service.latest_stock_value` handles it the same way, a branch at a time
    from its latest day, and the card and the column both say the dates do not reach it.
    """
    rows = await ds._rows("""
        SELECT l.branch_id AS branch_id, l.day AS stock_day, l.stock_value AS stock_value
          FROM branch_daily_stats l
         WHERE l.day = (SELECT MAX(x.day) FROM branch_daily_stats x WHERE x.branch_id = l.branch_id)
    """, [])
    return {str(r["branch_id"]): r for r in rows}


async def _cashiers(scope: Scope) -> list[dict]:
    """Who rang what, per branch per person.

    Grouped by the branch AND the name, never the name alone. No name works at two branches in the test window,
    which is exactly why it has to be the pair: the day one does, folding on the name would merge two people's
    takings into a row belonging to neither branch.
    """
    where, args = scope.where("c", only=("branch",))
    return await ds._rows(f"""
        SELECT c.branch_id AS branch_id, c.cashier_name AS name, SUM(c.invoices) AS invoices,
               SUM(c.net_sales) AS net, SUM(c.till_variance) AS variance,
               COUNT(DISTINCT c.day) AS days
          FROM branch_cashier_stats c WHERE {where}
         GROUP BY c.branch_id, c.cashier_name
    """, args)


async def _closes(scope: Scope) -> list[dict]:
    """Every till session in the window, one row each, folded in Python rather than in SQL.

    WHY THE ROWS COME BACK WHOLE
    ----------------------------
    Hours covered is the sum of (closed_at - opened_at) over the sessions, and the two engines have no common way
    to say that: SQL Server has DATEDIFF(minute, ...), SQLite has julianday arithmetic, and `core/dialect.py`
    offers no minutes helper to hide the difference behind. Thirteen of the checks in `ops/checks` run against a
    copy of cloud.db on SQLite, so a bare DATEDIFF here is a hard error there rather than a slower plan.

    Pulling the rows is affordable because of how few there are: 6,585 in the entire table, 319 in the 30 day
    test window. That is the opposite of the product rollup, where grouping in Python is what made the executive
    folds take 34 seconds. One read also serves two folds -- by person and by counter -- which a GROUP BY could
    not do without reading the sessions twice.
    """
    where, args = scope.where("t", only=("branch",))
    return await ds._rows(f"""
        SELECT t.branch_id AS branch_id, t.day AS day, t.session_number AS session_number,
               t.cashier_name AS cashier_name, t.counter_name AS counter_name,
               t.opened_at AS opened_at, t.closed_at AS closed_at,
               t.opening_float AS opening_float, t.counted_cash AS counted_cash, t.variance AS variance
          FROM branch_till_closes t WHERE {where}
         ORDER BY t.day DESC, t.closed_at DESC
    """, args)


# ── folding the till sessions ───────────────────────────────────────────────────────────────────
def _minutes(row: dict) -> int:
    """How long a session was open, in whole minutes, and nothing at all when the pair makes no sense.

    Two of the 6,585 sessions we hold are unusable: one has no `closed_at` at all -- a drawer somebody left open
    -- and one closes at or before it opened. Both read zero. Counting the open one from now instead would make
    it grow every time the page is refreshed, which is worse than reading short by one session.

    Closing after midnight is NOT one of those cases and must not be clamped: 116 of the 319 sessions in the
    test window close on the calendar day after the trading day they belong to, because these shops trade past
    midnight (`executive_service._totals_from_bills` has the proof that the stored times are local). The
    subtraction crosses the turn on its own and gets it right.
    """
    opened, closed = _at(row.get("opened_at")), _at(row.get("closed_at"))
    if not opened or not closed or closed <= opened:
        return 0
    return int((closed - opened).total_seconds() // 60)


def _fold_closes(rows: list[dict], key) -> dict:
    """The sessions grouped however the caller keys them: by branch and person, or by branch and counter."""
    out: dict = {}
    for row in rows:
        group = out.setdefault(key(row), {
            "closes": 0, "minutes": 0, "counted": D0, "float": D0, "variance": D0,
            "people": set(), "counters": set(), "unclosed": 0,
        })
        group["closes"] += 1
        group["minutes"] += _minutes(row)
        group["counted"] += ds._d(row.get("counted_cash"))
        group["float"] += ds._d(row.get("opening_float"))
        group["variance"] += ds._d(row.get("variance"))
        group["people"].add(str(row.get("cashier_name") or "--"))
        group["counters"].add(str(row.get("counter_name") or "--"))
        if not _at(row.get("closed_at")):
            group["unclosed"] += 1
    return out


# ── one row per branch, however the page was narrowed ───────────────────────────────────────────
async def _ranked(scope: Scope) -> tuple[list[dict], bool]:
    """Every selling branch with its window, its history and its own trading-day divisor.

    Returns the rows and whether an Item filter is in force, because that is what decides which half of the row
    is real: revenue, profit and units swap over to the Item rollup so they honour the click, while bills, basket
    and items per bill are left as None for the visuals to print as "--" under a note. They are NOT quietly
    recomputed from a filtered table that cannot answer them, and they are NOT left showing the whole branch's
    bill count beside a single category's revenue, which is the reading that would have a basket of Rs 38,875
    look like a company record rather than an artefact of two different questions.
    """
    dimensional = any(k in scope.picked for k in ITEM_PICKS)
    branches = await _selling(scope)
    window = await _window(scope)
    history = await _history()
    stock = await _latest_stock()
    items = await _by_item(scope) if dimensional else {}

    out: list[dict] = []
    for branch in branches:
        key = str(branch.id)
        w, h, s = window.get(key, {}), history.get(key, {}), stock.get(key, {})
        # The divisor, always from the branch's own calendar and never from the Item rows. A branch was open on a
        # day whether or not it happened to sell the clicked category, so narrowing to Hair Care must not shrink
        # the number of days Garden Town traded -- that would flatter a branch that sells the category rarely.
        days = int(w.get("days") or 0)
        net = ds._d(items[key]["net"]) if dimensional and key in items else ds._d(w.get("net"))
        cogs = ds._d(items[key]["cogs"]) if dimensional and key in items else ds._d(w.get("cogs"))
        qty = ds._d(items[key]["qty"]) if dimensional and key in items else ds._d(w.get("items"))
        if dimensional and key not in items:
            net = cogs = qty = D0
        invoices = None if dimensional else int(w.get("invoices") or 0)
        out.append({
            "key_": key, "label": branch.name, "code": branch.code, "status": branch.status,
            "days": days, "net": net, "cogs": cogs, "qty": qty, "profit": net - cogs,
            # Every one of these over the branch's own days, so a chart drawn from them is per-day throughout
            # rather than a per-day bar with a whole-window figure hanging off its tooltip.
            "per_day": _per(net, days), "cogs_per_day": _per(cogs, days),
            "qty_per_day": _per(qty, days), "profit_per_day": _per(net - cogs, days),
            "invoices": invoices,
            "basket": (net / invoices if invoices else D0) if invoices is not None else None,
            "per_bill": (qty / invoices if invoices else D0) if invoices is not None else None,
            "bills_per_day": (Decimal(invoices) / days if invoices and days else D0)
                             if invoices is not None else None,
            "returns_value": ds._d(w.get("returns_value")), "returns_count": int(w.get("returns_count") or 0),
            # The whole branch's own figures, never swapped for the Item rollup, because none of them exists per
            # Item: a discount is given on a bill and a rupee of credit is given to a person, not to a category.
            # `net_day` is the daily rows' net kept beside the possibly-narrowed `net` above so the money table
            # can put discount and cash against the figure they were actually taken out of.
            "net_day": ds._d(w.get("net")), "gross": ds._d(w.get("gross")), "disc": ds._d(w.get("disc")),
            "cash": ds._d(w.get("cash")), "credit": ds._d(w.get("credit")),
            "variance": ds._d(w.get("variance")), "closes": int(w.get("closes") or 0),
            "staff_days": int(w.get("staff_days") or 0), "named": int(w.get("named") or 0),
            "ever_days": int(h.get("ever_days") or 0), "first_ever": _day(h.get("first_ever")),
            "last_ever": _day(h.get("last_ever")),
            "stock_value": ds._d(s.get("stock_value")), "stock_day": _day(s.get("stock_day")),
        })
    out.sort(key=lambda r: r["net"], reverse=True)
    return out, dimensional


def _covers(scope: Scope, trading: int, branch_days: int, extra: list[dict] | None = None) -> list[dict]:
    """What the strip above the page says this page counted.

    SUPPLIED RATHER THAN LEFT TO THE CONTROLLER, FOR TWO REASONS, AND THE SECOND ONE IS 13 SECONDS
    ----------------------------------------------------------------------------------------------
    Left out, `dashboards_controller.page` falls back to `dashboards_service.headline`, which counts distinct
    Items, categories and brands over `branch_product_stats`. On a branch page those are the wrong five things to
    announce, and on the People page they are also the only reason the Item rollup is read at all: that page
    folds cashier rows and till sessions and touches 1.87 million product-days for nothing. Measured over the
    whole history, through the controller: the People page took 13.70 seconds with the fallback and takes 0.20
    with this, and the Branches page 14.26 against 0.75. The browser gives up at 15, so the all-time view was
    one second away from showing nothing at all, and it would have been spending those seconds counting brands
    on a page that does not mention brands.

    Trading days are counted here as branch-days, not calendar days, because that is the number this page ranks
    on. Over the whole history it is 2,767 branch-days across 771 calendar days, and the two mean different
    things: the second is how long we have been collecting, the first is how much trading there was to read.
    """
    return [
        {"label": "branches trading", "value": trading},
        {"label": "branch trading days", "value": branch_days},
        {"label": "days asked for", "value": (scope.end - scope.start).days + 1},
    ] + (extra or [])


def _timed(scope: Scope, note: str | None = None) -> str | None:
    """Whatever the visual wanted to say, plus the one thing a clock bound cannot do here.

    A branch reports itself a day at a time, so there is no 17:30 in these rollups to cut at. Saying so beats
    showing a whole day's bills under a label that names part of one.
    """
    clock = ("The times on the dates above are not applied: a branch reports one row per trading day, so there "
             "is no part of a day in these figures. The days themselves are the ones you picked.")
    if not scope.timed:
        return note
    return f"{note} {clock}" if note else clock


# ── the pages ───────────────────────────────────────────────────────────────────────────────────
def _overview(scope: Scope, rows: list[dict], dimensional: bool, trend: list[dict]) -> list[dict]:
    bill_note = BILLS_NOT_DIMENSIONAL if dimensional else None
    traded = [r for r in rows if r["days"]]
    # Four rows of zeros under a window nobody traded in reads as four branches that collapsed. Saying nothing
    # was open is the answer, and it is the same shape the HO bug took: a row of zeros with a label on it.
    quiet = None if traded else NO_TRADING

    # SORTED BY THEIR OWN VALUE, not by the total. A bar chart whose bars do not descend is a chart that gets
    # read as broken, and these two genuinely disagree with the revenue order: over the whole history Gulgasht
    # takes Rs 117,265,503 against Fort Colony's Rs 116,810,918, so it ranks third on the total -- but over
    # 745 trading days against Fort Colony's 637 that is Rs 157,403 a day against Rs 183,377, which is fourth.
    # That flip IS the feature, and it only shows if this chart is allowed to put them in its own order.
    per_day_rows = sorted(
        [{"key_": r["key_"], "label": r["label"], "net": r["per_day"],
          "cogs": r["cogs_per_day"], "qty": r["qty_per_day"]} for r in rows],
        key=lambda r: r["net"], reverse=True)
    profit_rows = sorted(rows, key=lambda r: r["profit_per_day"], reverse=True)

    # From `dashboards_service.trend`, which every other dashboard's trend also comes from. Reused rather than
    # rewritten so this page and Sales Analysis cannot disagree about what a day took, and because it reads the
    # Item rollup it is the one visual here that follows a clicked category without help. Its note is set
    # afterwards because `_shape.line` takes no note argument, and a timed window has to be called out here too:
    # the Item rollup is also kept a day at a time, so it cannot cut at 17:30 either.
    spine = line("trend", "Revenue by day", trend)
    spine["note"] = _timed(scope)

    visuals = [
        # A ring, not bars: four shops dividing one month of trade between them is the share question this
        # whole page is about, and four slices read at a glance where four bars read as a ranking.
        donut("branches", "Revenue by branch", "branch", rows, note=_timed(scope, bill_note or quiet)),
        # THE POINT OF THE PAGE, and the reason it sits second rather than last: the bar above ranks totals, this
        # one ranks the same branches by what they take on a day they are open.
        bars("per_day", "Revenue per trading day", "branch", per_day_rows,
             note=_timed(scope, quiet or
                         "Each branch's revenue divided by the days that branch itself traded in these dates, "
                         "which is the Days column in the table below, not the length of the window. Ranked on "
                         "that figure, so it can disagree with the chart above, and over a long window it "
                         "does.")),
        {
            "id": "profit_per_day", "kind": "bars", "title": "Gross profit per trading day", "dimension": "branch",
            "note": _timed(scope, quiet or
                           "Profit is revenue less cost of sale, over the same Days. A branch can be second on "
                           "revenue and first here: Gulgasht's 31.1% margin against Garden Town's 13.0% in the "
                           "test month is the whole reason this chart is not the one above in a different "
                           "colour."),
            "rows": [{"key": r["key_"], "label": r["label"], "value": money(r["profit_per_day"]),
                      "qty": money(r["qty_per_day"])} for r in profit_rows],
        },
        spine,
    ]

    visuals.append(table(
        "ranked", "Every branch, best first", "branch",
        [{"key": "label", "label": "Branch"},
         {"key": "days", "label": "Days", "align": "right"},
         {"key": "value", "label": "Revenue", "align": "right"},
         {"key": "profit", "label": "Gross profit", "align": "right"},
         {"key": "margin", "label": "Margin %", "align": "right"},
         {"key": "share", "label": "% of all", "align": "right"},
         {"key": "per_day", "label": "Per trading day", "align": "right"},
         {"key": "invoices", "label": "Bills", "align": "right"},
         {"key": "basket", "label": "Average basket", "align": "right"},
         {"key": "per_bill", "label": "Items per bill", "align": "right"},
         {"key": "bills_per_day", "label": "Bills per trading day", "align": "right"}],
        [{
            "key": r["key_"], "label": f"{r['label']} ({r['code']})",
            "days": str(r["days"]) if r["days"] else "did not trade",
            "value": money(r["net"]), "profit": money(r["profit"]),
            "margin": pct(r["profit"], r["net"]),
            "share": pct(r["net"], sum((x["net"] for x in rows), D0)),
            "per_day": money(r["per_day"]),
            "invoices": "--" if r["invoices"] is None else str(r["invoices"]),
            "basket": "--" if r["basket"] is None else money(r["basket"]),
            "per_bill": "--" if r["per_bill"] is None else f"{r['per_bill']:.1f}",
            "bills_per_day": "--" if r["bills_per_day"] is None else f"{r['bills_per_day']:.0f}",
        } for r in rows],
        note=_timed(scope, quiet or bill_note or
                    "Per trading day is this branch's own figure over its own Days, so a branch that was shut "
                    "for a week is not punished for the days it was shut, and one that opened last month is "
                    "not ranked against the months before it existed.")))

    visuals.append(table(
        "trading", "How long each branch has been trading", "branch",
        [{"key": "label", "label": "Branch"},
         {"key": "first_ever", "label": "First day reported"},
         {"key": "last_ever", "label": "Last day reported"},
         {"key": "ever_days", "label": "Days in all", "align": "right"},
         {"key": "days", "label": "Days in these dates", "align": "right"},
         {"key": "staff_days", "label": "People on a counter", "align": "right"},
         {"key": "closes", "label": "Till closes", "align": "right"},
         {"key": "named", "label": "Named customers", "align": "right"},
         {"key": "returns", "label": "Returns", "align": "right"},
         {"key": "returns_count", "label": "Returns taken", "align": "right"},
         {"key": "stock_value", "label": "Stock on hand", "align": "right"},
         {"key": "stock_day", "label": "Stock as at"}],
        [{
            "key": r["key_"], "label": f"{r['label']} ({r['code']})",
            "first_ever": _said(r["first_ever"]), "last_ever": _said(r["last_ever"]),
            "ever_days": str(r["ever_days"]), "days": str(r["days"]),
            "staff_days": str(r["staff_days"]), "closes": str(r["closes"]),
            "named": str(r["named"]), "returns": money(r["returns_value"]),
            "returns_count": str(r["returns_count"]),
            "stock_value": money(r["stock_value"]), "stock_day": _said(r["stock_day"]),
        } for r in rows],
        note=_timed(scope, quiet or
                    "Days in all and the first day reported are the branch's whole history, not these dates, "
                    "because that is what tells a branch that was shut for two days apart from one that opened "
                    "five months later. People on a counter and till closes are day counts added up, so five "
                    "people over six days reads 30. Till closes here is what the branch itself counted on its "
                    "daily rows, which is 316 across the test month against 319 session rows on the People "
                    "page: Gulgasht opened a drawer on two days it reported no trading at all, and once "
                    "reported one close on a day with two sessions. Stock on hand is each branch's own latest "
                    "reported day, shown beside it, because it is a right-now figure and summing it over a "
                    "date range would multiply it by the number of days. The last day reported is the one to "
                    "watch for a branch going quiet: all four sit on 13 October 2026 today.")))

    # The money side, which the two tables above deliberately leave out so each answers one question. Discount
    # and credit are the two that cost something and do not show up in revenue at all: Garden Town gave away
    # Rs 628,383 of discount in the test month against Fort Colony's Rs 10,678, on five times the trade.
    visuals.append(table(
        "money", "What was taken, and how", "branch",
        [{"key": "label", "label": "Branch"},
         {"key": "gross", "label": "Gross", "align": "right"},
         {"key": "disc", "label": "Discount given", "align": "right"},
         {"key": "disc_pct", "label": "Discount % of gross", "align": "right"},
         {"key": "value", "label": "Net", "align": "right"},
         {"key": "cash", "label": "Cash collected", "align": "right"},
         {"key": "credit", "label": "Credit given", "align": "right"},
         {"key": "returns", "label": "Returns", "align": "right"},
         {"key": "returns_pct", "label": "Returns % of net", "align": "right"}],
        [{
            "key": r["key_"], "label": f"{r['label']} ({r['code']})",
            "gross": money(r["gross"]), "disc": money(r["disc"]),
            "disc_pct": pct(r["disc"], r["gross"]), "value": money(r["net_day"]),
            "cash": money(r["cash"]), "credit": money(r["credit"]),
            "returns": money(r["returns_value"]), "returns_pct": pct(r["returns_value"], r["net_day"]),
        } for r in rows],
        note=_timed(scope, quiet or
                    "Every figure here comes from the branch's own daily rows, so it is the whole branch and "
                    "does NOT follow an Item filter. Net is that whole-branch figure for the same reason, which "
                    "is why it can differ from the Revenue column above when a category is clicked. Cash "
                    "collected plus credit given does come to net exactly, on all four branches over the test "
                    "month, so the split between them can be relied on. Gross less discount does NOT: a branch "
                    "charges each bill rounded to the rupee while gross is the unrounded line value, and over "
                    "the test month that leaves Garden Town Rs 23,215 apart on 23,138 bills, about a rupee a "
                    "bill, against Rs 11 at Fort Colony and nothing at all at Gulgasht.")))
    return visuals


def _people(scope: Scope, rows: list[dict], dimensional: bool,
            cashiers: list[dict], closes: list[dict]) -> list[dict]:
    names = {r["key_"]: f"{r['label']} ({r['code']})" for r in rows}
    allowed = set(names)

    # A name belongs to a branch, so both folds key on the pair. Rows from a branch the narrowing excluded are
    # dropped here rather than in SQL, because `_selling` is the one place that decides which branches a page is
    # about and head office is excluded there.
    by_person = _fold_closes([r for r in closes if str(r["branch_id"]) in allowed],
                             lambda r: (str(r["branch_id"]), str(r["cashier_name"] or "--")))
    by_counter = _fold_closes([r for r in closes if str(r["branch_id"]) in allowed],
                              lambda r: (str(r["branch_id"]), str(r["counter_name"] or "--")))

    people: list[dict] = []
    for row in cashiers:
        key = str(row["branch_id"])
        if key not in allowed:
            continue
        net, invoices = ds._d(row["net"]), int(row["invoices"] or 0)
        days = int(row["days"] or 0)
        till = by_person.get((key, str(row["name"]))) or {}
        minutes = int(till.get("minutes") or 0)
        people.append({
            "key_": f"{key}:{row['name']}", "label": str(row["name"]), "branch": names.get(key, "--"),
            "net": net, "invoices": invoices, "days": days,
            "basket": net / invoices if invoices else D0,
            # Over the days this person rang a sale, not over the window. Covering four days well and covering
            # thirty are different jobs, and a total ranks the first as the worse of the two.
            "per_day": _per(net, days),
            "closes": int(till.get("closes") or 0), "minutes": minutes,
            "counted": ds._d(till.get("counted")), "variance": ds._d(row["variance"]),
            "counters": len(till.get("counters") or ()),
            "per_hour": Decimal(invoices) * 60 / minutes if minutes else D0,
        })
    people.sort(key=lambda r: r["net"], reverse=True)

    # A person who closed a drawer and rang nothing: one in the test window (farhanmt at Model Town, a single
    # close counting Rs 1). Worth its own line because the cashier rollup cannot show it at all -- a person with
    # no sale has no row there -- and "who was at a till" is half of what this page is for.
    rang = {(str(r["branch_id"]), str(r["name"])) for r in cashiers}
    silent = [pair for pair in by_person if pair not in rang]

    total_net = sum((r["net"] for r in people), D0)
    visuals: list[dict] = []

    if dimensional:
        # Not one of these folds can follow an Item, so every visual on this page is returned empty with the
        # reason on it. Drawing them from the whole branch under an Item filter would be the silent-filter bug.
        empty = _timed(scope, PEOPLE_NOT_DIMENSIONAL)
        return [
            bars("people", "Revenue by cashier", None, [], note=empty),
            table("cashiers", "Every cashier, best first", None, [{"key": "label", "label": "Cashier"}], [],
                  note=empty),
            table("counters", "Every till, by what it counted", None,
                  [{"key": "label", "label": "Till"}], [], note=empty),
            table("sessions", "Till sessions, most recent first", None,
                  [{"key": "label", "label": "Session"}], [], note=empty),
        ]

    # `dimension` is None on every chart here, and it has to be. The app sends a visual's dimension straight back
    # as a query parameter, and the only ones the route accepts are branch, department, category, brand and sku
    # (see `dashboards_service.DIMENSIONS`). A chart declaring "cashier" would send `?cashier=ASAD`, which the
    # route does not read at all, so the click would appear to do nothing. Drilling into a person is the
    # Executive screen's job, where `executive_service.cashier_days` already answers it.
    #
    # Built by hand rather than through `_shape.bars`, which always derives a profit from net less cogs. There is
    # no cost of sale on a cashier row -- `branch_cashier_stats` keeps bills, takings and a dead variance and
    # nothing else -- so passing zero cogs would have every bar reporting its revenue a second time as profit,
    # and razzaqgtfbr's Rs 9,687,976 of takings would read as Rs 9,687,976 of margin. No profit field at all is
    # the honest shape, and the same one abc.py's hand-built bars use.
    visuals.append({
        "id": "people", "kind": "bars", "title": "Revenue by cashier", "dimension": None,
        "note": _timed(scope, "One row per person per branch, and the bills they rang beside it. Clicking a bar "
                              "does nothing on purpose: this page narrows by branch, not by person. There is no "
                              "cost of sale per person, so there is no margin here."),
        "rows": [{"key": r["key_"], "label": f"{r['label']} -- {r['branch']}",
                  "value": money(r["net"]), "qty": str(r["invoices"])} for r in people],
    })

    visuals.append(table(
        "cashiers", "Every cashier, best first", None,
        [{"key": "label", "label": "Cashier"},
         {"key": "branch", "label": "Branch"},
         {"key": "days", "label": "Days worked", "align": "right"},
         {"key": "value", "label": "Revenue", "align": "right"},
         {"key": "share", "label": "% of all", "align": "right"},
         {"key": "per_day", "label": "Per day worked", "align": "right"},
         {"key": "invoices", "label": "Bills", "align": "right"},
         {"key": "basket", "label": "Average basket", "align": "right"},
         {"key": "closes", "label": "Till closes", "align": "right"},
         {"key": "counters", "label": "Tills used", "align": "right"},
         {"key": "hours", "label": "Hours at a till", "align": "right"},
         {"key": "per_hour", "label": "Bills per hour", "align": "right"},
         {"key": "variance", "label": "Till variance", "align": "right"}],
        [{
            "key": r["key_"], "label": r["label"], "branch": r["branch"],
            "days": str(r["days"]), "value": money(r["net"]),
            "share": pct(r["net"], total_net), "per_day": money(r["per_day"]),
            "invoices": str(r["invoices"]), "basket": money(r["basket"]),
            "closes": str(r["closes"]), "counters": str(r["counters"]),
            "hours": f"{r['minutes'] / 60:.1f}",
            # One decimal, not none. Gulgasht's one cashier rings 121 bills across 333 hours at the till, which
            # is 0.4 an hour and printed as "0" to no decimals -- a true figure that reads as a missing one.
            "per_hour": f"{r['per_hour']:.1f}", "variance": money(r["variance"]),
        } for r in people],
        note=_timed(scope, "Per day worked is that person's own takings over the days they rang a sale, not "
                           "over the window, so somebody who covered four days is not ranked against somebody "
                           "who covered thirty. Hours at a till is measured from each session's own open and "
                           f"close, and bills per hour divides into it. {VARIANCE_IS_DEAD}")
             if people else PEOPLE_NOT_DIMENSIONAL))

    counters = sorted(
        ({"branch": names.get(branch, "--"), "name": counter, **group}
         for (branch, counter), group in by_counter.items()),
        key=lambda r: r["counted"], reverse=True)
    visuals.append(table(
        "counters", "Every till, by what it counted", None,
        [{"key": "label", "label": "Till"},
         {"key": "branch", "label": "Branch"},
         {"key": "closes", "label": "Closes", "align": "right"},
         {"key": "people", "label": "People on it", "align": "right"},
         {"key": "hours", "label": "Hours open", "align": "right"},
         {"key": "float", "label": "Opening float", "align": "right"},
         {"key": "counted", "label": "Cash counted", "align": "right"},
         {"key": "variance", "label": "Variance", "align": "right"},
         {"key": "unclosed", "label": "Left open", "align": "right"}],
        [{
            "key": f"{r['branch']}:{r['name']}", "label": r["name"], "branch": r["branch"],
            "closes": str(r["closes"]), "people": str(len(r["people"])),
            "hours": f"{r['minutes'] / 60:.1f}", "float": money(r["float"]),
            "counted": money(r["counted"]), "variance": money(r["variance"]),
            "unclosed": str(r["unclosed"]),
        } for r in counters],
        note=_timed(scope, "Counted cash is what was in the drawer at close, and it is NOT the day's takings: "
                           "cash is lifted off a till mid-shift, so over the test month Garden Town counted "
                           "Rs 16,325,296 against Rs 30,518,886 of cash collected on its daily rows. A till "
                           "with many closes and almost nothing counted is a drawer being opened and shut "
                           "rather than traded on, and there are real ones: Garden Town's Till 11 was closed 21 "
                           f"times for Rs 21 in all. {VARIANCE_IS_DEAD}")
             if counters else PEOPLE_NOT_DIMENSIONAL))

    recent = [r for r in closes if str(r["branch_id"]) in allowed][:150]
    visuals.append(table(
        "sessions", "Till sessions, most recent first", None,
        [{"key": "day", "label": "Trading day"},
         {"key": "branch", "label": "Branch"},
         {"key": "counter", "label": "Till"},
         {"key": "label", "label": "Cashier"},
         {"key": "opened", "label": "Opened"},
         {"key": "closed", "label": "Closed"},
         {"key": "hours", "label": "Hours", "align": "right"},
         {"key": "float", "label": "Opening float", "align": "right"},
         {"key": "counted", "label": "Cash counted", "align": "right"},
         {"key": "variance", "label": "Variance", "align": "right"}],
        [{
            "key": str(r["session_number"] or ""), "day": _said(_day(r["day"])),
            "branch": names.get(str(r["branch_id"]), "--"),
            "counter": str(r["counter_name"] or "--"), "label": str(r["cashier_name"] or "--"),
            "opened": f"{_at(r['opened_at']):%H:%M}" if _at(r["opened_at"]) else "--",
            "closed": f"{_at(r['closed_at']):%H:%M}" if _at(r["closed_at"]) else "still open",
            "hours": f"{_minutes(r) / 60:.1f}", "float": money(r["opening_float"]),
            "counted": money(r["counted_cash"]), "variance": money(r["variance"]),
        } for r in recent],
        note=_timed(scope, f"The {len(recent)} most recent sessions. A closing time earlier on the clock than "
                           f"the opening one is a session that ran past midnight, which is normal in these "
                           f"shops -- 116 of the 319 sessions in the test month do it -- and the hours are "
                           f"measured across the turn rather than clamped to the day. A session with no "
                           f"closing time at all reads as still open and counts no hours.")
             if recent else PEOPLE_NOT_DIMENSIONAL))

    if silent:
        visuals.append(table(
            "silent", "On a till, but no sale rung", None,
            [{"key": "label", "label": "Cashier"},
             {"key": "branch", "label": "Branch"},
             {"key": "closes", "label": "Closes", "align": "right"},
             {"key": "hours", "label": "Hours at a till", "align": "right"},
             {"key": "counted", "label": "Cash counted", "align": "right"}],
            [{
                "key": f"{branch}:{name}", "label": name, "branch": names.get(branch, "--"),
                "closes": str(by_person[(branch, name)]["closes"]),
                "hours": f"{by_person[(branch, name)]['minutes'] / 60:.1f}",
                "counted": money(by_person[(branch, name)]["counted"]),
            } for branch, name in sorted(silent)],
            note="These people opened and closed a drawer in these dates and have no row in the sales rollup at "
                 "all, so no table above can show them. Usually a till opened by mistake or for a test; it is "
                 "also the only shape a person who stood at a counter all day and sold nothing can take."))
    return visuals


async def build(page_id: str, scope: Scope) -> dict:
    rows, dimensional = await _ranked(scope)
    total_net = sum((r["net"] for r in rows), D0)
    total_profit = sum((r["profit"] for r in rows), D0)
    total_qty = sum((r["qty"] for r in rows), D0)
    # The days a branch traded, added across branches, which is the honest divisor for "what does a branch take
    # on a day it is open" over a group of them. Not the window's length and not the biggest branch's day count:
    # in the 30 day test month it is 118, because three branches traded all 30 and Gulgasht traded 28. Dividing
    # by 120 instead would understate the figure by 1.7% and would do so silently.
    branch_days = sum(r["days"] for r in rows)
    trading = sum(1 for r in rows if r["days"])
    total_invoices = None if dimensional else sum(int(r["invoices"] or 0) for r in rows)

    cards = [
        card("Revenue", money(total_net),
             sub=f"{trading} of {len(rows)} branch(es) traded in these dates" if rows else "no branch traded"),
        card("Gross Profit", money(total_profit), sub=f"{pct(total_profit, total_net)}% margin"),
        card("Per Branch Trading Day", money(_per(total_net, branch_days)),
             sub=f"over {branch_days} branch-day(s), each branch counted only for the days it traded"),
        card("Items Sold", money(total_qty), kind="number"),
    ]
    if total_invoices is None:
        cards.append(card("Bills", "--", kind="number", sub="bills cannot follow an Item filter"))
    else:
        cards += [
            card("Bills", str(total_invoices), kind="number"),
            card("Average Basket", money(total_net / total_invoices if total_invoices else D0)),
        ]

    if page_id == "overview":
        # Added across branches, which is the ONE direction this column may be added in. Each branch contributes
        # exactly one figure, from its own latest reported day, so the sum is four numbers and not four times
        # thirty. The sub names that day so nobody reads it as a figure for the window.
        stock = sum((r["stock_value"] for r in rows), D0)
        as_at = max((r["stock_day"] for r in rows if r["stock_day"]), default=None)
        cards.append(card("Stock on Hand", money(stock),
                          sub=f"as each branch last reported it, to {_said(as_at)}. Neither these dates nor an "
                              f"Item filter reaches it; Stock Health has the picture that does"))
        return {"cards": cards, "covers": _covers(scope, trading, branch_days),
                "visuals": _overview(scope, rows, dimensional, await ds.trend(scope))}

    if page_id == "people":
        cashiers = [] if dimensional else await _cashiers(scope)
        closes = [] if dimensional else await _closes(scope)
        allowed = {r["key_"] for r in rows}
        mine = [r for r in closes if str(r["branch_id"]) in allowed]
        minutes = sum(_minutes(r) for r in mine)
        cards = cards[:3] + [
            card("People Who Rang a Sale",
                 "--" if dimensional else str(len({(str(r['branch_id']), str(r['name'])) for r in cashiers})),
                 kind="number",
                 sub="one person at two branches counts once per branch" if not dimensional else None),
            # Counted from the session rows, not from the branches' own `tills_closed`. The two differ by three
            # over the test month (319 here against 316 there) and the Branches page says why; this is the one
            # the tables below are built from, so it is the one that has to be on the card above them.
            card("Tills Closed", "--" if dimensional else str(len(mine)), kind="number",
                 sub=None if dimensional else
                     f"session by session, on {len({str(r['counter_name'] or '--') for r in mine})} counter(s)"),
            card("Hours Covered", "--" if dimensional else f"{minutes / 60:.0f}", kind="number",
                 sub="measured from each session's own open and close"),
            card("Till Variance", "--" if dimensional else money(sum((r["variance"] for r in rows), D0)),
                 sub="reads zero on every row we hold; see the note below"),
        ]
        covers = _covers(scope, trading, branch_days, [
            {"label": "people", "value": len({(str(r["branch_id"]), str(r["name"])) for r in cashiers})},
            {"label": "tills", "value": len({str(r["counter_name"] or "--") for r in mine})},
            {"label": "till sessions", "value": len(mine)},
        ])
        return {"cards": cards, "covers": covers,
                "visuals": _people(scope, rows, dimensional, cashiers, closes)}

    raise KeyError(page_id)
