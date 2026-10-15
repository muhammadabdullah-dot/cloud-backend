"""Reports read off the bills head office now keeps.

Three of the old software's Sale Report entries needed something head office did not have until this week: every
bill with its lines, its party and its cost of goods. It has them now (models/branch_documents.py), so these are a
report rather than a rebuild.

    Invoice Wise Profit          what each bill made, and what it made it on
    Item Party Wise Sale Detail  which customer bought which Item, and how much of it
    Party Category Wise Sale     the same, rolled up to the category a customer belongs to

**These read bills, not figures.** `BranchProductStat` already answers "how much of this Item sold that day" and
the executive screens fold over it. What it cannot answer is *who bought it* and *what one bill made*, because a
day's total has no customer and no invoice number. That is the whole reason head office started keeping the
documents, and these are the first reports that needed them.

**Profit here is net of the discount and before tax**, which is what the branch's own `net_value` and `cogs` are:
the money the shop kept less what the goods cost it on the day. It is not the profit an income statement shows,
which carries overheads as well. Naming them the same thing would invite somebody to reconcile two figures that
were never meant to agree.

**A book of HO has no bills.** Head office does not sell; it buys and it ships to branches. A report asked for head
office alone comes back empty, and says so rather than looking broken.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core import dialect
from app.models import HEAD_OFFICE_BOOK
from app.services.accounts_chart_service import money
from app.services.accounts_reports_service import _d, _query, _s

ZERO = Decimal("0")

# How a bill's takings are grouped when somebody asks for it by something other than the customer.
GROUPS = ("party", "category", "cashier", "day", "counter")

# What a sale with no party code is called. One constant, because two reports show it and a shop reading both should
# not have to work out whether "Cash customer" and "Cash customers" are the same thing.
CASH_LABEL = "Cash customers"


class BillReportError(Exception):
    def __init__(self, message: str):
        self.message = message


def _when(start, end) -> tuple[str, list]:
    """The date-or-time condition on a bill, and the parameters it binds.

    A bill carries both: `day` is its trading day and `at` is the moment it was rung. These reports filtered on
    `day` alone, so the time on 1,066,286 of 1,066,299 bills was stored and never used.

    With no time asked for, the filter stays on `day`. That is not only for compatibility: `day` is what the index
    is on, and a whole-day range asked as instants would have to reason about the shop's start hour, which `day`
    already settles. A time is only honoured when one was actually chosen, and then `at` is the only column that
    can answer it.

    Both ends are inclusive either way. A bare date means the whole of that day, as it always did; a date with a
    time means exactly that moment, so whoever is asking decides where their day starts and ends.
    """
    from app.core import when

    if when.has_time(start) or when.has_time(end):
        return ("s.at >= ? AND s.at <= ?", [when.instant(start).isoformat(sep=" "),
                                            when.instant(end).isoformat(sep=" ")])
    return ("s.day >= ? AND s.day <= ?", [when.day_of(start).isoformat(), when.day_of(end).isoformat()])


async def branches_for(books: list[str]) -> list[dict]:
    """The branches these books name. Head office is left out: it keeps no bills of its own."""
    from app.models import Branch

    codes = [b for b in books if b != HEAD_OFFICE_BOOK]
    if not codes:
        return []
    return [{"id": str(b.id), "code": b.code, "name": b.name}
            for b in await Branch.filter(code__in=codes).order_by("code")]


def _empty(reason: str, **extra) -> dict:
    return {"rows": [], "count": 0, "note": reason, "totals": {}, **extra}


def _margin(profit: Decimal, sales: Decimal) -> str:
    """Profit as a percentage of what was sold. Blank rather than zero when nothing was sold, because 0% and
    "nothing happened" are different answers and a column of zeroes hides the difference."""
    if sales == ZERO:
        return ""
    return f"{(profit / sales * 100).quantize(Decimal('0.01'))}"


async def _party_categories(branch_ids: list[str], codes: list[str]) -> dict[tuple[str, str], str]:
    """Each customer's category, off the account that stands for them at head office.

    A branch's customer has no record of its own here; what head office holds is the account, carrying the party's
    own details as the branch sent them (models/accounts.py). Keyed by branch and party code, because two branches
    can each have a customer code 001 and they are not the same person.
    """
    from app.models import Account, Branch

    if not codes:
        return {}
    books = {str(b.id): b.code for b in await Branch.filter(id__in=branch_ids)}
    out: dict[tuple[str, str], str] = {}
    for chunk_start in range(0, len(codes), 400):
        chunk = codes[chunk_start:chunk_start + 400]
        for a in await Account.filter(book__in=list(books.values()), kind="customer", party_code__in=chunk).values(
            "book", "party_code", "party_category"
        ):
            for branch_id, code in books.items():
                if code == a["book"]:
                    out[(branch_id, a["party_code"])] = a["party_category"] or ""
    return out


# ── what each bill made ──────────────────────────────────────────────────────────────────────────

async def invoice_profit(books: list[str], start: date, end: date, min_margin: str | None = None,
                         max_margin: str | None = None, party_code: str | None = None,
                         limit: int = 500, offset: int = 0) -> dict:
    """Their Invoice Wise Profit: one line per bill, what it sold for and what those goods cost.

    The margin filters are the reason anybody opens this: not to read 4,000 bills but to find the handful sold at or
    below cost. Both ends, because a margin far above the usual is as worth a look as one below it.
    """
    branches = await branches_for(books)
    if not branches:
        return _empty("Head office keeps no bills of its own. Pick a branch, or the whole company.",
                      start=start.isoformat(), end=end.isoformat())
    ids = [b["id"] for b in branches]
    names = {b["id"]: b["code"] for b in branches}
    when_sql, when_params = _when(start, end)
    where = ["s.voided = 0", f"s.branch_id IN ({dialect.marks(ids)})", when_sql]
    params: list = [*ids, *when_params]
    if party_code:
        where.append(f"s.party_code = {dialect.mark('varchar(40)')}")
        params.append(party_code)
    rows = await _query(
        "SELECT s.id AS id, s.branch_id AS branch_id, s.invoice_number AS invoice_number, s.day AS day, s.at AS at, "
        "s.cashier_name AS cashier_name, s.party_name AS party_name, s.party_code AS party_code, "
        "s.counter_name AS counter_name, s.gross AS gross, s.disc_total AS disc_total, s.gst AS gst, "
        "s.net_value AS net_value, s.cogs AS cogs, s.is_credit_sale AS is_credit_sale "
        f"FROM branch_sales s WHERE {' AND '.join(where)} ORDER BY s.day, s.invoice_number", params,
    )

    lo = Decimal(min_margin) if min_margin not in (None, "") else None
    hi = Decimal(max_margin) if max_margin not in (None, "") else None
    out = []
    totals = {"sales": ZERO, "cogs": ZERO, "profit": ZERO, "discount": ZERO}
    for r in rows:
        sales, cost = money(_d(r["net_value"])), money(_d(r["cogs"]))
        profit = sales - cost
        pct = (profit / sales * 100) if sales else ZERO
        if (lo is not None and pct < lo) or (hi is not None and pct > hi):
            continue
        out.append({
            "saleId": str(r["id"]), "branch": names.get(str(r["branch_id"]), ""),
            "invoiceNumber": r["invoice_number"], "day": str(r["day"])[:10], "at": str(r["at"]),
            "cashierName": r["cashier_name"], "partyName": r["party_name"], "partyCode": r["party_code"],
            "counterName": r["counter_name"],
            "gross": _s(money(_d(r["gross"]))), "discount": _s(money(_d(r["disc_total"]))),
            "gst": _s(money(_d(r["gst"]))), "sales": _s(sales), "cogs": _s(cost), "profit": _s(profit),
            "margin": _margin(profit, sales), "onCredit": bool(r["is_credit_sale"]),
        })
        totals["sales"] += sales
        totals["cogs"] += cost
        totals["profit"] += profit
        totals["discount"] += money(_d(r["disc_total"]))
    return {
        "start": start.isoformat(), "end": end.isoformat(), "count": len(out),
        "rows": out[offset:offset + limit],
        "totals": {**{k: _s(v) for k, v in totals.items()},
                   "margin": _margin(totals["profit"], totals["sales"])},
    }


# ── who bought what ──────────────────────────────────────────────────────────────────────────────

async def item_party_sales(books: list[str], start: date, end: date, party_code: str | None = None,
                           sku: str | None = None, by: str = "party", limit: int = 1000) -> dict:
    """Their Item Party Wise Sale Detail: which customer bought which Item, and how much of it.

    `by` decides which way round it reads. Grouped by party, it is a customer's buying list; grouped by Item, it is
    who buys that Item, which is the question a shop asks before it stops stocking something.

    A walk-in bill has no party. Those are kept under one clearly named row rather than dropped, because a report
    that quietly loses most of a shop's sales is worse than one that admits they were cash customers.
    """
    if by not in ("party", "item"):
        raise BillReportError("Group by party or by item.")
    branches = await branches_for(books)
    if not branches:
        return _empty("Head office keeps no bills of its own. Pick a branch, or the whole company.",
                      start=start.isoformat(), end=end.isoformat(), by=by)
    ids = [b["id"] for b in branches]
    names = {b["id"]: b["code"] for b in branches}
    when_sql, when_params = _when(start, end)
    where = ["s.voided = 0", "l.is_return = 0", f"s.branch_id IN ({dialect.marks(ids)})", when_sql]
    params: list = [*ids, *when_params]
    if party_code:
        where.append(f"s.party_code = {dialect.mark('varchar(40)')}")
        params.append(party_code)
    if sku:
        where.append(f"l.product_sku = {dialect.mark('varchar(60)')}")
        params.append(sku)
    rows = await _query(
        "SELECT s.branch_id AS branch_id, s.party_code AS party_code, s.party_name AS party_name, "
        "l.product_sku AS sku, l.product_name AS product_name, l.department AS department, "
        "COUNT(DISTINCT s.id) AS invoices, SUM(l.qty) AS qty, "
        "SUM(l.qty * l.unit_price - l.disc_amount) AS sales, SUM(l.qty * l.unit_cost) AS cogs "
        f"FROM branch_sale_lines l JOIN branch_sales s ON s.id = l.sale_id WHERE {' AND '.join(where)} "
        "GROUP BY s.branch_id, s.party_code, s.party_name, l.product_sku, l.product_name, l.department", params,
    )

    groups: dict[str, dict] = {}
    totals = {"qty": ZERO, "sales": ZERO, "cogs": ZERO, "profit": ZERO}
    for r in rows:
        branch = names.get(str(r["branch_id"]), "")
        code = r["party_code"] or ""
        # A sale with no party code is a cash sale, whatever the bill happens to call the party. A shop's walk-in
        # party is often a real record with a name ("Walk-in Party"), so grouping on the name would scatter the cash
        # takings across however many such names exist, and the other report here keys on the code. One rule.
        party = (r["party_name"] or "").strip() if code else ""
        sales, cost = money(_d(r["sales"])), money(_d(r["cogs"]))
        key = (f"{branch}:{code}" if code else f"{branch}:cash") if by == "party" else r["sku"]
        label = (
            (f"{party or code} ({code})" if code else CASH_LABEL) if by == "party"
            else f"{r['sku']} {r['product_name'] or ''}".strip()
        )
        group = groups.setdefault(key, {"key": key, "label": label, "branch": branch, "rows": [],
                                        "qty": ZERO, "sales": ZERO, "cogs": ZERO})
        group["rows"].append({
            "branch": branch, "partyCode": code, "partyName": party or CASH_LABEL,
            "sku": r["sku"], "productName": r["product_name"], "department": r["department"],
            "invoices": int(r["invoices"] or 0), "qty": _s(money(_d(r["qty"]))),
            "sales": _s(sales), "cogs": _s(cost), "profit": _s(sales - cost), "margin": _margin(sales - cost, sales),
        })
        for field, value in (("qty", money(_d(r["qty"]))), ("sales", sales), ("cogs", cost)):
            group[field] += value
        totals["qty"] += money(_d(r["qty"]))
        totals["sales"] += sales
        totals["cogs"] += cost
    totals["profit"] = totals["sales"] - totals["cogs"]

    ordered = sorted(groups.values(), key=lambda g: -g["sales"])[:limit]
    for group in ordered:
        group["rows"].sort(key=lambda r: -Decimal(r["sales"]))
        profit = group["sales"] - group["cogs"]
        group["totals"] = {
            "qty": _s(group["qty"]), "sales": _s(group["sales"]), "cogs": _s(group["cogs"]),
            "profit": _s(profit), "margin": _margin(profit, group["sales"]),
        }
        # The running Decimals were only for adding up; from here the group carries its figures under `totals`.
        for field in ("qty", "sales", "cogs"):
            group.pop(field)
    return {
        "start": start.isoformat(), "end": end.isoformat(), "by": by, "groups": ordered,
        "count": sum(len(g["rows"]) for g in ordered),
        "totals": {**{k: _s(v) for k, v in totals.items()},
                   "margin": _margin(totals["profit"], totals["sales"])},
    }


# ── by the category a customer belongs to ────────────────────────────────────────────────────────

async def party_category_sales(books: list[str], start: date, end: date) -> dict:
    """Their Party Category Wise Sale: takings rolled up to the category each customer belongs to.

    The category lives on the account head office keeps for that customer, which only began carrying it this week.
    A customer with no category, and every cash sale, land under clearly named rows rather than being dropped.
    """
    branches = await branches_for(books)
    if not branches:
        return _empty("Head office keeps no bills of its own. Pick a branch, or the whole company.",
                      start=start.isoformat(), end=end.isoformat())
    ids = [b["id"] for b in branches]
    names = {b["id"]: b["code"] for b in branches}
    rows = await _query(
        "SELECT s.branch_id AS branch_id, s.party_code AS party_code, s.party_name AS party_name, "
        "COUNT(*) AS invoices, SUM(s.net_value) AS sales, SUM(s.cogs) AS cogs, SUM(s.disc_total) AS discount "
        f"FROM branch_sales s WHERE s.voided = 0 AND s.branch_id IN ({dialect.marks(ids)}) "
        f"AND {_when(start, end)[0]} GROUP BY s.branch_id, s.party_code, s.party_name",
        [*ids, *_when(start, end)[1]],
    )
    categories = await _party_categories(ids, sorted({r["party_code"] for r in rows if r["party_code"]}))

    buckets: dict[str, dict] = {}
    totals = {"invoices": 0, "sales": ZERO, "cogs": ZERO, "profit": ZERO, "discount": ZERO}
    for r in rows:
        code = r["party_code"]
        label = (CASH_LABEL if not code
                 else categories.get((str(r["branch_id"]), code)) or "No category")
        sales, cost = money(_d(r["sales"])), money(_d(r["cogs"]))
        bucket = buckets.setdefault(label, {"label": label, "invoices": 0, "parties": set(),
                                            "sales": ZERO, "cogs": ZERO, "discount": ZERO})
        bucket["invoices"] += int(r["invoices"] or 0)
        bucket["parties"].add(f"{names.get(str(r['branch_id']), '')}:{code or 'cash'}")
        bucket["sales"] += sales
        bucket["cogs"] += cost
        bucket["discount"] += money(_d(r["discount"]))
        totals["invoices"] += int(r["invoices"] or 0)
        totals["sales"] += sales
        totals["cogs"] += cost
        totals["discount"] += money(_d(r["discount"]))
    totals["profit"] = totals["sales"] - totals["cogs"]

    out = []
    for bucket in sorted(buckets.values(), key=lambda b: -b["sales"]):
        profit = bucket["sales"] - bucket["cogs"]
        out.append({
            "label": bucket["label"], "invoices": bucket["invoices"], "parties": len(bucket["parties"]),
            "sales": _s(bucket["sales"]), "cogs": _s(bucket["cogs"]), "discount": _s(bucket["discount"]),
            "profit": _s(profit), "margin": _margin(profit, bucket["sales"]),
            "share": _margin(bucket["sales"], totals["sales"]),
        })
    return {
        "start": start.isoformat(), "end": end.isoformat(), "rows": out, "count": len(out),
        "totals": {"invoices": totals["invoices"], **{k: _s(v) for k, v in totals.items() if k != "invoices"},
                   "margin": _margin(totals["profit"], totals["sales"])},
    }
