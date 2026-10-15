"""Analytical dashboards: a page of visuals that all answer the same question, and all narrow together.

WHAT MAKES THIS DIFFERENT FROM THE KPI DASHBOARD
-----------------------------------------------
The control room answers "how are we doing" in tiles, each one its own figure with its own drill-down. This answers
"why", and the whole page is one query: click Hair Care on the category chart and every other visual on the page --
the trend, the brands, the branches, the top sellers, the table -- redraws for Hair Care. That cross-filter IS the
feature. The charts are the easy half.

ONE ANSWER PER PAGE, NOT ONE PER VISUAL
---------------------------------------
Every page is a single endpoint returning every visual on it. Eight visuals would otherwise be eight requests that
re-read the same 1.87 million product-days eight times, and a click would fire all eight again. Here the filter is
applied once, the scope is built once, and each fold is a GROUP BY over the same narrowed set.

WHERE THE FIGURES COME FROM
---------------------------
    branch_product_stats   1,874,022 rows, one per Item per branch per day, carrying department, category, brand,
                           qty, net_sales and cogs. This is what makes a dimensional dashboard possible at all,
                           and `idx_bps_day_covering` is what makes it fast.
    branch_daily_stats     one row per branch per day: the trend line, invoices, discount, basket.
    branch_sales           bill level, for anything per-invoice, and the only place with a time on it.

Branch stock, cash in and cash out are deliberately absent: they are not dimensional, and a stock figure summed
over a date range is a figure multiplied by the number of days in it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.core import dialect

D0 = Decimal("0")

# Every dimension a visual may be clicked on, and the column it narrows. A visual declares one of these as its
# `dimension`; the app sends it straight back as a filter, so adding a chart never means adding filter code.
DIMENSIONS = {
    "branch": "s.branch_id",
    "department": "s.department",
    "category": "s.category",
    "brand": "s.brand",
    "sku": "s.product_sku",
}


# The product hierarchy, coarsest first. A branch is deliberately not in it: a branch is a PLACE, orthogonal to
# what was sold, and narrowing to one shop does not make "by department" a less useful chart.
DRILL_ORDER = ("department", "category", "brand", "sku")


def unpinned(scope, prefer: tuple[str, ...], count: int) -> list[str]:
    """Which product levels a page should draw, given what has already been clicked.

    A chart of the level you have just filtered to is one bar reading 100 per cent, and a table of it is one row.
    Both were being drawn: narrowing ABC Analysis to FOOD left "By category" as a single bar, "Where the long
    tail sits" as a single row, and half the screen beside them empty. That empty half is the page having nothing
    further to say at a level it has already answered.

    What it does have is the level BELOW. So a pinned level is dropped and the list is topped up from
    `DRILL_ORDER` with whatever is still open, which turns the filter into a drill-down: pick a category and the
    page moves to brands and Items, pick a brand and it moves to Items. Pin everything and it returns fewer than
    `count`, and the caller draws fewer charts rather than inventing one.

    IT ONLY EVER GOES DOWN. A level COARSER than the one picked collapses just as badly as the picked level
    itself: under `category=FOOD` a department chart is one bar, because FOOD sits in one department. The first
    version topped up from the start of `DRILL_ORDER` and handed back exactly that, which the drill check caught
    on four pages. So the floor is the deepest level already pinned, and everything offered is below it.
    """
    pinned = [name for name in DRILL_ORDER if name in scope.picked]
    floor = DRILL_ORDER.index(pinned[-1]) if pinned else -1
    out = [name for name in prefer
           if name not in scope.picked and DRILL_ORDER.index(name) > floor]
    for name in DRILL_ORDER[floor + 1:]:
        if len(out) >= count:
            break
        if name not in scope.picked and name not in out:
            out.append(name)
    return out[:count]


# What each level is called in a chart title, so a drilled page names itself correctly rather than saying
# "By category" over a chart of brands.
LEVEL_WORD = {"department": "department", "category": "category", "brand": "brand", "sku": "Item",
              "branch": "branch"}


@dataclass
class Scope:
    """The window and every narrowing on it. One of these is built per request and every fold below takes it."""

    start: date
    end: date
    at_start: datetime | None = None
    at_end: datetime | None = None
    picked: dict[str, str] = field(default_factory=dict)
    # ONE BAND OF A CLASSED DASHBOARD: "A" on ABC Analysis, "X" on XYZ. Not a dimension and deliberately not in
    # `picked`, because a class is not a column on any table -- it is worked out from the window, by the
    # dashboard, every time. So `where()` knows nothing about it and only the two dashboards that cut classes
    # read it, to show one band of the list they already rank. Clicking Class A is what sets it.
    band: str | None = None

    @property
    def timed(self) -> bool:
        return self.at_start is not None or self.at_end is not None

    def where(self, alias: str = "s", day_column: str = "day",
              only: tuple[str, ...] | None = None) -> tuple[str, list]:
        """The SQL every fold shares. `picked` is whatever the person clicked, in the order they clicked it.

        `only` names the dimensions the table being filtered actually HAS. Only `branch_product_stats` carries a
        department, a category and a brand; `branch_sales` and `branch_hourly_stats` carry a branch and a day and
        nothing else. Applying all of them everywhere asks SQL Server for a column named `category` on a table of
        bills, which is exactly what it did the first time this ran.
        """
        where = [f"{alias}.{day_column} >= ?", f"{alias}.{day_column} <= ?"]
        args: list = [self.start, self.end]
        for name, value in self.picked.items():
            column = DIMENSIONS.get(name)
            if not column or (only is not None and name not in only):
                continue
            column = column.replace("s.", f"{alias}.", 1)
            # Keys are char(36) on SQL Server and a plain parameter binds as nvarchar, which converts on every
            # row. `dialect.mark()` casts it; measured elsewhere in this codebase at nine times on one comparison.
            if name == "branch":
                where.append(f"{column} = {dialect.mark()}")
            else:
                where.append(f"{column} = ?")
            args.append(value)
        return " AND ".join(where), args


async def _rows(sql: str, args: list) -> list[dict]:
    from tortoise import Tortoise

    from app.services import query_cache

    return await query_cache.remember(
        sql, args, lambda: Tortoise.get_connection("default").execute_query_dict(sql, args))


async def picked_branch(scope: Scope):
    """The Branch row a page has been narrowed to, or None when none has been.

    Every dashboard can be read a branch at a time, and the three tables they fold from say "branch" in three
    different ways: `branch_product_stats` and `branch_sales` carry a `branch_id`; `acc_vouchers` and
    `acc_accounts` carry a `book`, which IS a shop ('GT', 'GG', 'MT', 'FC', and head office's own 'HO'); and a
    purchase order carries a `for_branch_id`. The first needs nothing but the id, the second needs the branch's
    code, and all three need to know the branch exists before saying a page is empty. That is what this is for.

    Returns None for an id that is not a branch of ours as well as for no filter at all, so a caller that cares
    about the difference checks `scope.picked` itself. Every caller here does.
    """
    picked = scope.picked.get("branch")
    if not picked:
        return None
    from app.models import Branch

    return await Branch.filter(id=picked).first()


async def branch_list() -> list[dict]:
    """Every branch a dashboard may be narrowed to, for the picker that sits on all of them.

    Head office is in the list and belongs there: it keeps the book most of `payables` and `finance` is about, and
    it raises the purchase orders `purchasing` draws. It has no counter sales, so narrowing a sales page to it
    answers "nothing", which is the true answer rather than a missing option.
    """
    from app.models import Branch

    rows = await Branch.filter(status="active").order_by("name").values("id", "code", "name")
    return [{"id": str(r["id"]), "code": r["code"], "name": r["name"]} for r in rows]


def _d(value) -> Decimal:
    if value is None:
        return D0
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ── the folds ───────────────────────────────────────────────────────────────────────────────────
async def headline(scope: Scope) -> dict:
    """The strip across the top. Taken from the product-days so it agrees with every chart beneath it: a headline
    built from a different table than the charts is a headline somebody will one day find does not add up."""
    where, args = scope.where()
    row = (await _rows(f"""
        SELECT SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs, SUM(s.qty) AS qty,
               COUNT(DISTINCT s.product_sku) AS skus, COUNT(DISTINCT s.branch_id) AS branches,
               COUNT(DISTINCT s.category) AS categories, COUNT(DISTINCT s.brand) AS brands,
               COUNT(DISTINCT s.day) AS days
          FROM branch_product_stats s WHERE {where}
    """, args))[0]
    net, cogs = _d(row["net"]), _d(row["cogs"])
    days = int(row["days"] or 0)
    return {
        "net": net, "cogs": cogs, "profit": net - cogs,
        "margin": (net - cogs) / net * 100 if net else D0,
        "qty": _d(row["qty"]), "skus": int(row["skus"] or 0), "days": days,
        "branches": int(row["branches"] or 0), "categories": int(row["categories"] or 0),
        "brands": int(row["brands"] or 0),
        "per_day": net / days if days else D0,
    }


async def invoices(scope: Scope) -> dict:
    """Bill-level figures, which the product-days cannot give: how many bills, and what was in each.

    Narrowed by category or brand this answers a different question and says so -- "bills that contained Hair
    Care", not "Hair Care's share of a bill" -- so the app labels it that way when a dimension is picked.
    """
    # A bill carries a branch and a day, never a category, so only those two narrow it directly.
    where, args = scope.where("b", only=("branch",))
    narrowed = any(k in scope.picked for k in ("department", "category", "brand", "sku"))
    join = ""
    if narrowed:
        # THROUGH THE LINES, NOT THE DAILY ROLLUP. `branch_product_stats` is one row per Item per branch per DAY
        # and holds no bill, so joining bills to it on branch and day matches every bill on a day that Item sold
        # anywhere in the branch. Measured: filtering to the brand Shan that way claimed 40,860 of 47,229 bills
        # contained it, against Rs 140,341 of Shan sales. The lines know which bill they were on; the rollup does
        # not. Category and brand live on the Item, so the Item is joined for them.
        on = []
        line_args: list = []
        for name, value in scope.picked.items():
            if name == "sku":
                # Cast, not a bare `?`. An Item code is varchar on SQL Server and a plain parameter binds as
                # nvarchar, so the comparison converts on every one of 3,553,327 line rows instead of seeking.
                # `ops/checks/sql_key_casts.py` is what catches this, and it caught exactly this line.
                on.append(f"l.product_sku = {dialect.mark('varchar(60)')}")
            elif name == "department":
                on.append("l.department = ?")
            elif name == "category":
                on.append("p.category = ?")
            elif name == "brand":
                on.append("p.brand = ?")
            else:
                continue
            line_args.append(value)
        join = f"""JOIN (SELECT DISTINCT l.sale_id FROM branch_sale_lines l
                          JOIN products p ON p.sku = l.product_sku
                         WHERE {' AND '.join(on)}) k ON k.sale_id = b.id"""
        args = line_args + args
    row = (await _rows(f"""
        SELECT COUNT(*) AS bills, SUM(b.net_value) AS net, SUM(b.disc_total) AS disc
          FROM branch_sales b {join} WHERE {where}
    """, args))[0]
    bills = int(row["bills"] or 0)
    net = _d(row["net"])
    return {"bills": bills, "net": net, "disc": _d(row["disc"]),
            "basket": net / bills if bills else D0, "narrowed": narrowed}


async def trend(scope: Scope) -> list[dict]:
    """Day by day. The spine of the page: whatever is clicked, this is what it did over time."""
    where, args = scope.where()
    return await _rows(f"""
        SELECT s.day AS label, SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs, SUM(s.qty) AS qty
          FROM branch_product_stats s WHERE {where}
         GROUP BY s.day ORDER BY s.day
    """, args)


async def by_dimension(scope: Scope, dimension: str, limit: int = 12) -> list[dict]:
    """One dimension, biggest first. Everything that is clickable on the page comes through here, which is why a
    new chart is a line of configuration rather than a new query."""
    column = DIMENSIONS.get(dimension)
    if not column:
        return []
    where, args = scope.where()
    label = "s.product_name" if dimension == "sku" else column
    group = f"{column}, s.product_name" if dimension == "sku" else column
    return await _rows(f"""
        SELECT {dialect.top(limit)} {column} AS key_, {label} AS label,
               SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs, SUM(s.qty) AS qty
          FROM branch_product_stats s WHERE {where} AND {column} IS NOT NULL AND {column} <> ''
         GROUP BY {group} ORDER BY SUM(s.net_sales) DESC {dialect.limit(limit)}
    """, args)


async def branches(scope: Scope) -> list[dict]:
    """By branch, with the branch's own name rather than its id, and its share of the whole."""
    where, args = scope.where()
    return await _rows(f"""
        SELECT s.branch_id AS key_, MIN(br.name) AS label, MIN(br.code) AS code,
               SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs, SUM(s.qty) AS qty,
               COUNT(DISTINCT s.day) AS days
          FROM branch_product_stats s JOIN branches br ON br.id = s.branch_id
         WHERE {where}
         GROUP BY s.branch_id ORDER BY SUM(s.net_sales) DESC
    """, args)


async def hours(scope: Scope) -> list[dict]:
    """The trading day by hour. Only from the bills, because only a bill knows what time it was."""
    if any(k in scope.picked for k in ("department", "category", "brand", "sku")):
        # The hourly rollup has no Item in it, so a dimensional filter cannot reach it. Returning nothing, and
        # letting the page say why, beats drawing a chart that quietly ignores what was clicked.
        return []
    where, args = scope.where("h", only=("branch",))
    return await _rows(f"""
        SELECT h.hour AS label, SUM(h.net_sales) AS net, SUM(h.invoices) AS invoices
          FROM branch_hourly_stats h WHERE {where}
         GROUP BY h.hour ORDER BY h.hour
    """, args)


async def table(scope: Scope, limit: int = 200) -> list[dict]:
    """Where the page lands: the Items themselves, which is what somebody chasing a number actually wants."""
    where, args = scope.where()
    return await _rows(f"""
        SELECT {dialect.top(limit)} s.product_sku AS sku, MIN(s.product_name) AS name,
               MIN(s.department) AS department, MIN(s.category) AS category, MIN(s.brand) AS brand,
               SUM(s.qty) AS qty, SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs
          FROM branch_product_stats s WHERE {where}
         GROUP BY s.product_sku ORDER BY SUM(s.net_sales) DESC {dialect.limit(limit)}
    """, args)
