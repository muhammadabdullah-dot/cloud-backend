"""Trade Accounts: the named buyers, and how much of the trade is nobody in particular.

WHY THIS IS NOT A CUSTOMER RETENTION DASHBOARD
----------------------------------------------
The report this was modelled on counts 310,667 customers, splits them into new, churned and retained, and charts
revenue by the year each was acquired. None of that can be done here, and the reason is worth stating plainly
rather than drawing an empty version of it.

Measured over all 1,069,437 bills:

    1,059,177 bills (99.0%)   "CASH SALE WALKING CUSTOMER" -- one shared party row
    59                        distinct party codes in the whole history
    58                        distinct member codes, which mirror the party and are not a loyalty scheme:
                              00000-L00000 appears on all 1,059,177 walk-in bills

So there are 59 identities, not 310,667, and 99% of trade is anonymous by design: head office holds the party
NAME on a bill and no customer master of its own. A retention rate over 59 accounts would be a number with no
meaning, and a cohort chart would be a straight line.

What those 59 ARE is worth a dashboard: they are the trade and aggregator accounts -- Food Panda at 8,300 bills,
Kenzo AG Group at Rs 19.5M over 46, TCS Website, corporate buyers. That is a real channel, nobody has a screen
for it, and it is what this draws.

If customer-level analysis is wanted later, the branch is where identity lives; it would have to travel up.
"""
from __future__ import annotations

from app.core import dialect
from app.dashboards._shape import bars, card, money, pct, table, to_page, to_screen
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Trade Accounts",
    "blurb": "The named buyers behind the walk-in trade: who they are, what they take, and how often.",
    "resource": "executive.dashboard",
    "pages": [
        {"id": "overview", "label": "Accounts"},
        {"id": "credit", "label": "Credit Sales"},
    ],
}

# Their walk-in party. Everything that is not this is somebody with a name.
WALK_IN = "CASH SALE WALKING CUSTOMER"


async def split(scope: Scope) -> dict:
    """Named trade against anonymous counter trade, which is the first thing anybody should see here."""
    where, args = scope.where("b", only=("branch",))
    row = (await ds._rows(f"""
        SELECT COUNT(*) AS bills, SUM(b.net_value) AS net,
               SUM(CASE WHEN b.party_name = ? THEN 1 ELSE 0 END) AS walk_bills,
               SUM(CASE WHEN b.party_name = ? THEN b.net_value ELSE 0 END) AS walk_net,
               COUNT(DISTINCT CASE WHEN b.party_name <> ? THEN b.party_code END) AS accounts,
               SUM(CASE WHEN b.is_credit_sale = 1 THEN 1 ELSE 0 END) AS credit_bills,
               SUM(CASE WHEN b.is_credit_sale = 1 THEN b.net_value ELSE 0 END) AS credit_net
          FROM branch_sales b WHERE {where}
    """, [WALK_IN, WALK_IN, WALK_IN] + args))[0]
    bills, net = int(row["bills"] or 0), ds._d(row["net"])
    walk_bills, walk_net = int(row["walk_bills"] or 0), ds._d(row["walk_net"])
    return {
        "bills": bills, "net": net,
        "walk_bills": walk_bills, "walk_net": walk_net,
        "named_bills": bills - walk_bills, "named_net": net - walk_net,
        "accounts": int(row["accounts"] or 0),
        "credit_bills": int(row["credit_bills"] or 0), "credit_net": ds._d(row["credit_net"]),
    }


async def accounts(scope: Scope, limit: int = 25, credit_only: bool = False) -> list[dict]:
    """Each named account, biggest first, with how often they buy and what a visit is worth."""
    where, args = scope.where("b", only=("branch",))
    extra = " AND b.is_credit_sale = 1" if credit_only else ""
    return await ds._rows(f"""
        SELECT {dialect.top(limit)} b.party_code AS key_, MIN(b.party_name) AS label,
               COUNT(*) AS bills, SUM(b.net_value) AS net, SUM(b.cogs) AS cogs,
               MIN(b.day) AS first_day, MAX(b.day) AS last_day,
               COUNT(DISTINCT b.day) AS days, COUNT(DISTINCT b.branch_id) AS branches
          FROM branch_sales b
         WHERE {where} AND b.party_name <> ?{extra}
         GROUP BY b.party_code ORDER BY SUM(b.net_value) DESC {dialect.limit(limit)}
    """, args + [WALK_IN])


async def by_day(scope: Scope, named_only: bool = True) -> list[dict]:
    where, args = scope.where("b", only=("branch",))
    extra = " AND b.party_name <> ?" if named_only else ""
    return await ds._rows(f"""
        SELECT b.day AS label, SUM(b.net_value) AS net, COUNT(*) AS bills
          FROM branch_sales b WHERE {where}{extra}
         GROUP BY b.day ORDER BY b.day
    """, args + ([WALK_IN] if named_only else []))


