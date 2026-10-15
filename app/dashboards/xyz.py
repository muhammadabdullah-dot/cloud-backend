"""XYZ Analysis: which Items sell steadily, and which arrive in lumps.

WHAT IT IS, AND WHY IT SITS BESIDE ABC RATHER THAN INSIDE IT
------------------------------------------------------------
ABC asks how much an Item earns. XYZ asks how PREDICTABLE it is, and the two are independent: a staple can be a
C that sells three a day without fail, and a luxury line can be an A that sells nothing for a fortnight and then
forty at once. Buying for those two is a different job, which is why they are two dashboards and the interesting
screen is the one that crosses them.

    X   steady           coefficient of variation under 0.5
    Y   seasonal, lumpy  0.5 to 1.0
    Z   erratic          over 1.0, or sells on only a few days

The coefficient of variation is the standard deviation of daily quantity divided by the mean. It is used rather
than the raw standard deviation because it has no units: an Item selling 1,000 a day and one selling 3 a day can
be compared, which a standard deviation in units cannot do.

WHAT A CROSS OF THE TWO IS FOR
-------------------------------
    AX   the backbone. Never run out, order on a schedule.
    AZ   earns well and is unpredictable. The expensive one to get wrong in either direction.
    CX   steady and small. A candidate for a standing order and no thought.
    CZ   rare, erratic and cheap. The list to consider delisting.

DAYS WITH NO SALE COUNT AS ZERO, DELIBERATELY
----------------------------------------------
`branch_product_stats` only holds a row for a day an Item actually moved, so a naive standard deviation over the
rows present would measure only the days it sold and call a thing that sells once a month perfectly steady. The
gaps are what make it erratic, so the arithmetic below divides by the number of trading days in the window, not
by the number of rows found.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import dialect
from app.dashboards._shape import bars, card, money, pct, table
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "XYZ Analysis",
    "blurb": "Which Items sell steadily and which arrive in lumps, crossed with what they earn.",
    "resource": "executive.stock",
    "pages": [
        {"id": "overview", "label": "Steadiness"},
        {"id": "cross", "label": "ABC x XYZ"},
    ],
}

X_CUT = "0.5"
Y_CUT = "1.0"


def _variability(scope: Scope) -> tuple[str, list]:
    """Per Item: mean daily quantity, its spread, and the resulting class.

    The mean is over EVERY trading day in the window, including the ones with no row at all, which is what makes
    this measure steadiness rather than just the shape of the days it happened to sell. The sum of squares is
    done in SQL because pulling 11,956 Items back to average them in Python is the mistake the executive folds
    were once rewritten to stop making.
    """
    where, args = scope.where()
    # Its OWN alias and its own WHERE. Written first with the outer alias reused, which SQL Server read as a
    # correlated subquery referring to the outer `s.day` and refused: "invalid in the select list because it is
    # not contained in either an aggregate function or the GROUP BY clause". It is meant to be independent --
    # how many trading days the window holds, for everybody -- so it gets its own alias.
    days_where, days_args = scope.where("d")
    days = f"(SELECT COUNT(DISTINCT d.day) FROM branch_product_stats d WHERE {days_where})"
    return f"""
        SELECT t.*,
               CASE WHEN t.mean <= 0 THEN 'Z'
                    WHEN t.spread / t.mean < {X_CUT} THEN 'X'
                    WHEN t.spread / t.mean < {Y_CUT} THEN 'Y'
                    ELSE 'Z' END AS cls
          FROM (
            SELECT g.sku, g.name, g.category, g.brand, g.net, g.cogs, g.qty, g.sold_on, g.days,
                   g.qty / g.days AS mean,
                   -- Population standard deviation over every trading day, the zero days included: the second
                   -- moment less the square of the mean, floored at nothing so a rounding wobble cannot ask for
                   -- the root of a negative.
                   SQRT(CASE WHEN (g.sq / g.days) - ((g.qty / g.days) * (g.qty / g.days)) < 0 THEN 0
                             ELSE (g.sq / g.days) - ((g.qty / g.days) * (g.qty / g.days)) END) AS spread
              FROM (
                SELECT s.product_sku AS sku, MIN(s.product_name) AS name,
                       MIN(s.category) AS category, MIN(s.brand) AS brand,
                       SUM(s.net_sales) AS net, SUM(s.cogs) AS cogs,
                       SUM(s.qty) AS qty, SUM(s.qty * s.qty) AS sq,
                       COUNT(DISTINCT s.day) AS sold_on,
                       CAST({days} AS decimal(18,4)) AS days
                  FROM branch_product_stats s WHERE {where}
                 GROUP BY s.product_sku
              ) g
              WHERE g.days > 0
          ) t
    """, days_args + args


async def classes(scope: Scope) -> list[dict]:
    sql, args = _variability(scope)
    rows = await ds._rows(f"""
        SELECT cls, COUNT(*) AS items, SUM(net) AS net, SUM(cogs) AS cogs, SUM(qty) AS qty,
               AVG(sold_on) AS avg_days
          FROM ({sql}) x GROUP BY cls
    """, args)
    order = {"X": 0, "Y": 1, "Z": 2}
    return sorted(rows, key=lambda r: order.get(str(r["cls"]), 9))


async def steadiest(scope: Scope, cls: str, limit: int = 15, worst: bool = False) -> list[dict]:
    sql, args = _variability(scope)
    direction = "DESC" if worst else "ASC"
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} x.sku AS key_, x.name AS label, x.net, x.cogs, x.qty,
               x.sold_on, x.mean, x.spread
          FROM ({sql}) x WHERE x.cls = ? AND x.qty > 0
         ORDER BY x.spread / x.mean {direction} {dialect.limit(limit)}
    """, args + [cls])


