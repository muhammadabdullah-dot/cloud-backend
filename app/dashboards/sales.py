"""Sales Analysis: what sold, where, and what it made.

The page every other dashboard is modelled on. Its own note, worth keeping: the headline is taken from the same
product-days the charts are, so a figure at the top can never disagree with the chart beneath it.
"""
from __future__ import annotations

from app.dashboards._shape import bars, card, donut, money, to_page, to_screen
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Sales Analysis",
    "blurb": "What sold, where, and what it made. Click anything to narrow the whole page.",
    "resource": "executive.dashboard",
    "pages": [
        {"id": "overview", "label": "Overview"},
        {"id": "mix", "label": "Category & Brand Mix"},
    ],
}


def _covers(head: dict) -> list[dict]:
    """What this page covers, from the headline it already read."""
    return [
        {"label": "Items", "value": head["skus"]},
        {"label": "categories", "value": head["categories"]},
        {"label": "brands", "value": head["brands"]},
        {"label": "branches", "value": head["branches"]},
        {"label": "trading days", "value": head["days"]},
    ]


async def build(page_id: str, scope: Scope) -> dict:
    head = await ds.headline(scope)
    net, profit = head["net"], head["profit"]
    cards = [
        # Every figure here leads to the chart that takes it apart. The trend is on the overview and the
        # mix pages are where the same money is split by department, so the cards point at whichever of the
        # two the reader is not already looking at.
        card("Revenue", money(net), go=to_screen("trend") if page_id == "overview" else to_screen("department")),
        card("Gross Profit", money(profit), sub=f"{head['margin']:.1f}% margin",
             go=to_screen("trend") if page_id == "overview" else to_page("overview")),
        card("Cost of Sale", money(head["cogs"]), go=to_page("mix")),
        card("Items Sold", money(head["qty"]), kind="number", go=to_page("mix")),
        card("Per Trading Day", money(head["per_day"]), sub=f"over {head['days']} day(s)",
             go=to_screen("trend") if page_id == "overview" else to_page("overview")),
    ]
    visuals: list[dict] = []

    if page_id == "overview":
        bill = await ds.invoices(scope)
        cards += [
            # The hourly chart is the only thing on this dashboard about bills rather than Items, so the
            # three bill figures all lead to it.
            card("Invoices", str(bill["bills"]), kind="number",
                 sub="bills that contained this" if bill["narrowed"] else None, go=to_screen("hours")),
            card("Average Basket", money(bill["basket"]), go=to_screen("hours")),
            card("Discount", money(bill["disc"]), go=to_screen("hours")),
        ]
        days = await ds.trend(scope)
        visuals.append({
            "id": "trend", "kind": "line", "title": "Revenue and profit by day", "dimension": None,
            "rows": [{"label": str(r["label"])[:10], "value": money(ds._d(r["net"])),
                      "profit": money(ds._d(r["net"]) - ds._d(r["cogs"])),
                      "qty": money(ds._d(r["qty"]))} for r in days],
        })
        visuals.append(bars("branches", "By branch", "branch", await ds.branches(scope)))
        hourly = await ds.hours(scope)
        visuals.append({
            "id": "hours", "kind": "bars", "title": "By hour of the trading day", "dimension": None,
            "note": None if hourly else
                    "The hourly figures are kept per branch per hour with no Item in them, so this cannot follow "
                    "a category or a brand. Clear that filter to see it.",
            "rows": [{"key": str(r["label"]), "label": f"{int(r['label']):02d}:00",
                      "value": money(ds._d(r["net"])), "qty": str(r["invoices"])} for r in hourly],
        })
        visuals.append(bars("top", "Top Items", "sku", await ds.by_dimension(scope, "sku", 15)))
        return {"cards": cards, "visuals": visuals, "covers": _covers(head)}

    if page_id == "mix":
        # DRILL DOWN RATHER THAN COLLAPSE. These were pinned to department, category and brand, so narrowing to
        # one of them drew a chart with a single bar in it. They now take the three finest levels still open:
        # inside a category it is brand and Item, inside a brand it is the Items alone. `ds.unpinned` is the
        # same rule on every dashboard, and the Items table below is the bottom of it whatever is picked.
        levels = ds.unpinned(scope, ("department", "category", "brand"), 3)
        for level in levels:
            word = ds.LEVEL_WORD[level]
            rows_level = await ds.by_dimension(scope, level, 14)
            # A ring where the share IS the question and the slices are few. Departments are nine at most and
            # add up to the whole trade, which is the one chart on this page somebody reads as a split; the
            # hundreds of categories and brands stay as bars, where the names are written down the side.
            shape = donut if level == "department" and len(rows_level) <= 9 else bars
            visuals.append(shape(level, f"By {word}", level, rows_level))
        rows = await ds.table(scope, 200)
        visuals.append({
            "id": "items", "kind": "table", "title": "Items", "dimension": "sku",
            "columns": [
                {"key": "label", "label": "Item"}, {"key": "department", "label": "Department"},
                {"key": "category", "label": "Category"}, {"key": "brand", "label": "Brand"},
                {"key": "qty", "label": "Qty", "align": "right"},
                {"key": "value", "label": "Revenue", "align": "right"},
                {"key": "profit", "label": "Profit", "align": "right"},
                {"key": "margin", "label": "Margin %", "align": "right"},
            ],
            "rows": [{
                "key": str(r["sku"]), "label": str(r["name"] or r["sku"]),
                "department": str(r["department"] or ""), "category": str(r["category"] or ""),
                "brand": str(r["brand"] or ""),
                "qty": money(ds._d(r["qty"])), "value": money(ds._d(r["net"])),
                "profit": money(ds._d(r["net"]) - ds._d(r["cogs"])),
                "margin": f"{(ds._d(r['net']) - ds._d(r['cogs'])) / ds._d(r['net']) * 100:.1f}"
                          if ds._d(r["net"]) else "0.0",
            } for r in rows],
        })
        return {"cards": cards, "visuals": visuals, "covers": _covers(head)}

    raise KeyError(page_id)
