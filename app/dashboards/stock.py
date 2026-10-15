"""Stock Health: what the shelves are holding, where it sits, and what is not moving.

THE ONE THING TO KNOW BEFORE CHANGING ANYTHING HERE
---------------------------------------------------
`branch_product_stock` has no `day` column. It is one row per branch per Item saying what that branch last
reported, so **a figure out of it must never be summed over a date range**: thirty days of scope would multiply
Rs 124,113,595 of stock by thirty and answer Rs 3.7 billion, which is not wrong by a rounding, it is wrong by the
calendar. Every fold against it therefore builds its own WHERE and never calls `Scope.where`, because that helper
always emits the two day bounds and there is no way to ask it not to.

So the dates on this page narrow the SALES half only. Each stock visual carries a note saying so in plain words
rather than quietly drawing a picture of a window it never read.

ONE PICTURE PER BRANCH, NOT EVERY PICTURE IT EVER SENT
------------------------------------------------------
A branch holds tens of thousands of lines and pushes them in chunks, so every row carries the `snapshot_id` of the
push that wrote it and the previous generation is dropped only once the branch says the push finished (see
`models/stock_snapshot.py`). A plain `SELECT ... FROM branch_product_stock` is therefore right only while no push
is in flight, and doubles a branch the moment one is. Measured today: 24 complete runs across the four branches,
one live generation each, 43,135 rows in all. `_live_snapshots` pins each branch to its latest complete picture,
which is the same thing `branch_stock_service` reads, so this page and the Stock screen cannot disagree.

WHERE "WHAT HAS NOT SOLD" COMES FROM, AND WHY IT IS NOT THE OBVIOUS COLUMN
--------------------------------------------------------------------------
`branch_product_stock` carries `last_sold_at`, `units_sold_period` and `days_with_sales`, and all three are dead:
NULL or zero on all 43,135 rows, along with `last_received_at`, `last_moved_at`, `changed_at`, `flows` and
`locations`. They are filled by a branch server watching its own movements, and none of these four shops runs one
-- they are loaded nightly out of Multi-Tec, which sends a quantity and a cost and no history. That is already
written down as accounted-for in `ops/checks/legacy_blank_columns.py`.

Movement is therefore read from `branch_product_stats`, which is alive: 1,874,022 rows, one per Item per branch per
day. "Not sold" means no row there for that branch and that Item inside the scope's dates, which is a fact about
the dates rather than a column somebody forgot to fill.
"""
from __future__ import annotations

from app.core import dialect
from app.dashboards._shape import bars, card, donut, money, pct, table, to_page, to_screen
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Stock Health",
    "blurb": "What stock is worth, where it sits, and what is not moving. Click anything to narrow the whole page.",
    "resource": "executive.stock",
    "pages": [
        {"id": "overview", "label": "Overview"},
        {"id": "movement", "label": "Movement"},
    ],
}

# Every dimension a stock row carries -- all five, as it happens, because `branch_product_stock` keeps the Item's
# department, category and brand on the row beside the quantity. What it does not keep is a day, which is why this
# table exists separately from `dashboards_service.DIMENSIONS` instead of being borrowed from it.
STOCK_DIMENSIONS = {
    "branch": "s.branch_id",
    "department": "s.department",
    "category": "s.category",
    "brand": "s.brand",
    "sku": "s.product_sku",
}

NO_DATES = ("Stock is what each branch last reported, not a day's worth of anything, so the dates above do not "
            "narrow it. Everything else you have clicked does.")

NOTHING_LIVE = ("No branch in this narrowing has finished sending a stock picture, so there is nothing to count. "
                "Head office is not a branch: its stock is the godown, which is counted on its own.")


# ── the live picture, and the filter every stock fold shares ────────────────────────────────────
async def _live_snapshots() -> list[tuple[str, str]]:
    """Each branch's latest COMPLETE stock picture, as (branch id, snapshot id).

    Deduped here rather than with a window function, because thirteen of the checks in `ops/checks` run against a
    copy of cloud.db on SQLite and this is a table of two dozen rows. Picking the latest *complete* run is also
    what makes a half-finished push harmless: its rows are in the table beside the last good generation's, and
    this ignores them until the branch says the push is done.
    """
    rows = await ds._rows(
        "SELECT r.branch_id AS branch_id, r.snapshot_id AS snapshot_id, r.completed_at AS completed_at "
        "  FROM branch_snapshot_runs r WHERE r.status = 'complete' ORDER BY r.completed_at DESC", [])
    live: dict[str, str] = {}
    for row in rows:
        live.setdefault(str(row["branch_id"]), str(row["snapshot_id"]))
    return list(live.items())