async def build(page_id: str, scope: Scope) -> dict:
    s = await split(scope)
    # ITS OWN, NOT THE CONTROLLER'S FIVE. Left out, the strip above this page read "11,661 Items, 34 categories,
    # 804 brands" over a page about seven named buyers, because the fallback counts the product rollup. These are
    # what this page actually counted, they are already in hand, and they cost nothing.
    covers = [
        {"label": "named accounts", "value": s["accounts"]},
        {"label": "of their bills", "value": s["named_bills"]},
        {"label": "counter bills beside them", "value": s["walk_bills"]},
        {"label": "days in the window", "value": (scope.end - scope.start).days + 1},
    ]
    cards = [
        card("Named Accounts", str(s["accounts"]), kind="number", go=to_screen("accounts"),
             sub="everyone else is the counter"),
        card("Their Revenue", money(s["named_net"]), go=to_screen("accounts"),
             sub=f"{pct(s['named_net'], s['net'])}% of all trade"),
        card("Their Bills", str(s["named_bills"]), kind="number", go=to_screen("frequency"),
             sub=f"{pct(ds._d(s['named_bills']), ds._d(s['bills']))}% of all bills"),
        card("Average Bill", money(s["named_net"] / s["named_bills"] if s["named_bills"] else 0),
             sub="against " + money(s["walk_net"] / s["walk_bills"] if s["walk_bills"] else 0)
                 + " at the counter"),
        card("Counter Trade", money(s["walk_net"]),
             sub=f"{s['walk_bills']:,} bills, nobody named"),
        card("On Credit", money(s["credit_net"]), sub=f"{s['credit_bills']:,} bills"),
    ]
    visuals: list[dict] = []

    if page_id == "overview":
        rows = await accounts(scope, 25)
        # Not clickable: a party is not one of the dimensions the rest of the dashboards narrow by, because the
        # product-day rollup every other visual reads has no party on it. Saying nothing and quietly doing
        # nothing on a click would be worse than not offering it.
        visuals.append(bars("accounts", "Trade accounts by revenue", None, rows))
        visuals.append({
            "id": "frequency", "kind": "bars", "title": "How often they buy", "dimension": None,
            "note": "Bills in the window. An aggregator like Food Panda buys daily; a corporate account buys "
                    "a few times a year for far more each time.",
            "rows": [{"key": str(r["key_"] or ""), "label": str(r["label"] or "--"),
                      "value": str(int(r["bills"] or 0)),
                      "qty": money(r["net"])} for r in rows],
        })
        visuals.append({
            "id": "named_trend", "kind": "line", "title": "Named trade by day", "dimension": None,
            "rows": [{"label": str(r["label"])[:10], "value": money(r["net"])}
                     for r in await by_day(scope, named_only=True)],
        })
        visuals.append(table(
            "accounts_table", "Every named account", None,
            [{"key": "label", "label": "Account"},
             {"key": "bills", "label": "Bills", "align": "right"},
             {"key": "value", "label": "Revenue", "align": "right"},
             {"key": "avg", "label": "Average bill", "align": "right"},
             {"key": "profit", "label": "Profit", "align": "right"},
             {"key": "days", "label": "Days active", "align": "right"},
             {"key": "branches", "label": "Branches", "align": "right"},
             {"key": "last", "label": "Last bought"}],
            [{
                "key": str(r["key_"] or ""), "label": str(r["label"] or "--"),
                "bills": f"{int(r['bills'] or 0):,}",
                "value": money(r["net"]),
                "avg": money(ds._d(r["net"]) / int(r["bills"]) if r["bills"] else 0),
                "profit": money(ds._d(r["net"]) - ds._d(r["cogs"])),
                "days": str(int(r["days"] or 0)),
                "branches": str(int(r["branches"] or 0)),
                "last": str(r["last_day"])[:10],
            } for r in rows]))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    if page_id == "credit":
        rows = await accounts(scope, 25, credit_only=True)
        visuals.append(bars("credit_accounts", "Who buys on credit", None, rows,
                            note=None if rows else
                            "No credit sales in this window. Only 9,964 of 1,069,437 bills in the whole history "
                            "were on credit, so a short window often has none."))
        visuals.append(table(
            "credit_table", "Credit sales by account", None,
            [{"key": "label", "label": "Account"},
             {"key": "bills", "label": "Bills", "align": "right"},
             {"key": "value", "label": "On credit", "align": "right"},
             {"key": "first", "label": "First"},
             {"key": "last", "label": "Last"}],
            [{"key": str(r["key_"] or ""), "label": str(r["label"] or "--"),
              "bills": f"{int(r['bills'] or 0):,}", "value": money(r["net"]),
              "first": str(r["first_day"])[:10], "last": str(r["last_day"])[:10]} for r in rows]))
        return {"cards": cards, "visuals": visuals, "covers": covers}

    raise KeyError(page_id)
