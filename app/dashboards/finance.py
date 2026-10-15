"""Finance Overview: the profit and loss as arithmetic anybody can follow.

Revenue, less what the goods cost, is gross profit. Plus whatever else came in, less what it took to run the place,
is net profit. Two subtractions and an addition, and that is the whole of a profit and loss account -- which is why
this page leads with cards rather than a chart. The arithmetic IS the finding here, and five bars of the same five
numbers would hide the subtractions that connect them.

WHERE THE FIGURES COME FROM
---------------------------
The books themselves: `acc_voucher_lines` for the money, `acc_vouchers` for the day, the book and whether the entry
is posted, and `acc_accounts` through its group for what part of the statement each line belongs to. There is no
rollup to read, because a voucher line is the grain the ledger is kept at.

Everything here is `v.status = 'posted'`, applied once in `_where` so that no fold can forget it. A draft voucher is
somebody's intention, not the books.

`kind` ON AN ACCOUNT IS NOT WHAT SORTS THESE
--------------------------------------------
It reads 'supplier' (2,662), 'customer' (2,350), 'general' (1,983), 'cash' (334), 'interoffice' (6), 'wallet' (3)
and 'bank' (1) -- who an account belongs to, not what it is on the statement. Sorting on it would have put the sales
account, the purchase account and the electricity bill in one heap of 1,983 'general' accounts. What decides a
section is the account's GROUP, through its category and its type: `acc_types.statement` is 'income' or 'balance',
and `acc_categories.code` is 41 SALES REVENUE, 42 OTHER INCOME, 51 COST OF SALES, 52 OPERATING EXPENSES,
53 FINANCIAL EXPENSES. `SECTION_OF_CATEGORY` is imported from the Income Statement report rather than copied, so the
two can never drift into filing the same account differently.

CHECKED AGAINST THE REPORT, NOT TRUSTED
---------------------------------------
Over 13 September to 12 October 2026, every figure this builds and every figure
`accounts_reports_service.income_statement` builds agree to the paisa: revenue 51,988,296.00, cost of sale
45,809,290.92, gross profit 6,179,005.08, other income 122,065.00, expense 9,278,633.99, net profit -2,977,563.91.
That agreement is the whole point of the import above. A second way of adding up one ledger that quietly answers
something else is worse than having no second way.

THE LOSS IS THEIRS, NOT A BUG
-----------------------------
Net profit over that window is negative, Rs -2,977,563.91, and their own software says the same: for 12 September to
11 October `ops/checks/pl_reconcile.py` read their live server and reported their net profit as Rs -2,602,510
against ours. The reason is on this page's own account group chart: cost of sales in that window is PURCHASES
46,258,684.92 and PURCHASE RETURNS -449,394.00, and nothing else at all. There is no inventory increase or decrease
in it, so nothing adjusts what was BOUGHT down to what actually left the shelf. Over September that was
23,059,358.01 of cost against 29,175,407.00 of revenue; over the first twelve days of October, 22,749,932.91
against 22,812,889.00. Buying is lumpy and selling is not, so a short window's gross margin here moves with the
buying, and only a window long enough to even that out reads as a trading margin.

A BOOK'S OWN GROSS PROFIT IS NOT THAT BRANCH'S
----------------------------------------------
Picking a branch narrows to that branch's book, which is right for its revenue, right for its expenses and
misleading for its cost of sale, because the buying is not posted where the selling is. In the same window:

    cost of sale    HO 33,721,589.96   GT 12,087,700.96   FC, GG and MT nothing at all
    revenue         HO nothing at all  GT 30,260,512.00   FC 5,997,289.00  GG 4,530,156.00  MT 11,200,339.00

So Fort Colony's book on its own is revenue with no cost against it and a gross profit of 100%, and Garden Town's is
a 60% margin against the company's 11.89%. The cards say so in plain words the moment a branch is picked, and the
By book table shows the whole split before anybody clicks, because the shape of it is worth knowing: head office is
where this company buys and the branches are where it sells.

WHAT IT WILL NOT PRETEND TO DO
------------------------------
A voucher carries an account, a book and a day. It carries no department, category, brand or Item, and every
dashboard shares one set of filters in the address, so one of those can arrive here from a click on another page.
When it does, every panel comes back empty with the reason written on it and every card reads `--`. Answering a
question nobody asked, with a figure that looks like an answer, is the one outcome worth any amount of code to
avoid.

A time cannot be honoured either, and that one is a trap rather than a gap. `acc_vouchers.at` is filled for most
vouchers and not for all, so a timed window silently drops the rest -- which is tolerable for a report that lists
entries and counts what it dropped, and not tolerable for a figure that is supposed to balance. `core/when.py` holds
that line for every balance report in the application and a profit and loss is a balance report, so a time here
gets a card saying it was not used, and whole days are read.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import dialect
from app.dashboards._shape import card, line, money, pct, table
from app.services import dashboards_service as ds
from app.services.accounts_reports_service import SECTION_OF_CATEGORY, SECTION_TITLES
from app.services.dashboards_service import Scope

META = {
    "label": "Finance Overview",
    "blurb": "What came in, what it cost, and what is left, in the order the books add it up.",
    # An existing grant, never a new one: this draws the Income Statement, so whoever may read that may see it.
    "resource": "accounts.income-statement",
    "pages": [
        {"id": "overview", "label": "Overview"},
        {"id": "accounts", "label": "Accounts"},
    ],
}

# Income is a credit and expense a debit, so each section is netted the way the statement nets it. A sales return is
# a debit on a revenue account and has to come OFF revenue: Fort Colony's SALES RETURN ACCOUNT ran to -64,408.00 in
# the window below, and adding it on would have overstated the shop by twice that.
CREDIT_SECTIONS = ("sales", "other")

# What a person reads on the Accounts page. The report's own words, so one account is not "Cost of sales" on one
# screen and "Cost of goods" on the next.
SECTION_LABEL = dict(SECTION_TITLES)

# The dimensions the books cannot follow at all, and what a person calls each one. Bare, with no article, because
# both sentences built from these need it that way: "it carries no brand", "the books carry no department or Item".
BLIND = {"department": "department", "category": "category", "brand": "brand", "sku": "Item"}

# Day by day up to this much window, month by month beyond it. Measured on the real books, interleaved, each shape
# run two or three times: grouping the window's lines by day costs roughly 0.11s per day of window -- 3.6s over
# 30 days, 10.9s over 92, 35s over a year -- while grouping by month is nearly flat at 2.0s to 5.1s. 45 days keeps
# the day-grained line, which is what anybody wants for the default month, inside about five seconds.
BY_DAY_DAYS = 45

# How many rows the two listing visuals show. Everything is folded from one query that fetches every account that
# moved, so these are slices of rows already in hand and not a limit on any query.
TOP_ACCOUNTS = 12
TOP_GROUPS = 14
TABLE_ROWS = 300

# The ledger, with just enough of the chart joined on to know what part of the statement each line is. `t.statement`
# is the one condition keeping a balance sheet account out of a profit figure.
#
# NO JOIN HINT HERE, and `dialect.hash_join` is the reason it looks as though there should be one. Its measurements
# are for a sum over every line in a window; this reads only the lines on profit and loss accounts, which was 50,683
# of them in the month below. Asking for the hash join makes SQL Server build over the whole table instead of
# seeking: 1.37s plain against 3.11s hinted over 30 days, and 2.14s against 11.31s over a year. Five times slower,
# not twice as fast, so the hint stays out and this comment stays in.
_LEDGER = """
          FROM acc_voucher_lines l
          JOIN acc_vouchers v ON v.id = l.voucher_id
          JOIN acc_accounts a ON a.id = l.account_id
          JOIN acc_groups g ON g.id = a.group_id
          JOIN acc_categories c ON c.code = g.category_id
          JOIN acc_types t ON t.code = c.type_id
