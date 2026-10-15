"""ABC Analysis: which Items earn the money, and which only take up shelf.

WHAT THE CLASSES MEAN
---------------------
Every Item is ranked by what it brought in over the window, and the running total decides its class:

    A   the Items that make the first 80% of revenue
    B   the next 15% (up to 95%)
    C   everything after that

That is the standard cut and it is deliberately NOT "the top 20% of Items". The whole point of the analysis is to
find out what share of the range earns what share of the money, so fixing the share of Items in advance would
answer the question before asking it.

THE CUT IS ON REVENUE, AND PROFIT IS SHOWN BESIDE IT
----------------------------------------------------
A class that sells a lot and earns nothing is the single most useful thing this can show, and it only shows if
the classes are cut one way and judged another. Technosys's own version of this report cut on revenue and then
put gross profit per class next to it, and their C class -- 26,199 Items, the long tail nobody defends -- still
produced 28% of their gross profit. Cutting on profit would have hidden that.

WHERE THE FIGURES COME FROM
---------------------------
`branch_product_stats`, one row per Item per branch per day, so an Item's class depends on the window and on
whatever has been clicked. Narrow to one branch and the classes are that branch's, which is the right answer: an
Item can be an A at Garden Town and a C at Gulgasht, and a single company-wide class would hide exactly that.

A NOTE ON RETURNS
-----------------
An Item whose returns outweigh its sales has negative revenue for the window. It sorts to the bottom and lands in
C, which is correct, but it means the running total can move backwards at the tail. The classes are unaffected --
the boundaries are crossed long before -- and the ranked table shows the negative as it is rather than hiding it.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import dialect
from app.dashboards._shape import bars, card, donut, money, pct, table
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "ABC Analysis",
    "blurb": "Which Items earn the money and which only take up shelf. Classed on revenue, judged on profit.",
    "resource": "executive.stock",
    "pages": [
        {"id": "overview", "label": "Classes"},
        {"id": "items", "label": "Ranked Items"},
    ],
}

A_CUT = Decimal("0.80")
B_CUT = Decimal("0.95")
CLASS_ORDER = {"A": 0, "B": 1, "C": 2}


def _ranked(scope: Scope) -> tuple[str, list]:
    """Every Item in the window with its running share, as nested derived tables.

    Window functions rather than a second pass in Python: the running total over 11,956 Items is the database's
    job, and pulling them all back to add them up here is how the executive folds used to take 34 seconds.

    DERIVED TABLES AND NOT A COMMON TABLE EXPRESSION, which is what this was written as first. SQL Server only
    accepts `WITH` at the start of a statement, so the moment a caller wrapped it -- `SELECT ... FROM (WITH ...)`
    -- it answered "Incorrect syntax near the keyword 'WITH'". Every caller here wraps it, because the whole
    point is to group the same ranking three different ways. Nested subqueries nest, on both engines.
    """
    where, args = scope.where()
    return f"""
        SELECT r.*, CASE WHEN r.grand <= 0 THEN 'C'
                         WHEN r.running <= r.grand * {A_CUT} THEN 'A'
                         WHEN r.running <= r.grand * {B_CUT} THEN 'B'
                         ELSE 'C' END AS cls
          FROM (
            SELECT t.*,
                   SUM(t.net) OVER (ORDER BY t.net DESC, t.sku
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running,
                   SUM(t.net) OVER () AS grand,
                   ROW_NUMBER() OVER (ORDER BY t.net DESC, t.sku) AS place
              FROM (
                SELECT s.product_sku AS sku, MIN(s.product_name) AS name,
                       MIN(s.department) AS department, MIN(s.category) AS category, MIN(s.brand) AS brand,
                       SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs, SUM(s.qty) AS qty
                  FROM branch_product_stats s WHERE {where}
                 GROUP BY s.product_sku
              ) t
          ) r
    """, args


async def classes(scope: Scope) -> list[dict]:
    sql, args = _ranked(scope)
    rows = await ds._rows(f"""
        SELECT cls, COUNT(*) AS items, SUM(net) AS net, SUM(cogs) AS cogs, SUM(qty) AS qty
          FROM ({sql}) x GROUP BY cls
    """, args)
    return sorted(rows, key=lambda r: CLASS_ORDER.get(str(r["cls"]), 9))


async def classes_by(scope: Scope, dimension: str, limit: int = 12) -> list[dict]:
    """How each department, category, brand or Item splits across the classes: where the dead range lives.

    `sku` is grouped by the code and LABELLED by the name, because the drill-down ends on Items and a chart of
    bare product codes is a chart nobody can read. The other three are their own label.
    """
    # THE RANKED SUBQUERY'S OWN COLUMN NAMES, not `branch_product_stats`'s. `_ranked` selects
    # `s.product_sku AS sku`, so asking it for `product_sku` is asking for a column that is not there, which is
    # what "Invalid column name 'product_sku'" was: drilling into a brand crashed the page outright.
    inner = {"sku": "sku"}.get(dimension) or (ds.DIMENSIONS.get(dimension) or "").replace("s.", "", 1)
    if not inner:
        return []
    label = "MIN(x.name)" if dimension == "sku" else f"x.{inner}"
    sql, args = _ranked(scope)
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} x.{inner} AS key_, {label} AS label,
               SUM(CASE WHEN x.cls = 'A' THEN 1 ELSE 0 END) AS a_items,
               SUM(CASE WHEN x.cls = 'B' THEN 1 ELSE 0 END) AS b_items,
               SUM(CASE WHEN x.cls = 'C' THEN 1 ELSE 0 END) AS c_items,
               COUNT(*) AS items, SUM(x.net) AS net, SUM(x.cogs) AS cogs, SUM(x.qty) AS qty
          FROM ({sql}) x
         WHERE x.{inner} IS NOT NULL AND x.{inner} <> ''
         GROUP BY x.{inner} ORDER BY SUM(x.net) DESC {dialect.limit(limit)}
    """, args)