def _stock_where(scope: Scope, live: list[tuple[str, str]]) -> tuple[str, list]:
    """The WHERE every stock fold shares: the live generation, plus whatever was clicked, and no dates at all.

    An empty clause means there is nothing to read, and every caller turns that into an empty visual with
    `NOTHING_LIVE` on it rather than into SQL with a hole in the middle.
    """
    picked_branch = scope.picked.get("branch")
    pairs = [(branch_id, snapshot) for branch_id, snapshot in live
             if not picked_branch or branch_id == picked_branch]
    if not pairs:
        return "", []
    # Branch and snapshot go together or not at all: filtering on the branch alone would read every generation it
    # ever sent, and on the snapshot alone would be a bare id with nothing to scope it.
    where = ["(" + " OR ".join(
        f"(s.branch_id = {dialect.mark()} AND s.snapshot_id = {dialect.mark('varchar(60)')})"
        for _ in pairs) + ")"]
    args: list = []
    for branch_id, snapshot in pairs:
        args += [branch_id, snapshot]
    for name, value in scope.picked.items():
        column = STOCK_DIMENSIONS.get(name)
        # The branch is already pinned by the pair above, so comparing it again would only repeat the work.
        if not column or name == "branch":
            continue
        where.append(f"{column} = {dialect.mark('varchar(60)')}" if name == "sku" else f"{column} = ?")
        args.append(value)
    return " AND ".join(where), args


# ── the folds ───────────────────────────────────────────────────────────────────────────────────
async def _totals(where: str, args: list) -> dict:
    """The strip across the top of the Overview, from the same rows the charts beneath it read.

    Value at cost and value at retail are both carried because they answer different questions: cost is what the
    money is worth to the business, retail is what the shelf says, and the gap between them is the markup sitting
    in the shop. Measured today: Rs 124,113,595.45 at cost against Rs 171,111,252.38 at retail.
    """
    if not where:
        return {}
    return (await ds._rows(f"""
        SELECT COUNT(*) AS lines, COUNT(DISTINCT s.branch_id) AS branches,
               COUNT(DISTINCT s.product_sku) AS skus, SUM(s.qty) AS units,
               SUM(s.qty * s.avg_cost) AS at_cost, SUM(s.qty * s.price) AS at_retail,
               SUM(CASE WHEN s.qty > 0 THEN 1 ELSE 0 END) AS holding,
               SUM(CASE WHEN s.qty = 0 THEN 1 ELSE 0 END) AS at_zero,
               SUM(CASE WHEN s.qty < 0 THEN 1 ELSE 0 END) AS negative,
               SUM(CASE WHEN s.qty < 0 THEN s.qty * s.avg_cost ELSE 0 END) AS negative_cost
          FROM branch_product_stock s WHERE {where}
    """, args))[0]


async def _by_branch(where: str, args: list) -> list[dict]:
    """One line per branch, with the branch's own name rather than its id, and how its shelves stand."""
    if not where:
        return []
    return await ds._rows(f"""
        SELECT s.branch_id AS key_, MIN(br.name) AS label, MIN(br.code) AS code, COUNT(*) AS lines,
               SUM(s.qty) AS qty, SUM(s.qty * s.avg_cost) AS at_cost, SUM(s.qty * s.price) AS at_retail,
               SUM(CASE WHEN s.qty > 0 THEN 1 ELSE 0 END) AS holding,
               SUM(CASE WHEN s.qty = 0 THEN 1 ELSE 0 END) AS at_zero,
               SUM(CASE WHEN s.qty < 0 THEN 1 ELSE 0 END) AS negative
          FROM branch_product_stock s JOIN branches br ON br.id = s.branch_id
         WHERE {where} GROUP BY s.branch_id ORDER BY SUM(s.qty * s.avg_cost) DESC
    """, args)