"""


def _section(category: str, type_code: str) -> str:
    """Which part of the statement an account's category is.

    The fallback is the report's, kept identical on purpose: anything of type 4 that is not sales revenue counts as
    other income, and anything of type 5 that is not cost of sales counts as an operating expense. It is reached
    only by a category nobody has mapped yet, and the point of having it is that such a category lands somewhere
    visible on the statement rather than vanishing out of the profit altogether.
    """
    return SECTION_OF_CATEGORY.get(category) or ("other" if type_code == "4" else "operating")


async def _book(scope: Scope) -> tuple[str | None, str | None]:
    """The book a picked branch keeps, or why it cannot be read.

    This is the one dimension on the page the books CAN follow, and the only one. `acc_vouchers` has no branch_id
    column; it has `book`, and a book is a shop -- 'GT', 'GG', 'MT', 'FC', and head office's own 'HO'. The branch
    rows carry the same codes, so a picked branch id is turned into its code here and compared as a code.
    """
    picked = scope.picked.get("branch")
    if not picked:
        return None, None
    from app.models import Branch

    branch = await Branch.filter(id=picked).first()
    if not branch:
        return None, "That branch is not one of ours, so there is no book to read. Clear the branch filter."
    return branch.code, None


def _where(scope: Scope, book: str | None, extra: str = "") -> tuple[str, list]:
    """The conditions every fold here shares, and the values in the order the statement wants them.

    `only=()` because `acc_vouchers` carries a book and a date and nothing else -- no branch_id, no department, no
    category, no brand, no sku. Left to its default, `scope.where` would ask SQL Server for `v.category` on a table
    of vouchers, which is the exact failure its own docstring records from the first time it ran.
    """
    where, args = scope.where("v", "date", only=())
    clauses = [where, "v.status = 'posted'", "t.statement = 'income'"]
    if book:
        # `book` is varchar(20), and the cast is why this is `dialect.mark` rather than a bare `?`: pyodbc sends a
        # Python string as NVARCHAR, which outranks varchar, so SQL Server converts the COLUMN on every row instead
        # of converting the one parameter. `ops/checks/sql_key_casts.py` fails the build on a comparison that
        # forgets it.
        clauses.append(f"v.book = {dialect.mark('varchar(20)')}")
        args = [*args, book]
    if extra:
        clauses.append(extra)
    return " AND ".join(clauses), args


# ── the folds ───────────────────────────────────────────────────────────────────────────────────
async def accounts(scope: Scope, book: str | None) -> list[dict]:
    """Every profit and loss account that moved in the window, with what moved through it.

    EVERY one, not a top slice, because the cards at the top of the page are added up from these same rows. A
    headline summed from the biggest fifteen accounts would be a figure that disagrees with the report, with the
    table underneath it and with itself the moment a sixteenth account mattered.

    It stays small without being limited: 93 accounts moved in the 30 days to 12 October 2026 and 196 in the year to
    it, out of 7,339 in the chart. Grouping by the named columns rather than by `a.id` with `MIN` over the rest was
    measured both ways and the difference is noise at a month (1.98s against 2.37s) and two seconds at a year (8.6s
    against 6.0s); the clearer statement was kept, and the year view's cost is written down under `build`.
    """
    where, args = _where(scope, book)
    return await ds._rows(f"""
        SELECT a.id AS id, a.book AS book, a.code AS code, a.name AS name, g.name AS grp,
               c.code AS cat, t.code AS typ,
               SUM(l.debit) AS dr, SUM(l.credit) AS cr, COUNT(*) AS entries
        {_LEDGER} WHERE {where}
         GROUP BY a.id, a.book, a.code, a.name, g.name, c.code, t.code
    """, args)


async def buckets(scope: Scope, book: str | None, by_day: bool) -> list[dict]:
    """The window cut into days or months, each piece split by section, for the trend.

    The book is deliberately NOT in this grouping. Adding it answers a question no visual on the page asks, and it
    costs the whole year view: grouped by book, month and category a year took 34.8s to 35.8s, against 4.6s to 5.1s
    for the same rows grouped by month and category. The per-book figures come out of `accounts` instead, where they
    are free.
    """
    bucket = "v.date" if by_day else dialect.year_month("v.date")
    where, args = _where(scope, book)
    return await ds._rows(f"""
        SELECT {bucket} AS bucket, c.code AS cat, t.code AS typ,
               SUM(l.debit) AS dr, SUM(l.credit) AS cr
        {_LEDGER} WHERE {where}
         GROUP BY {bucket}, c.code, t.code ORDER BY {bucket}
    """, args)


# ── the arithmetic ──────────────────────────────────────────────────────────────────────────────
def _sections(rows: list[dict]) -> dict[str, Decimal]:
    """Debits and credits added up into the five sections a profit and loss account has."""
    out: dict[str, Decimal] = {key: ds._d(0) for key in SECTION_TITLES}
    for row in rows:
        section = _section(str(row["cat"] or ""), str(row["typ"] or ""))
        dr, cr = ds._d(row["dr"]), ds._d(row["cr"])
        out[section] += (cr - dr) if section in CREDIT_SECTIONS else (dr - cr)
    return out


def _chain(rows: list[dict]) -> dict[str, Decimal]:
    """The five figures and the two subtractions, in the order a reader follows them.

    Other income is kept as its own step rather than folded into revenue. Folding it in would read as one unbroken
    subtraction, and it would also make this page's gross profit disagree with the Income Statement's, which takes
    gross profit off sales revenue alone. In the window in the notes above it is 122,065.00 of shelf rent and
    excess cash -- small, real, and not a sale.
    """
    parts = _sections(rows)
    revenue, cost = parts["sales"], parts["cost"]
    expense = parts["operating"] + parts["financial"]
    gross = revenue - cost
    return {**parts, "revenue": revenue, "cost": cost, "gross": gross, "other": parts["other"],
            "expense": expense, "net": gross + parts["other"] - expense}


def _share(part: Decimal, revenue: Decimal) -> str:
    """One figure as a share of revenue, or words where there is no revenue to be a share of.

    `pct` answers "0.0" when the whole is zero, which is the right answer for a ratio and the wrong thing to print:
    head office's book alone holds 33,721,589.96 of cost and no sales at all, and every card under it read
    "0.0% of revenue" as though the cost were nothing.
    """
    return f"{pct(part, revenue)}% of revenue" if revenue else "no revenue in this window to compare with"


def _cards(chain: dict[str, Decimal], days: int, with_entries: int | None, book: str | None) -> list[dict]:
    """The P&L as five cards and a subtraction you can do in your head, each one a share of revenue.

    Every percentage is of revenue, including net profit's, because that is the one figure every other figure on a
    profit and loss account is judged against.
    """
    revenue = chain["revenue"]
    cost_sub = _share(chain["cost"], revenue)
    gross_sub = _share(chain["gross"], revenue)
    net_sub = _share(chain["net"], revenue)
    if book:
        # The caveat from the docstring, said where somebody will actually read it. Revenue and expense are the two
        # figures a single book does not distort: they really are its own.
        cost_sub = "this book's own buying only"
        if not chain["cost"]:
            gross_sub = "nothing taken off -- the buying is in another book"
            if revenue:
                # Fort Colony's book comes out at a 93.9% net margin for exactly this reason, and a reader who
                # takes that at face value has been misled by arithmetic that is correct.
                net_sub = f"{pct(chain['net'], revenue)}% of revenue, with no cost of sale taken off"
        elif not revenue:
            gross_sub = "this book keeps the buying, not the selling"
        else:
            gross_sub = f"{pct(chain['gross'], revenue)}% here, but not a trading margin"
    span = f"over {days} day(s)"
    if with_entries is not None and with_entries != days:
        # Only when it is news. A window with entries on every day of it has nothing to add here, and a window
        # whose books stop halfway has everything to add.
        span = f"{span}, {with_entries} with entries"
    return [
        card("Revenue", money(revenue), sub=span),
        card("Cost of Sale", money(chain["cost"]), sub=cost_sub),
        card("Gross Profit", money(chain["gross"]), sub=gross_sub),
        card("Other Income", money(chain["other"]), sub=_share(chain["other"], revenue)),
        card("Expense", money(chain["expense"]), sub=_share(chain["expense"], revenue)),
        card("Net Profit", money(chain["net"]), sub=net_sub),
    ]


def _margin(figures: dict[str, Decimal]) -> str:
    """Gross profit as a share of revenue, or `--` where the division would mislead rather than inform.

    A book with revenue and no cost of sale at all divides out at exactly 100%, and three of the five do: the
    buying is posted in head office's book and the selling in theirs. Head office is the same fault the other way
    up, 33,721,589.96 of cost against no revenue. A figure of 100.0% in a Margin column is read as a margin by
    everybody who sees it, so where there is nothing to divide by, or nothing taken off, this says so instead.
    """
    if not figures["revenue"] or not figures["cost"]:
        return "--"
    return pct(figures["gross"], figures["revenue"])


def _label(row: dict, book: str | None) -> str:
    """An account as a person reads it, with its book when more than one is in view.

    All five books keep an account called SALES ACCOUNT and all five keep a PURCHASE ACCOUNT, so a chart of the
    biggest accounts without the book on it is several identical bars: four of those sales accounts moved in the
    window below and two of the purchase accounts. `accounts_reports_service._label` writes the same form, for the
    same reason, and dropping the book when one is picked keeps it off every row of a page that is already one
    book's.
    """
    name = str(row["name"] or row["code"] or "--")
    return name if book else f"{row['book']} · {name}"


def _amount(row: dict) -> Decimal:
    """One account's movement, in its own natural direction, so revenue and expense are both positive."""
    section = _section(str(row["cat"] or ""), str(row["typ"] or ""))
    dr, cr = ds._d(row["dr"]), ds._d(row["cr"])
    return (cr - dr) if section in CREDIT_SECTIONS else (dr - cr)


