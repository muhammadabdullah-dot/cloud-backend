"""Each branch's stock, item by item, as head office sees it — and where it came from.

Reads the branch's latest complete stock picture (see snapshot_service), which the branch keeps up to
date between full pushes by sending just the items that moved. Everything here is read-only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.core import dialect
from app.models import Branch, BranchSnapshotRun, selling_branches

D0 = Decimal("0")

STATES = {
    "in-stock": "CAST(s.qty AS REAL) > 0",
    "out": "CAST(s.qty AS REAL) = 0",
    "negative": "CAST(s.qty AS REAL) < 0",
    "all": "1 = 1",
}
def origins() -> dict[str, str]:
    """Where the stock on hand came from. Built per call, like `sorts()`, because two entries read a JSON column and
    the two engines spell that differently."""
    return {
        "warehouse": "CAST(s.origin_warehouse AS REAL) > 0",
        "branches": "CAST(s.origin_branches AS REAL) > 0",
        "within": "CAST(s.origin_within AS REAL) > 0",
        # Ever received from the godown / another branch, whatever is left of it now.
        "ever-warehouse": f"{dialect.json_value('s.flows', '$.fromWarehouse')} IS NOT NULL",
        "ever-branches": f"{dialect.json_value('s.flows', '$.fromBranches')} IS NOT NULL",
    }
# The sort picker's orders and the table's column headings: what each sorts by, and which way it goes when the
# app doesn't say (the picker's own choice). Blank cells go last whichever way.
def sorts() -> dict[str, tuple[str, str]]:
    """The sort each column offers, built when it is asked for rather than at import.

    It used to be a module-level dict, which cannot work once the SQL in it depends on the engine: the dict is built
    while the module is imported, before there is a connection to ask, so every entry would be frozen to whichever
    engine the helper guesses with nothing to read. Four of these entries differ between the two engines, so the
    whole table is built per call. It is ten strings, not a query.
    """
    return {
        "moved": ("s.last_moved_at", "DESC"),
        "qty": ("CAST(s.qty AS REAL)", "DESC"),
        "value": ("CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL)", "DESC"),
        "sold": (f"COALESCE(CAST({dialect.json_value('s.flows', '$.sold')} AS REAL), 0)", "DESC"),
        "name": (dialect.nocase("s.product_name"), "ASC"),
        # Codes in number order, as the tables sort in the browser: an all-digit code is padded, so 200 comes before
        # 11050010.
        "code": (
            dialect.nocase(
                "CASE WHEN TRIM(COALESCE(s.product_sku, '')) = '' THEN NULL "
                f"WHEN {dialect.all_digits('s.product_sku')} "
                f"THEN {dialect.zero_pad('s.product_sku')} ELSE s.product_sku END"),
            "ASC",
        ),
        "warehouse": ("CAST(s.origin_warehouse AS REAL)", "DESC"),
        "branches": ("CAST(s.origin_branches AS REAL)", "DESC"),
        "within": ("CAST(s.origin_within AS REAL)", "DESC"),
        "lastIn": (dialect.json_value("s.last_in", "$.at"), "DESC"),
    }


def _order_by(sort: str, order: str | None) -> str:
    """Unknown keys fall back to Recently moved. Rows that tie keep a steady order by name, then code, so paging
    never shows an Item twice or skips one."""
    every = sorts()
    field, default = every.get(sort, every["moved"])
    direction = {"asc": "ASC", "desc": "DESC"}.get(order or "", default)
    # Blank cells last, written as a CASE rather than `(field) IS NULL`. SQLite treats the comparison as 0 or 1 and
    # sorts on it; SQL Server has no boolean value to sort and refuses the expression outright.
    return (f"CASE WHEN ({field}) IS NULL THEN 1 ELSE 0 END, {field} {direction}, "
            "s.product_name, s.product_sku")


async def _q(sql: str, params: list) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, params)


def _d(v) -> Decimal:
    if v is None or v == "":
        return D0
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return D0


def _money(v) -> str:
    return str(_d(v).quantize(Decimal("0.01")))


def _qty(v) -> str:
    text = format(_d(v).normalize(), "f")
    return "0" if text in ("-0", "") else text


def _json(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _current(branch_id: str) -> BranchSnapshotRun | None:
    return await BranchSnapshotRun.filter(branch_id=branch_id, status="complete").order_by("-completed_at").first()


def _item(r: dict) -> dict:
    return {
        "sku": r["product_sku"], "name": r["product_name"], "category": r["category"], "brand": r["brand"],
        "department": r["department"],
        "qty": _qty(r["qty"]), "avgCost": _money(r["avg_cost"]), "price": _money(r["price"]),
        "valueAtCost": _money(_d(r["qty"]) * _d(r["avg_cost"])),
        "origin": {"warehouse": _qty(r["origin_warehouse"]), "branches": _qty(r["origin_branches"]), "within": _qty(r["origin_within"])},
        "flows": {k: _qty(v) for k, v in (_json(r["flows"]) or {}).items()},
        "locations": _json(r["locations"]) or [],
        "lastIn": _json(r["last_in"]),
        "lastMovedAt": _dt(r["last_moved_at"]), "lastSoldAt": _dt(r["last_sold_at"]),
        "changedAt": _dt(r["changed_at"]),
    }


def totals_sql() -> str:
    """The figures under the stock table. A function for the same reason as `sorts()`: three lines read a JSON
    column, whose function has a different name on each engine."""
    return f"""
    COUNT(*) AS items,
    SUM(CASE WHEN CAST(s.qty AS REAL) > 0 THEN CAST(s.qty AS REAL) ELSE 0 END) AS units,
    SUM(CASE WHEN CAST(s.qty AS REAL) > 0 THEN CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL) ELSE 0 END) AS value_cost,
    SUM(CAST(s.origin_warehouse AS REAL)) AS o_warehouse,
    SUM(CAST(s.origin_branches AS REAL)) AS o_branches,
    SUM(CAST(s.origin_within AS REAL)) AS o_within,
    SUM(COALESCE(CAST({dialect.json_value('s.flows', '$.fromWarehouse')} AS REAL), 0)) AS in_warehouse,
    SUM(COALESCE(CAST({dialect.json_value('s.flows', '$.fromBranches')} AS REAL), 0)) AS in_branches,
    SUM(COALESCE(CAST({dialect.json_value('s.flows', '$.sold')} AS REAL), 0)) AS sold,
    SUM(CASE WHEN CAST(s.qty AS REAL) > 0 THEN 1 ELSE 0 END) AS in_stock,
    SUM(CASE WHEN CAST(s.qty AS REAL) < 0 THEN 1 ELSE 0 END) AS negative