async def _by_stock_dimension(where: str, args: list, dimension: str, limit: int = 14) -> list[dict]:
    """One dimension of the stock picture, most valuable first. Every clickable stock chart comes through here."""
    column = STOCK_DIMENSIONS.get(dimension)
    if not where or not column:
        return []
    # The drill can now reach the Items themselves, and a chart of bare product codes is one nobody can read, so
    # an Item is grouped by its code and labelled by its name. Everything coarser is its own label.
    label = "MIN(s.product_name)" if dimension == "sku" else column
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} {column} AS key_, {label} AS label,
               SUM(s.qty) AS qty, SUM(s.qty * s.avg_cost) AS at_cost
          FROM branch_product_stock s
         WHERE {where} AND {column} IS NOT NULL AND {column} <> ''
         GROUP BY {column} ORDER BY SUM(s.qty * s.avg_cost) DESC {dialect.limit(limit)}
    """, args)


async def _negative_lines(where: str, args: list, limit: int = 40) -> list[dict]:
    """The lines a branch's own books say are below nothing, worst first and NOT clamped.

    Fort Colony holds 486 of these and Garden Town 2,514, Rs 3,391,110.19 of negative value between the four
    branches. Clamping them at zero would make the stock value read Rs 127,504,705.63 and would hide the thing
    worth seeing: a shelf goes negative when a sale is rung up before the delivery that supplied it is entered, so
    each of these is a receipt somebody has not posted.
    """
    if not where:
        return []
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} s.product_sku AS key_, MIN(s.product_name) AS label,
               MIN(br.code) AS code, MIN(s.department) AS department, MIN(s.category) AS category,
               SUM(s.qty) AS qty, SUM(s.qty * s.avg_cost) AS at_cost
          FROM branch_product_stock s JOIN branches br ON br.id = s.branch_id
         WHERE {where} AND s.qty < 0
         GROUP BY s.branch_id, s.product_sku
         ORDER BY SUM(s.qty * s.avg_cost) ASC {dialect.limit(limit)}
    """, args)


async def _godown(scope: Scope) -> dict | None:
    """Head office's own stock, which is the godown, counted from its movement ledger.

    None when a branch is picked, because the godown is not a branch and a row claiming otherwise would be read as
    one. `selling_branches` has the whole argument; the short version is that head office has never reported a
    stock line, so it is absent from `branch_product_stock` entirely and its stock has to be counted here instead.

    The balance is `SUM(qty)` over the signed ledger, the same way `executive_service.godown_balances` counts it,
    so the two cannot drift apart. `costed` is carried because of what it found: `products.avg_cost` is blank on
    68,029 of 68,030 Items, so valuing the godown at cost answers Rs 1,200 against Rs 11,956,239.93 at retail.
    That is not a cheap godown, it is a column head office does not fill, and the table says "not recorded" rather
    than printing it.
    """
    if "branch" in scope.picked:
        return None
    where = ["1 = 1"]
    args: list = []
    for name, column in (("department", "p.department"), ("category", "p.category"), ("brand", "p.brand")):
        if name in scope.picked:
            where.append(f"{column} = ?")
            args.append(scope.picked[name])
    if "sku" in scope.picked:
        where.append(f"p.sku = {dialect.mark('varchar(60)')}")
        args.append(scope.picked["sku"])
    return (await ds._rows(f"""
        SELECT COUNT(*) AS lines, SUM(t.units) AS units,
               SUM(t.units * p.avg_cost) AS at_cost, SUM(t.units * p.price) AS at_retail,
               SUM(CASE WHEN p.avg_cost > 0 THEN 1 ELSE 0 END) AS costed,
               SUM(CASE WHEN t.units > 0 THEN 1 ELSE 0 END) AS holding,
               SUM(CASE WHEN t.units = 0 THEN 1 ELSE 0 END) AS at_zero,
               SUM(CASE WHEN t.units < 0 THEN 1 ELSE 0 END) AS negative
          FROM (SELECT x.product_id AS product_id, SUM(x.qty) AS units
                  FROM warehouse_stock_movements x GROUP BY x.product_id) t
          JOIN products p ON p.id = t.product_id
         WHERE {' AND '.join(where)}
    """, args))[0]