def _bars(id_: str, title: str, rows: list[dict]) -> dict:
    """A bar chart of accounts or groups.

    Written out rather than taken from `_shape.bars`, which folds `net` less `cogs` into a profit and a `qty`
    beside it. Neither exists on a voucher line, and a payload carrying `profit: 0.00` on an electricity bill is a
    figure somebody will eventually read. The shape is otherwise exactly the shape that helper returns, and the
    same one `sales.py` writes out for its hourly chart.

    `dimension` is None on all of these: an account is not something the app can narrow by. What a click sends back
    has to be one of branch, department, category, brand or Item -- the five in `dashboards_service.DIMENSIONS` --
    and the By book table below is the one visual here whose rows really are one of those.
    """
    return {
        "id": id_, "kind": "bars", "title": title, "dimension": None, "note": None,
        "rows": [{"key": str(r["key_"]), "label": str(r["label"]), "value": money(r["value"])} for r in rows],
    }


def _empty(id_: str, kind: str, title: str, note: str) -> dict:
    """A panel that says why it has nothing.

    The app draws a visual's `note` ONLY when the visual has no rows (`Bars`, `Line` and `Table` each return an
    Empty carrying it). So this is how a reason reaches a reader at all: a note on a chart that has bars in it is
    never shown to anybody.
    """
    return {"id": id_, "kind": kind, "title": title, "dimension": None, "note": note, "rows": []}