"""


def _totals(r: dict) -> dict:
    """The figures under a branch's stock, and whether the origin split among them means anything.

    `origin` is three numbers saying how much of the shelf came from the godown, from another branch, and from the
    branch's own buying. They are worked out by `stock_provenance_service` on the BRANCH server, which watches
    movements as they happen. A branch with no server of its own -- all four of ours today, loaded from Multi-Tec
    by the nightly import -- has no such record: their stock report is a quantity and a cost per Item, and the
    payload the import builds carries no `origin` key at all, so `snapshot_service` stores the column default of
    zero on all 42,690 rows.

    Zero therefore means two different things, and the screen was reading the wrong one: it drew an empty bar and
    "Godown 0 - Other branches 0 - Own (branch) 0", which looks like stock that came from nowhere rather than
    provenance nobody recorded. `originKnown` is the difference. It is False when the three sum to nothing, which
    is also the right answer for a branch holding no stock: there is no split to show either way.
    """
    known = sum(float(r.get(f"o_{k}") or 0) for k in ("warehouse", "branches", "within")) > 0
    return {
        "originKnown": known,
        "items": int(r.get("items") or 0), "inStock": int(r.get("in_stock") or 0), "negative": int(r.get("negative") or 0),
        "units": _qty(round(r.get("units") or 0, 3)), "valueAtCost": _money(round(r.get("value_cost") or 0, 2)),
        "origin": {k: _qty(round(r.get(f"o_{k}") or 0, 3)) for k in ("warehouse", "branches", "within")},
        "receivedFromWarehouse": _qty(round(r.get("in_warehouse") or 0, 3)),
        "receivedFromBranches": _qty(round(r.get("in_branches") or 0, 3)),
        "sold": _qty(round(r.get("sold") or 0, 3)),
    }


async def overview() -> list[dict]:
    """One line per branch: how much it holds, what that stock is made of, and how fresh the picture is.

    Head office is absent on purpose: its stock is the godown, which has its own screens. See
    `selling_branches`."""
    out = []
    for branch in await selling_branches().order_by("code"):
        run = await _current(str(branch.id))
        entry = {"branchId": str(branch.id), "code": branch.code, "name": branch.name, "snapshotAt": None, "lastChangeAt": None, "totals": None}
        if run:
            rows = await _q(f"SELECT {totals_sql()} FROM branch_product_stock s WHERE s.branch_id = {dialect.mark()} AND s.snapshot_id = {dialect.mark('varchar(60)')}", [str(branch.id), run.snapshot_id])
            entry.update(snapshotAt=run.completed_at, lastChangeAt=run.last_change_at, totals=_totals(rows[0] if rows else {}))
        out.append(entry)
    return out


async def items(branch_id: str, *, q: str | None, state: str, origin: str | None, sort: str, limit: int, offset: int,
                order: str | None = None) -> dict | None:
    branch = await Branch.get_or_none(id=branch_id)
    if not branch:
        return None
    run = await _current(branch_id)
    base = {"branchId": branch_id, "code": branch.code, "name": branch.name}
    if not run:
        return {**base, "snapshotAt": None, "lastChangeAt": None, "items": [], "total": 0, "totals": _totals({})}
    where = [f"s.branch_id = {dialect.mark()}", f"s.snapshot_id = {dialect.mark('varchar(60)')}",
             STATES.get(state, STATES["in-stock"])]
    params: list = [branch_id, run.snapshot_id]
    every_origin = origins()
    if origin in every_origin:
        where.append(every_origin[origin])
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(s.product_sku LIKE ? OR s.product_name LIKE ? OR s.brand LIKE ? OR s.category LIKE ?)")
        params += [like, like, like, like]
    clause = " AND ".join(where)
    totals = (await _q(f"SELECT {totals_sql()} FROM branch_product_stock s WHERE {clause}", params))[0]
    paging, page_args = dialect.page(min(max(limit, 1), 500), max(offset, 0))
    rows = await _q(
        f"SELECT s.* FROM branch_product_stock s WHERE {clause} ORDER BY {_order_by(sort, order)} {paging}",
        params + page_args,
    )
    return {
        **base, "snapshotAt": run.completed_at, "lastChangeAt": run.last_change_at,
        "items": [_item(r) for r in rows], "total": int(totals.get("items") or 0), "totals": _totals(totals),
    }


async def item(branch_id: str, sku: str) -> dict | None:
    """One item at one branch, and how much of it every other branch holds."""
    run = await _current(branch_id)
    if not run:
        return None
    rows = await _q(f"SELECT s.* FROM branch_product_stock s WHERE s.branch_id = {dialect.mark()} AND s.snapshot_id = {dialect.mark('varchar(60)')} AND s.product_sku = {dialect.mark('varchar(60)')}",
                    [branch_id, run.snapshot_id, sku])
    if not rows:
        return None
    elsewhere = []
    for branch in await Branch.exclude(id=branch_id).order_by("code"):
        other = await _current(str(branch.id))
        if not other:
            continue
        found = await _q(
            f"SELECT qty, origin_warehouse, origin_branches, origin_within, last_moved_at FROM branch_product_stock "
            f"WHERE branch_id = {dialect.mark()} AND snapshot_id = {dialect.mark('varchar(60)')} "
            f"AND product_sku = {dialect.mark('varchar(60)')}",
            [str(branch.id), other.snapshot_id, sku],
        )
        if found:
            f = found[0]
            elsewhere.append({
                "branchId": str(branch.id), "code": branch.code, "name": branch.name, "qty": _qty(f["qty"]),
                "origin": {"warehouse": _qty(f["origin_warehouse"]), "branches": _qty(f["origin_branches"]), "within": _qty(f["origin_within"])},
                "lastMovedAt": _dt(f["last_moved_at"]),
            })
    return {**_item(rows[0]), "elsewhere": elsewhere}