async def _not_sold(scope: Scope, where: str, args: list, limit: int = 100) -> list[dict]:
    """Stock on the shelf with no sale of it at that branch inside the scope's dates.

    The grain is branch and Item, not Item, because that is the grain somebody acts on: an Item dead at Gulgasht
    and selling at Garden Town is a transfer, and one row per Item would hide which half is which.

    The subquery matches on branch, Item and day ONLY. Narrowing it by category as well would be asking a
    different question -- "did it sell as a member of this category" -- and the department on a sales row and the
    department on a stock row are two copies of the same fact that can drift. The outer WHERE is where the
    narrowing belongs, and that is applied to the stock side where the person clicked it.
    """
    if not where:
        return []
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} s.product_sku AS key_, MIN(s.product_name) AS label,
               MIN(br.code) AS code, MIN(s.department) AS department, MIN(s.category) AS category,
               SUM(s.qty) AS qty, SUM(s.qty * s.avg_cost) AS at_cost
          FROM branch_product_stock s JOIN branches br ON br.id = s.branch_id
         WHERE {where} AND s.qty > 0
           AND NOT EXISTS (SELECT 1 FROM branch_product_stats m
                            WHERE m.branch_id = s.branch_id AND m.product_sku = s.product_sku
                              AND m.day >= ? AND m.day <= ?)
         GROUP BY s.branch_id, s.product_sku
         ORDER BY SUM(s.qty * s.avg_cost) DESC {dialect.limit(limit)}
    """, args + [scope.start, scope.end])


async def _not_sold_totals(scope: Scope, where: str, args: list) -> dict:
    """What the dead stock adds up to, folded the same way the list above is so the two agree."""
    if not where:
        return {}
    return (await ds._rows(f"""
        SELECT COUNT(*) AS lines, SUM(s.qty) AS units, SUM(s.qty * s.avg_cost) AS at_cost
          FROM branch_product_stock s
         WHERE {where} AND s.qty > 0
           AND NOT EXISTS (SELECT 1 FROM branch_product_stats m
                            WHERE m.branch_id = s.branch_id AND m.product_sku = s.product_sku
                              AND m.day >= ? AND m.day <= ?)
    """, args + [scope.start, scope.end]))[0]


async def _not_sold_by_dimension(scope: Scope, where: str, args: list,
                                 dimension: str, limit: int = 14) -> list[dict]:
    """The dead stock folded one dimension at a time, so the answer to "what is not moving" can be clicked.

    A separate fold from `_by_stock_dimension` rather than a flag on it: the dates belong to the subquery here and
    to nothing at all there, and one function taking both would have two different argument orders.
    """
    column = STOCK_DIMENSIONS.get(dimension)
    if not where or not column:
        return []
    label = "MIN(s.product_name)" if dimension == "sku" else column
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} {column} AS key_, {label} AS label,
               SUM(s.qty) AS qty, SUM(s.qty * s.avg_cost) AS at_cost
          FROM branch_product_stock s
         WHERE {where} AND s.qty > 0 AND {column} IS NOT NULL AND {column} <> ''
           AND NOT EXISTS (SELECT 1 FROM branch_product_stats m
                            WHERE m.branch_id = s.branch_id AND m.product_sku = s.product_sku
                              AND m.day >= ? AND m.day <= ?)
         GROUP BY {column} ORDER BY SUM(s.qty * s.avg_cost) DESC {dialect.limit(limit)}
    """, args + [scope.start, scope.end])