def _nothing(page_id: str, why: str, short: str) -> dict:
    """Every panel empty with the reason on it, and not one figure that ignores what was clicked.

    The cards have nowhere to put a sentence, so they read `--`, which is what the app prints for a value that is
    not a number, and the first of them carries the short form. Six dashes and one reason beats six figures that
    answer a question the reader did not ask.
    """
    cards = [card(label, "--", sub=short if first else None) for first, label in (
        (True, "Revenue"), (False, "Cost of Sale"), (False, "Gross Profit"),
        (False, "Other Income"), (False, "Expense"), (False, "Net Profit"))]
    if page_id == "accounts":
        visuals = [_empty("groups", "bars", "What it cost, by account group", why),
                   _empty("accounts", "table", "Accounts that moved", why)]
    else:
        visuals = [_empty("revenue_trend", "line", "Revenue", why),
                   _empty("cost_trend", "line", "Cost of sale and expense", why),
                   _empty("books", "table", "By book", why),
                   _empty("top_revenue", "bars", "Biggest revenue accounts", why),
                   _empty("top_expense", "bars", "Biggest cost and expense accounts", why)]
    return {"cards": cards, "visuals": visuals,
            # Its own, so the strip above does not go and read 1.87 million product-days to say nothing.
            "covers": {"branches": 0, "categories": 0, "brands": 0, "items": 0, "days": 0}}