async def ranked_items(scope: Scope, limit: int = 300) -> list[dict]:
    sql, args = _ranked(scope)
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} x.* FROM ({sql}) x ORDER BY x.place {dialect.limit(limit)}
    """, args)


async def build(page_id: str, scope: Scope) -> dict:
    rows = await classes(scope)
    by_class = {str(r["cls"]): r for r in rows}
    total_items = sum(int(r["items"] or 0) for r in rows)
    total_net = sum(ds._d(r["net"]) for r in rows)
    total_profit = sum(ds._d(r["net"]) - ds._d(r["cogs"]) for r in rows)

    def klass(name: str, field: str) -> Decimal:
        row = by_class.get(name)
        if not row:
            return ds._d(0)
        if field == "profit":
            return ds._d(row["net"]) - ds._d(row["cogs"])
        return ds._d(row[field])

    # Supplied, never left to the controller. Its fallback counts distinct Items, categories and brands over
    # 1.87 million product-days, measured at 13.70s on an all-time window against the 15 seconds the browser
    # waits. Everything in this strip is already in hand, so it costs nothing.
    covers = [
        {"label": "Items", "value": total_items},
        {"label": "classes", "value": len(rows)},
        {"label": "trading days in the window", "value": (scope.end - scope.start).days + 1},
    ]

    cards = [
        card("Items", str(total_items), kind="number"),
        card("Revenue", money(total_net)),
        card("Gross Profit", money(total_profit), sub=f"{pct(total_profit, total_net)}% margin"),
    ]
    for name in ("A", "B", "C"):
        items = int(by_class.get(name, {}).get("items") or 0)
        cards.append(card(
            f"Class {name}", money(klass(name, "net")),
            sub=f"{items} Item{'' if items == 1 else 's'}"
                f" ({pct(ds._d(items), ds._d(total_items))}% of range)"
                f" · {pct(klass(name, 'net'), total_net)}% of revenue"))

    visuals: list[dict] = []

    if page_id == "overview":
        # The headline comparison, and the reason the cut is on revenue. Three classes is a share-of-the-whole
        # question with three slices, which is exactly what a ring is for; the Item counts stay as bars beside
        # it, because the point of the pair is to read one against the other and two rings do not compare.
        visuals.append(donut("by_class_net", "Revenue by class", None, [
            {"key_": r["cls"], "label": f"Class {r['cls']}", "net": r["net"], "cogs": r["cogs"], "qty": r["qty"]}
            for r in rows]))
        visuals.append({
            "id": "by_class_items", "kind": "bars", "title": "How many Items are in each class",
            "dimension": None,
            "rows": [{"key": str(r["cls"]), "label": f"Class {r['cls']}",
                      "value": str(int(r["items"] or 0)),
                      "qty": money(r["qty"])} for r in rows],
        })
        visuals.append({
            "id": "by_class_profit", "kind": "bars", "title": "Gross profit by class", "dimension": None,
            "note": "Classed on revenue, judged on profit. A class that sells well and earns little only shows "
                    "up when the two are read side by side.",
            "rows": [{"key": str(r["cls"]), "label": f"Class {r['cls']}",
                      "value": money(ds._d(r["net"]) - ds._d(r["cogs"])),
                      "qty": money(r["qty"])} for r in rows],
        })

        # DRILL DOWN RATHER THAN COLLAPSE. These two were pinned to category and brand, so narrowing to one
        # category drew a chart with a single bar in it and left the half screen beside it blank. They now take
        # the two finest levels still open: category and brand to begin with, brand and Item inside a category,
        # and the Items alone inside a brand. `ds.unpinned` is the same rule on every dashboard.
        levels = ds.unpinned(scope, ("category", "brand"), 2)
        for level in levels:
            word = ds.LEVEL_WORD[level]
            visuals.append(bars(level, f"By {word}", level, await classes_by(scope, level, 12)))

        # The table tabulates the FINEST level on screen, so it is a further step down rather than a repeat of
        # the chart above it. Everything pinned and there is nothing left to tabulate; the ranked Items page is
        # where that reader is going anyway, so the table is left out rather than drawn with one row in it.
        if levels:
            deepest = levels[-1]
            word = ds.LEVEL_WORD[deepest]
            rows_deep = await classes_by(scope, deepest, 40 if deepest == "sku" else 14)
            visuals.append(table(
                "class_spread", f"Where the long tail sits, by {word}", deepest,
                [{"key": "label", "label": word.capitalize()},
                 {"key": "a", "label": "A", "align": "right"},
                 {"key": "b", "label": "B", "align": "right"},
                 {"key": "c", "label": "C", "align": "right"},
                 {"key": "items", "label": "Items", "align": "right"},
                 {"key": "value", "label": "Revenue", "align": "right"},
                 {"key": "share", "label": "% of revenue", "align": "right"}],
                [{"key": str(r["key_"] or ""), "label": str(r["label"] or "--"),
                  "a": str(int(r["a_items"] or 0)), "b": str(int(r["b_items"] or 0)),
                  "c": str(int(r["c_items"] or 0)), "items": str(int(r["items"] or 0)),
                  "value": money(r["net"]), "share": pct(ds._d(r["net"]), total_net)} for r in rows_deep]))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    if page_id == "items":
        items = await ranked_items(scope, 300)
        visuals.append(table(
            "ranked", "Every Item, best first", "sku",
            [{"key": "place", "label": "#", "align": "right"},
             {"key": "cls", "label": "Class"},
             {"key": "label", "label": "Item"},
             {"key": "category", "label": "Category"},
             {"key": "brand", "label": "Brand"},
             {"key": "qty", "label": "Qty", "align": "right"},
             {"key": "value", "label": "Revenue", "align": "right"},
             {"key": "profit", "label": "Profit", "align": "right"},
             {"key": "margin", "label": "Margin %", "align": "right"},
             {"key": "running", "label": "Cumulative %", "align": "right"}],
            [{
                "key": str(r["sku"]), "place": str(int(r["place"] or 0)), "cls": str(r["cls"]),
                "label": str(r["name"] or r["sku"]),
                "category": str(r["category"] or ""), "brand": str(r["brand"] or ""),
                "qty": money(r["qty"]), "value": money(r["net"]),
                "profit": money(ds._d(r["net"]) - ds._d(r["cogs"])),
                "margin": pct(ds._d(r["net"]) - ds._d(r["cogs"]), ds._d(r["net"])),
                "running": pct(ds._d(r["running"]), ds._d(r["grand"])),
            } for r in items],
            note="The first 300 by revenue. Cumulative % is what this Item and everything above it adds up to, "
                 "which is the line the classes are cut on."))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    raise KeyError(page_id)