async def _running_out(scope: Scope, where: str, args: list, limit: int = 60) -> list[dict]:
    """The fast movers with the least left: fewest days of cover first.

    Days of cover is stock on hand divided by units sold per day, and "per day" here is over the CALENDAR days in
    the scope rather than over the days that branch happened to trade. The two are the same number today -- all
    four branches traded all 30 days of the test window -- and calendar days is what a buyer means by "a day", so
    the simpler one is used and said out loud in the note instead of being inferred.

    The derived table is not narrowed by what was clicked: it joins on branch and Item, so the outer WHERE has
    already decided which rows can survive the join.
    """
    if not where:
        return []
    days = (scope.end - scope.start).days + 1
    # Written out twice because SQL Server cannot ORDER BY a select-list alias that is an aggregate expression.
    cover = f"SUM(s.qty) * {days} / MIN(m.units)"
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} s.product_sku AS key_, MIN(s.product_name) AS label,
               MIN(br.code) AS code, MIN(s.department) AS department,
               SUM(s.qty) AS qty, MIN(m.units) AS units, {cover} AS cover
          FROM branch_product_stock s
          JOIN branches br ON br.id = s.branch_id
          JOIN (SELECT x.branch_id AS branch_id, x.product_sku AS product_sku, SUM(x.qty) AS units
                  FROM branch_product_stats x WHERE x.day >= ? AND x.day <= ?
                 GROUP BY x.branch_id, x.product_sku) m
            ON m.branch_id = s.branch_id AND m.product_sku = s.product_sku
         WHERE {where} AND s.qty > 0 AND m.units > 0
         GROUP BY s.branch_id, s.product_sku
         ORDER BY {cover} ASC, s.product_sku {dialect.limit(limit)}
    """, [scope.start, scope.end] + args)


async def _sold_out(scope: Scope, where: str, args: list, limit: int = 60) -> list[dict]:
    """What sold in these dates and is now at zero or below: the shelf that is empty behind a live seller.

    Ordered by what it took in rather than by how negative it is, because the question is what the empty shelf is
    costing. Garden Town's SIMPLE FARM EGG 30PCS sold Rs 160,800 in the test window and the picture puts it at
    -126.
    """
    if not where:
        return []
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} s.product_sku AS key_, MIN(s.product_name) AS label,
               MIN(br.code) AS code, MIN(s.department) AS department,
               SUM(s.qty) AS qty, MIN(m.units) AS units, MIN(m.net) AS net
          FROM branch_product_stock s
          JOIN branches br ON br.id = s.branch_id
          JOIN (SELECT x.branch_id AS branch_id, x.product_sku AS product_sku,
                       SUM(x.qty) AS units, SUM(x.net_sales) AS net
                  FROM branch_product_stats x WHERE x.day >= ? AND x.day <= ?
                 GROUP BY x.branch_id, x.product_sku) m
            ON m.branch_id = s.branch_id AND m.product_sku = s.product_sku
         WHERE {where} AND s.qty <= 0 AND m.units > 0
         GROUP BY s.branch_id, s.product_sku
         ORDER BY MIN(m.net) DESC {dialect.limit(limit)}
    """, [scope.start, scope.end] + args)


# ── the pages ───────────────────────────────────────────────────────────────────────────────────
def _where_it_sits(branch_rows: list[dict], godown: dict | None) -> dict:
    """Branches and the godown side by side, with the one cell that cannot be filled saying so.

    Not clickable, and that is deliberate: four of its rows are branches and one is not, so a single `dimension`
    would send the godown back as a branch id. The bar chart beside it is the clickable one.
    """
    rows = [{
        "key": str(r["key_"]), "label": f"{r['label']} ({r['code']})",
        "lines": str(int(r["lines"] or 0)), "holding": str(int(r["holding"] or 0)),
        "at_zero": str(int(r["at_zero"] or 0)), "negative": str(int(r["negative"] or 0)),
        "units": money(r["qty"]), "at_cost": money(r["at_cost"]), "at_retail": money(r["at_retail"]),
    } for r in branch_rows]
    note = NO_DATES
    if godown is not None:
        lines = int(godown["lines"] or 0)
        rows.append({
            "key": "godown", "label": "Godown (head office)",
            "lines": str(lines), "holding": str(int(godown["holding"] or 0)),
            "at_zero": str(int(godown["at_zero"] or 0)), "negative": str(int(godown["negative"] or 0)),
            "units": money(godown["units"]),
            # Only when every Item behind it has a cost. One of 2,118 does, so this reads "not recorded" today and
            # will start printing a figure by itself if head office ever fills the column in.
            "at_cost": money(godown["at_cost"]) if lines and int(godown["costed"] or 0) == lines
                       else "not recorded",
            "at_retail": money(godown["at_retail"]),
        })
    else:
        note = (f"{NO_DATES} The godown is left out because you have narrowed to one branch, and the godown is "
                "head office's stock rather than any branch's.")
    # Narrowing to head office reaches here with no branch row and no godown row, and saying only that the godown
    # was left out would read as though something else had been counted. Nothing was.
    if not rows:
        note = NOTHING_LIVE
    return table(
        "where", "Where the stock sits", None,
        [{"key": "label", "label": "Location"},
         {"key": "lines", "label": "Lines", "align": "right"},
         {"key": "holding", "label": "In stock", "align": "right"},
         {"key": "at_zero", "label": "At zero", "align": "right"},
         {"key": "negative", "label": "Negative", "align": "right"},
         {"key": "units", "label": "Units", "align": "right"},
         {"key": "at_cost", "label": "At cost", "align": "right"},
         {"key": "at_retail", "label": "At retail", "align": "right"}],
        rows, note=note)