async def build(page_id: str, scope: Scope) -> dict:
    """One page of this dashboard: two queries, and every figure on it folded from those two.

    WHAT IT COSTS, MEASURED
    -----------------------
    The default month is 5.74s cold -- 2.0s for the accounts and 3.7s for the day-grained trend -- and a second look
    inside 45 seconds is 0.00s, because both folds go through the query cache. A year is 9.97s, which is inside the
    15s the app waits and not comfortably, and a window much wider than a year will not be.

    The road out of that is already built, for this exact problem, in `accounts_reports_service`: whole calendar
    months come out of `vw_ledger_month` through `sp_ledger_sums` and only the ragged piece at each end is read from
    the lines, which took a year of all five books from 7.84s to 0.32s. It is not taken here because it is no use
    to the window this page opens on -- 13 September to 12 October contains no whole calendar month at all, so the
    procedure would read the very lines this reads and charge its own overhead on top. Worth taking the day
    somebody asks for a multi-year view, and not before.
    """
    if page_id not in ("overview", "accounts"):
        raise KeyError(page_id)

    book, why_not = await _book(scope)
    blind = [BLIND[name] for name in BLIND if name in scope.picked]
    if blind:
        joined = blind[0] if len(blind) == 1 else ", ".join(blind[:-1]) + f" or {blind[-1]}"
        why_not = (f"A voucher carries an account, a book and a day. It carries no {joined}, so the books cannot "
                   f"be read that way. Clear that filter to see this.")
        short = f"the books carry no {joined}"
    else:
        short = "that branch is not one of ours"
    if why_not:
        return _nothing(page_id, why_not, short)

    days = (scope.end - scope.start).days + 1
    by_day = days <= BY_DAY_DAYS
    rows = await accounts(scope, book)
    periods = await buckets(scope, book, by_day)

    chain = _chain(rows)
    trend: dict[str, dict[str, Decimal]] = {}
    for row in periods:
        # char(7) from `year_month` on SQL Server, a date object when grouped by day. Stripped because a trailing
        # space in a key would not raise anywhere -- the buckets would simply never match each other.
        slot = trend.setdefault(str(row["bucket"])[:10].strip(), {})
        section = _section(str(row["cat"] or ""), str(row["typ"] or ""))
        dr, cr = ds._d(row["dr"]), ds._d(row["cr"])
        slot[section] = slot.get(section, ds._d(0)) + ((cr - dr) if section in CREDIT_SECTIONS else (dr - cr))
    # Sorted rather than left in the order the rows arrived: both keys are ISO text, so this is chronological, and
    # a trend line whose order depends on what the engine felt like returning is a trend line nobody can trust.
    order = sorted(trend)

    cards = _cards(chain, days, len(order) if by_day else None, book)
    if scope.timed:
        # Said on a card because there is nowhere else a reader would see it: the controller reports this window's
        # basis as "bills", which is true of the sales dashboard and not of this one, and a note on a panel is only
        # drawn when the panel is empty. `core/when.py` is the reasoning -- 'at' is filled for most vouchers and
        # not all, so a timed window would drop the rest out of a figure that is supposed to balance.
        cards.append(card("Time of day", "not used", kind="number",
                          sub="a voucher is dated, not timed, so these are whole days"))
    grain = "day" if by_day else "month"
    visuals: list[dict] = []

    if page_id == "overview":
        # NOTHING THAT CAN TURN NEGATIVE IS PLOTTED. The app's line draws a point at `h - value / biggest * h`, so
        # a negative value lands below the frame and is clipped away silently. Over this window net profit is
        # Rs -2,977,563.91, which would have drawn as a flat line along the floor and read as a month of nothing.
        # Revenue, and what it cost, are the two series that stay positive; net profit is a card and a column.
        visuals.append(line("revenue_trend", f"Revenue, by {grain}",
                            [{"label": key, "net": trend[key].get("sales", ds._d(0))} for key in order]))
        visuals.append(line("cost_trend", f"Cost of sale and expense, by {grain}",
                            [{"label": key, "net": trend[key].get("cost", ds._d(0))
                              + trend[key].get("operating", ds._d(0))
                              + trend[key].get("financial", ds._d(0))} for key in order]))

        from app.models import Branch

        # Five rows, read so that a book's row can be clicked and carry the branch id the filter wants rather than
        # the code the books are kept under. The one visual here that is a dimension the app knows.
        named = {b.code: b for b in await Branch.all()}
        per_book: dict[str, list[dict]] = {}
        for row in rows:
            per_book.setdefault(str(row["book"]), []).append(row)
        book_rows = []
        for code, figures in sorted(((code, _chain(lines_)) for code, lines_ in per_book.items()),
                                    key=lambda pair: -pair[1]["revenue"]):
            branch = named.get(code)
            book_rows.append({
                "key": str(branch.id) if branch else "", "book": code,
                "label": branch.name if branch else code,
                "revenue": money(figures["revenue"]), "cost": money(figures["cost"]),
                "gross": money(figures["gross"]), "expense": money(figures["expense"]),
                "net": money(figures["net"]),
                "margin": _margin(figures),
            })
        # The company's own line, last, and only when there is a book above it. Without it this table is four books
        # at a 100% margin and one at 60% with nothing on the screen to say the real answer is 11.89%; with it the
        # arithmetic explains itself, the margins that cannot be read saying so right above the one that can.
        if book_rows:
            book_rows.append({
                "key": "", "book": "ALL", "label": "All books",
                "revenue": money(chain["revenue"]), "cost": money(chain["cost"]),
                "gross": money(chain["gross"]), "expense": money(chain["expense"]),
                "net": money(chain["net"]), "margin": _margin(chain),
            })
        visuals.append(table(
            "books", "By book", "branch",
            [{"key": "book", "label": "Book"}, {"key": "label", "label": "Branch"},
             {"key": "revenue", "label": "Revenue", "align": "right"},
             {"key": "cost", "label": "Cost of Sale", "align": "right"},
             {"key": "gross", "label": "Gross Profit", "align": "right"},
             {"key": "margin", "label": "Margin %", "align": "right"},
             {"key": "expense", "label": "Expense", "align": "right"},
             {"key": "net", "label": "Net Profit", "align": "right"}],
            book_rows))

        income = sorted((r for r in rows if _section(str(r["cat"] or ""), str(r["typ"] or "")) in CREDIT_SECTIONS),
                        key=lambda r: -_amount(r))
        spent = sorted((r for r in rows if _section(str(r["cat"] or ""), str(r["typ"] or "")) not in CREDIT_SECTIONS),
                       key=lambda r: -_amount(r))
        visuals.append(_bars("top_revenue", "Biggest revenue accounts", [
            {"key_": r["id"], "label": _label(r, book), "value": _amount(r)} for r in income[:TOP_ACCOUNTS]]))
        visuals.append(_bars("top_expense", "Biggest cost and expense accounts", [
            {"key_": r["id"], "label": _label(r, book), "value": _amount(r)} for r in spent[:TOP_ACCOUNTS]]))
        return {"cards": cards, "visuals": visuals, "covers": _covers(rows, days)}

    # page_id == "accounts"
    groups: dict[str, Decimal] = {}
    for row in rows:
        if _section(str(row["cat"] or ""), str(row["typ"] or "")) in CREDIT_SECTIONS:
            continue
        name = str(row["grp"] or "--")
        groups[name] = groups.get(name, ds._d(0)) + _amount(row)
    ranked = sorted(groups.items(), key=lambda pair: -pair[1])[:TOP_GROUPS]
    visuals.append(_bars("groups", "What it cost, by account group",
                         [{"key_": name, "label": name, "value": amount} for name, amount in ranked]))

    listed = sorted(rows, key=lambda r: -abs(_amount(r)))[:TABLE_ROWS]
    revenue = chain["revenue"]
    visuals.append(table(
        "accounts", "Accounts that moved", None,
        [{"key": "book", "label": "Book"}, {"key": "code", "label": "Code"},
         {"key": "label", "label": "Account"}, {"key": "grp", "label": "Group"},
         {"key": "section", "label": "Section"},
         {"key": "entries", "label": "Entries", "align": "right"},
         {"key": "debit", "label": "Debit", "align": "right"},
         {"key": "credit", "label": "Credit", "align": "right"},
         {"key": "value", "label": "Movement", "align": "right"},
         {"key": "share", "label": "% of revenue", "align": "right"}],
        [{
            "key": str(r["id"]), "book": str(r["book"]), "code": str(r["code"] or ""),
            "label": str(r["name"] or r["code"] or "--"), "grp": str(r["grp"] or ""),
            "section": SECTION_LABEL.get(_section(str(r["cat"] or ""), str(r["typ"] or "")), ""),
            "entries": str(int(r["entries"] or 0)),
            "debit": money(r["dr"]), "credit": money(r["cr"]),
            "value": money(_amount(r)), "share": pct(_amount(r), revenue),
        } for r in listed],
        note=None if listed else "Nothing was posted to a profit and loss account in this window."))
    return {"cards": cards, "visuals": visuals, "covers": _covers(rows, days)}


def _covers(rows: list[dict], days: int) -> list[dict]:
    """The strip under the title, counted from what this page actually read, in words that fit the books.

    Supplied rather than left out, because left out the controller falls back to `dashboards_service.headline`,
    which reads 1.87 million product-days to count Items and brands that no figure on this page comes from.

    It used to be forced into the app's five fixed words and read "93 Items, 4 categories, 11 brands" about a
    profit and loss account, with a note here explaining that Items meant accounts. A strip that needs a footnote
    to be read is a strip that should say the right word instead, so a page now names its own.
    """
    sections = {_section(str(r["cat"] or ""), str(r["typ"] or "")) for r in rows}
    return [
        {"label": "accounts that moved", "value": len(rows)},
        {"label": "account groups", "value": len({str(r["grp"] or "") for r in rows})},
        {"label": "sections", "value": len(sections)},
        {"label": "books", "value": len({str(r["book"]) for r in rows})},
        {"label": "trading days", "value": days},
    ]