async def cross(scope: Scope) -> list[dict]:
    """ABC against XYZ. The classes are cut here rather than imported so the two always agree about the window."""
    sql, args = _variability(scope)
    abc_where, abc_args = scope.where()
    abc = f"""
        SELECT r.sku, CASE WHEN r.grand <= 0 THEN 'C'
                           WHEN r.running <= r.grand * 0.80 THEN 'A'
                           WHEN r.running <= r.grand * 0.95 THEN 'B'
                           ELSE 'C' END AS abc
          FROM (
            SELECT t.sku, t.net,
                   SUM(t.net) OVER (ORDER BY t.net DESC, t.sku
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running,
                   SUM(t.net) OVER () AS grand
              FROM (SELECT s.product_sku AS sku, SUM(s.net_sales) AS net
                      FROM branch_product_stats s WHERE {abc_where}
                     GROUP BY s.product_sku) t
          ) r
    """
    return await ds._rows(f"""
        SELECT a.abc, x.cls AS xyz, COUNT(*) AS items, SUM(x.net) AS net, SUM(x.cogs) AS cogs
          FROM ({sql}) x JOIN ({abc}) a ON a.sku = x.sku
         GROUP BY a.abc, x.cls
    """, args + abc_args)


async def build(page_id: str, scope: Scope) -> dict:
    rows = await classes(scope)
    by = {str(r["cls"]): r for r in rows}
    total_items = sum(int(r["items"] or 0) for r in rows)
    total_net = sum(ds._d(r["net"]) for r in rows)

    # Supplied, never left to the controller. Its fallback counts distinct Items, categories and brands over
    # 1.87 million product-days, measured at 13.70s on an all-time window against the 15 seconds the browser
    # waits. Everything in this strip is already in hand, so it costs nothing.
    covers = [
        {"label": "Items", "value": total_items},
        {"label": "classes", "value": len(rows)},
        {"label": "trading days in the window", "value": (scope.end - scope.start).days + 1},
    ]

    cards = [card("Items", str(total_items), kind="number"), card("Revenue", money(total_net))]
    for name, what in (("X", "steady"), ("Y", "lumpy"), ("Z", "erratic")):
        row = by.get(name, {})
        items = int(row.get("items") or 0)
        cards.append(card(
            f"{name} -- {what}", str(items), kind="number",
            sub=f"{pct(ds._d(items), ds._d(total_items))}% of range · "
                f"{pct(ds._d(row.get('net')), total_net)}% of revenue"))

    visuals: list[dict] = []

    if page_id == "overview":
        visuals.append({
            "id": "by_class", "kind": "bars", "title": "How many Items are in each class", "dimension": None,
            "rows": [{"key": str(r["cls"]), "label": f"{r['cls']} -- "
                      + {"X": "steady", "Y": "lumpy", "Z": "erratic"}.get(str(r["cls"]), ""),
                      "value": str(int(r["items"] or 0)), "qty": money(r["qty"])} for r in rows],
        })
        visuals.append(bars("revenue_by_class", "Revenue by class", None, [
            {"key_": r["cls"], "label": f"Class {r['cls']}", "net": r["net"], "cogs": r["cogs"],
             "qty": r["qty"]} for r in rows]))
        visuals.append({
            "id": "days_sold", "kind": "bars", "title": "Days sold on, average", "dimension": None,
            "note": "Out of the trading days in this window. An erratic Item is usually one that sells on very "
                    "few of them, which is exactly what makes it hard to buy for.",
            "rows": [{"key": str(r["cls"]), "label": f"Class {r['cls']}",
                      "value": f"{ds._d(r['avg_days']):.1f}", "qty": ""} for r in rows],
        })
        steady = await steadiest(scope, "X", 15)
        visuals.append(table(
            "steady", "The steadiest sellers", "sku",
            [{"key": "label", "label": "Item"},
             {"key": "sold_on", "label": "Days sold", "align": "right"},
             {"key": "mean", "label": "Per day", "align": "right"},
             {"key": "spread", "label": "Spread", "align": "right"},
             {"key": "value", "label": "Revenue", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]),
              "sold_on": str(int(r["sold_on"] or 0)), "mean": f"{ds._d(r['mean']):.2f}",
              "spread": f"{ds._d(r['spread']):.2f}", "value": money(r["net"])} for r in steady],
            note="These are the Items to put on a standing order: they sell the same amount every day."))
        erratic = await steadiest(scope, "Z", 15, worst=True)
        visuals.append(table(
            "erratic", "The most erratic", "sku",
            [{"key": "label", "label": "Item"},
             {"key": "sold_on", "label": "Days sold", "align": "right"},
             {"key": "mean", "label": "Per day", "align": "right"},
             {"key": "spread", "label": "Spread", "align": "right"},
             {"key": "value", "label": "Revenue", "align": "right"}],
            [{"key": str(r["key_"]), "label": str(r["label"] or r["key_"]),
              "sold_on": str(int(r["sold_on"] or 0)), "mean": f"{ds._d(r['mean']):.2f}",
              "spread": f"{ds._d(r['spread']):.2f}", "value": money(r["net"])} for r in erratic],
            note="Sold on very few days, in lumps. Holding stock for these is what ties up money."))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    if page_id == "cross":
        grid = await cross(scope)
        cell = {(str(r["abc"]), str(r["xyz"])): r for r in grid}
        meaning = {
            ("A", "X"): "the backbone", ("A", "Y"): "earns well, seasonal", ("A", "Z"): "earns well, unpredictable",
            ("B", "X"): "steady middle", ("B", "Y"): "seasonal middle", ("B", "Z"): "awkward middle",
            ("C", "X"): "small and steady", ("C", "Y"): "small and seasonal", ("C", "Z"): "rare and erratic",
        }
        visuals.append(table(
            "grid", "Every Item, by what it earns and how steadily", None,
            [{"key": "label", "label": "Class"},
             {"key": "meaning", "label": "What it means"},
             {"key": "items", "label": "Items", "align": "right"},
             {"key": "value", "label": "Revenue", "align": "right"},
             {"key": "share", "label": "% of revenue", "align": "right"},
             {"key": "profit", "label": "Profit", "align": "right"}],
            [{
                "key": f"{a}{x}", "label": f"{a}{x}", "meaning": meaning[(a, x)],
                "items": str(int(cell.get((a, x), {}).get("items") or 0)),
                "value": money(cell.get((a, x), {}).get("net")),
                "share": pct(ds._d(cell.get((a, x), {}).get("net")), total_net),
                "profit": money(ds._d(cell.get((a, x), {}).get("net"))
                                - ds._d(cell.get((a, x), {}).get("cogs"))),
            } for a in ("A", "B", "C") for x in ("X", "Y", "Z")],
            note="AZ is the row to read first: it earns real money and cannot be predicted, so it is the one "
                 "that is expensive to get wrong in either direction."))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    raise KeyError(page_id)