async def build(page_id: str, scope: Scope) -> dict:
    live = await _live_snapshots()
    where, args = _stock_where(scope, live)

    if page_id == "overview":
        head = await _totals(where, args)
        at_cost, at_retail = ds._d(head.get("at_cost")), ds._d(head.get("at_retail"))
        lines = int(head.get("lines") or 0)
        cards = [
            card("Stock at Cost", money(at_cost), go=to_screen("branches"),
                 sub=f"the last picture from {int(head.get('branches') or 0)} branch(es), not these dates"),
            card("Stock at Retail", money(at_retail), go=to_screen("where")),
            card("Markup on the Shelf", money(at_retail - at_cost),
                 sub=f"{pct(at_retail - at_cost, at_cost)}% over cost"),
            card("Units on Hand", money(head.get("units")), kind="number", go=to_page("movement"),
                 sub=f"{int(head.get('skus') or 0):,} distinct Items"),
            card("Stock Lines", f"{lines}", kind="number",
                 sub=f"{int(head.get('negative') or 0):,} negative, {int(head.get('at_zero') or 0):,} at zero"),
            card("Value Below Nothing", money(head.get("negative_cost")), go=to_screen("negatives"),
                 sub="what the negative lines take off the total"),
        ]
        godown = await _godown(scope)
        if godown is not None:
            cards.append(card("Godown, at Retail", money(godown["at_retail"]),
                              sub="head office keeps no cost per Item, so cost cannot be shown"))

        branch_rows = await _by_branch(where, args)
        visuals = [
            # A ring: four branches holding one pile of stock between them is a share question, and four
            # slices is well inside what a ring can say at a glance.
            donut("branches", "Stock at cost, by branch", "branch", branch_rows,
                  note=NO_DATES if branch_rows else NOTHING_LIVE, value_key="at_cost"),
            _where_it_sits(branch_rows, godown),
        ]
        # Drill down rather than collapse: pinned to department and category, narrowing to one of them drew a
        # chart with a single bar and left the screen beside it empty. `ds.unpinned` hands back the two finest
        # levels still open, so inside a category this becomes brand and Item.
        for dimension in ds.unpinned(scope, ("department", "category"), 2):
            word = ds.LEVEL_WORD[dimension]
            rows = await _by_stock_dimension(where, args, dimension)
            visuals.append(bars(dimension, f"Stock at cost, by {word}", dimension, rows,
                                note=NO_DATES if rows else NOTHING_LIVE, value_key="at_cost"))

        negatives = await _negative_lines(where, args)
        visuals.append(table(
            "negatives", "Negative lines: stock the branch's own books put below nothing", "sku",
            [{"key": "label", "label": "Item"}, {"key": "code", "label": "Branch"},
             {"key": "department", "label": "Department"}, {"key": "category", "label": "Category"},
             {"key": "qty", "label": "Qty", "align": "right"},
             {"key": "at_cost", "label": "At cost", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]), "code": str(r["code"] or ""),
              "department": str(r["department"] or ""), "category": str(r["category"] or ""),
              "qty": money(r["qty"]), "at_cost": money(r["at_cost"])} for r in negatives],
            note=("A shelf reads below nothing when a sale was rung up before the delivery that supplied it was "
                  "entered, so each of these is a receipt nobody has posted. Shown as they stand, not clamped "
                  f"to zero. {NO_DATES}") if negatives else NOTHING_LIVE))
        # ITS OWN, NOT THE CONTROLLER'S FIVE. The fallback counts Items, categories and brands that SOLD in the
        # window, off 1.87 million product-days; this page is about what is on the shelf whatever the dates say,
        # so those five were both the wrong figures and a scan nobody asked for. These are already in hand.
        return {"cards": cards, "visuals": visuals, "covers": [
            {"label": "stock lines", "value": lines},
            {"label": "distinct Items", "value": int(head.get("skus") or 0)},
            {"label": "branches with a live count", "value": int(head.get("branches") or 0)},
        ]}

    if page_id == "movement":
        days = (scope.end - scope.start).days + 1
        head = await _totals(where, args)
        sold = await ds.headline(scope)
        dead = await _not_sold_totals(scope, where, args)
        on_hand, sold_units = ds._d(head.get("units")), sold["qty"]
        cards = [
            card("Not Sold in These Dates", money(dead.get("at_cost")), go=to_screen("dead"),
                 sub=f"{int(dead.get('lines') or 0):,} of {int(head.get('holding') or 0):,} lines holding stock"),
            card("Units Not Sold", money(dead.get("units")), kind="number", go=to_screen("dead")),
            card("Units Sold", money(sold_units), kind="number", sub=f"over {days} day(s)"),
            card("Sales in These Dates", money(sold["net"]), go=to_page("overview")),
            # Stock on hand against the rate it is leaving at. Rs 124.1M of stock against 411,977.871 units sold
            # in the 30 day test window comes out at 56 days, which is a figure a buyer can argue with.
            card("Days of Cover", f"{on_hand * days / sold_units:.0f}" if sold_units else "0", kind="number",
                 sub="stock on hand against units sold per day"),
        ]

        not_sold = await _not_sold(scope, where, args)
        visuals = [table(
            "dead", "Holding stock, nothing sold in these dates", "sku",
            [{"key": "label", "label": "Item"}, {"key": "code", "label": "Branch"},
             {"key": "department", "label": "Department"}, {"key": "category", "label": "Category"},
             {"key": "qty", "label": "On hand", "align": "right"},
             {"key": "at_cost", "label": "At cost", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]), "code": str(r["code"] or ""),
              "department": str(r["department"] or ""), "category": str(r["category"] or ""),
              "qty": money(r["qty"]), "at_cost": money(r["at_cost"])} for r in not_sold],
            note=("One line per branch per Item, because an Item dead at one branch and selling at another is a "
                  "transfer rather than dead stock. What is on hand is each branch's latest picture whatever "
                  "these dates say; whether it sold is these dates.") if not_sold else NOTHING_LIVE)]

        running = await _running_out(scope, where, args)
        visuals.append(table(
            "running_out", "Running out: fewest days of cover", "sku",
            [{"key": "label", "label": "Item"}, {"key": "code", "label": "Branch"},
             {"key": "department", "label": "Department"},
             {"key": "qty", "label": "On hand", "align": "right"},
             {"key": "units", "label": "Sold", "align": "right"},
             {"key": "per_day", "label": "Per day", "align": "right"},
             {"key": "cover", "label": "Days left", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]), "code": str(r["code"] or ""),
              "department": str(r["department"] or ""), "qty": money(r["qty"]), "units": money(r["units"]),
              "per_day": f"{ds._d(r['units']) / days:.1f}",
              "cover": f"{ds._d(r['cover']):.1f}"} for r in running],
            note=(f"Days left is what is on hand divided by units sold per day over the {days} day(s) above, "
                  "counting every day in the window rather than only the days that branch traded. Items with "
                  "nothing on hand are absent: they have already run out and are in the list below."
                  ) if running else NOTHING_LIVE))

        empty_shelf = await _sold_out(scope, where, args)
        visuals.append(table(
            "sold_out", "Sold in these dates and now at or below zero", "sku",
            [{"key": "label", "label": "Item"}, {"key": "code", "label": "Branch"},
             {"key": "department", "label": "Department"},
             {"key": "qty", "label": "On hand", "align": "right"},
             {"key": "units", "label": "Units sold", "align": "right"},
             {"key": "net", "label": "Sales", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]), "code": str(r["code"] or ""),
              "department": str(r["department"] or ""), "qty": money(r["qty"]), "units": money(r["units"]),
              "net": money(r["net"])} for r in empty_shelf],
            note=("Biggest seller first, because the question an empty shelf asks is what it is costing. A "
                  "negative figure here is the same unposted receipt the Overview counts."
                  ) if empty_shelf else NOTHING_LIVE))

        # Drill down rather than collapse, as on the overview page: these were pinned to department and
        # category, so a page narrowed to one of them drew a chart with a single bar in it.
        for dimension in ds.unpinned(scope, ("department", "category"), 2):
            rows = await _not_sold_by_dimension(scope, where, args, dimension)
            visuals.append(bars(f"dead_{dimension}", f"Not sold in these dates, by {ds.LEVEL_WORD[dimension]}",
                                dimension, rows, value_key="at_cost", note=None if rows else NOTHING_LIVE))
        # Its own, for the reason given on the overview page. This one already read `headline` for Units Sold,
        # so three of its counts are free here rather than a second scan of the same rollup.
        return {"cards": cards, "visuals": visuals, "covers": [
            {"label": "lines holding stock", "value": int(head.get("holding") or 0)},
            {"label": "of them sold nothing", "value": int(dead.get("lines") or 0)},
            {"label": "Items sold in the window", "value": sold["skus"]},
            {"label": "days in the window", "value": days},
        ]}

    raise KeyError(page_id)
