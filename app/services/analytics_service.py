"""Inventory classification — ABC, XYZ, dead stock and movement bands.

These four answer different questions and are routinely confused with each other, so:

* **ABC** ranks by *money*. Which items earn the revenue. Classic Pareto: A is the handful of lines
  carrying most of the turnover, C is the long tail. It says where to spend attention.
* **XYZ** ranks by *predictability*. Whether demand is steady, seasonal or erratic. It says how
  much safety stock a line needs — an A item you cannot forecast is a harder problem than a C item
  you can.
* **Dead stock** is about *time since last sale*, not rank. An item can be C-class and healthy, or
  C-class and money that has been sitting on a shelf since last year. Only the second is dead.
* **Movement bands** are about *velocity* — units per trading day — which is what a shelf-space or
  reorder conversation actually runs on.

Together they are the standard 9-box (AX through CZ), and each cut is computed from a different
column on purpose. Collapsing them into one "product score" is how you end up unable to explain why
an item is flagged.

**On honesty with thin data.** XYZ needs a demand *series*. Given a fortnight of trading, almost
every line is statistically erratic, and reporting that as "these items have unpredictable demand"
would be reading noise as a finding. So demand is counted in buckets sized to the period (days, weeks
or months), an Item needs eight of them before it gets a class at all, and short of that it is said to
be too new to tell. A classification that quietly degrades into nonsense as data thins is worse than
one that refuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.models import Branch, BranchSnapshotRun
from app.services.executive_service import months_back

D0 = Decimal("0")

# Pareto, in the form nearly every ERP and textbook uses, with the cut read from the top: walking the
# Items from the largest down, an Item is A while the Items above it carry less than 80% of the total,
# B while they carry less than 95%, and C after that. Reading the share BEFORE the Item means the Item
# that crosses 80% is still A (it is the one that got the total there), and the largest Item is always
# A however big it is. Fixed rather than configurable: a threshold somebody can tune is a threshold
# nobody can compare across periods. The branch's own analysis uses the same rule.
ABC_A_CUT = Decimal("0.80")
ABC_B_CUT = Decimal("0.95")

# What an Item can be ranked by. Money in, money kept, or how much of it went out of the door.
BASES = {"sales": "Sales value", "profit": "Gross profit", "units": "Units"}

# Coefficient of variation bands. 0.5 and 1.0 are the conventional cuts: below half, demand is
# steady enough to plan on; above one, the standard deviation exceeds the mean and "average demand"
# has stopped being a useful sentence.
XYZ_X_CUT = Decimal("0.5")
XYZ_Y_CUT = Decimal("1.0")

# Fewer buckets than this and a coefficient of variation is arithmetic rather than information, so
# the Item is "too new to tell" instead of being called erratic on the strength of a handful of points.
MIN_BUCKETS = 8
TOO_NEW = "new"

# Dead-stock tiers, in days since the item last sold. 90 and 180 map to a quarter and a half year,
# which is how buying decisions are actually discussed.
STALE_DAYS = 90
DEAD_DAYS = 180


async def _q(sql: str, *args) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, list(args))


def _d(v) -> Decimal:
    if v is None:
        return D0
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return D0


@dataclass
class Coverage:
    """What the classification was actually able to see. Travels with every result so a number on
    screen can always be qualified by the data behind it."""
    trading_days: int
    products: int
    classified: int
    unrated: int
    reason: str | None = None
    # XYZ only: what one point of demand is (day, week or month).
    bucket: str | None = None


# ── which stock rows are current ───────────────────────────────────────────────────────────────

async def current_snapshots() -> dict[str, str]:
    """branch_id -> the snapshot id every stock reader must filter on.

    Stock arrives in chunks and is only promoted when the branch says the picture is whole, so
    reading without this filter can mean reading half a shop that is still in flight.
    """
    runs = await BranchSnapshotRun.filter(status="complete").order_by("-completed_at")
    latest: dict[str, str] = {}
    for run in runs:
        key = str(run.branch_id)
        latest.setdefault(key, run.snapshot_id)
    return latest


async def _stock_filter() -> tuple[str, list]:
    """A SQL fragment restricting to current snapshots, plus its parameters. Returns a clause that
    is always safe to AND into a WHERE."""
    latest = await current_snapshots()
    if not latest:
        return "1 = 0", []  # nothing complete has arrived; better to return empty than to lie
    pairs = " OR ".join("(s.branch_id = ? AND s.snapshot_id = ?)" for _ in latest)
    params: list = []
    for branch_id, snapshot_id in latest.items():
        params += [branch_id, snapshot_id]
    return f"({pairs})", params


# ── ABC ────────────────────────────────────────────────────────────────────────────────────────

def basis_of(value: str | None) -> str:
    return value if value in BASES else "sales"


def abc_class(share_before: Decimal) -> str:
    """The class of an Item, given the share of the total carried by every Item ranked above it."""
    if share_before < ABC_A_CUT:
        return "A"
    if share_before < ABC_B_CUT:
        return "B"
    return "C"


def _scope(where: list[str], params: list, branch_code: str | None) -> None:
    if branch_code:
        where.append("b.code = ?")
        params.append(branch_code.upper())


async def abc(start, end, branch_code: str | None = None, basis: str = "sales") -> tuple[list[dict], Coverage]:
    """Rank every Item that sold in the window by the chosen basis, then cut the running total.

    Value by default, not volume. A pharmacy line selling four boxes a week at Rs 3,000 matters more than
    a sweet selling four hundred at Rs 5, and a ranking by units would say the opposite. Units and gross
    profit are there for the questions they answer: shelf space, and what the business actually keeps.
    An Item that earned nothing on the basis (sold at cost or below, on gross profit) is ranked last and
    is C: it carries none of the total.
    """
    basis = basis_of(basis)
    where = ["ps.day >= ?", "ps.day <= ?"]
    params: list = [str(start), str(end)]
    _scope(where, params, branch_code)

    rows = await _q(f"""
        SELECT ps.product_sku AS sku, MAX(ps.product_name) AS name,
               MAX(ps.category) AS category, MAX(ps.brand) AS brand, MAX(ps.department) AS department,
               SUM(CAST(ps.net_sales AS REAL)) AS net_sales,
               SUM(CAST(ps.cogs AS REAL))      AS cogs,
               SUM(CAST(ps.qty AS REAL))       AS qty,
               COUNT(DISTINCT ps.day)          AS selling_days
        FROM branch_product_stats ps
        JOIN branches b ON b.id = ps.branch_id
        WHERE {' AND '.join(where)}
        GROUP BY ps.product_sku
        HAVING SUM(CAST(ps.qty AS REAL)) > 0 OR SUM(CAST(ps.net_sales AS REAL)) > 0
    """, *params)

    days = await _trading_days(start, end, branch_code)
    if not rows:
        return [], Coverage(days, 0, 0, 0, "Nothing sold in this period.")

    cents, grams = Decimal("0.01"), Decimal("0.001")
    items = []
    for r in rows:
        # REAL sums carry float dust; rounding first keeps an Item sitting exactly on a cut from wobbling.
        net = _d(r["net_sales"]).quantize(cents)
        profit = (net - _d(r["cogs"])).quantize(cents)
        qty = _d(r["qty"]).quantize(grams)
        value = {"sales": net, "profit": profit, "units": qty}[basis]
        items.append((value, net, profit, qty, r))
    items.sort(key=lambda i: (-i[0], i[4]["sku"]))
    total = sum((max(i[0], D0) for i in items), D0)

    out: list[dict] = []
    running = D0
    for rank, (value, net, profit, qty, r) in enumerate(items, start=1):
        before = running / total if total > 0 else Decimal(1)
        running += max(value, D0)
        klass = abc_class(before) if value > 0 else "C"
        out.append({
            "rank": rank, "sku": r["sku"], "name": r["name"],
            "category": r["category"], "brand": r["brand"], "department": r["department"],
            "netSales": str(net), "grossProfit": str(profit),
            "marginPct": float(profit / net * 100) if net else 0.0,
            "qty": str(qty),
            "sellingDays": r["selling_days"],
            "basisValue": str(value),
            "sharePct": float(max(value, D0) / total * 100) if total > 0 else 0.0,
            "beforePct": float(before * 100) if total > 0 else 100.0,
            "cumulativePct": float(running / total * 100) if total > 0 else 100.0,
            "abcClass": klass,
        })
    reason = None if total > 0 else f"No Item had any {BASES[basis].lower()} in this period, so nothing can be ranked on it."
    return out, Coverage(days, len(out), len(out), 0, reason)


# ── XYZ ────────────────────────────────────────────────────────────────────────────────────────

def bucket_kind(start: date, end: date) -> str:
    """What one point of demand is. Daily for up to a month; weekly up to 26 weeks, which is six months
    (184 days is the longest any six calendar months can be, so "Last 6 months" always counts in weeks);
    monthly beyond. Daily demand over a year is mostly zeros, and one monthly point for a fortnight is none."""
    days = (end - start).days + 1
    if days <= 31:
        return "day"
    if days <= 184:
        return "week"
    return "month"


def bucket_index(day: date, end: date, kind: str) -> int:
    """Which bucket a day falls in, counting back from the last day of the period (0 = the latest).

    Buckets are counted back from the end so the latest one is always whole: a week is the 7 days ending
    on the period's last day, the 7 before that, and so on; a month runs from the day after that date a
    month earlier (17 Aug to 16 Sep). Calendar weeks and months would leave a half-finished last bucket
    that looks like a slump.
    """
    if kind == "day":
        return (end - day).days
    if kind == "week":
        return (end - day).days // 7
    k = (end.year - day.year) * 12 + end.month - day.month
    return k - 1 if day > months_back(end, k) else k


def xyz_class(buckets: list[Decimal]) -> tuple[str, Decimal | None]:
    """X, Y or Z from a demand series (zeros included), or too new to tell. Returns the class and the CV."""
    n = len(buckets)
    if n < MIN_BUCKETS:
        return TOO_NEW, None
    mean = sum(buckets, D0) / n
    if mean <= 0:
        return TOO_NEW, None
    # Population variance across every bucket. The empty buckets add nothing to the sum but everything
    # to the count, which is the correct treatment: not selling is part of the pattern.
    variance = sum(((x - mean) ** 2 for x in buckets), D0) / n
    cv = variance.sqrt() / mean
    return ("X" if cv <= XYZ_X_CUT else "Y" if cv <= XYZ_Y_CUT else "Z"), cv


def _day(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


async def xyz(start, end, branch_code: str | None = None) -> tuple[list[dict], Coverage]:
    """Classify each Item by how steadily it sells.

    Demand is summed into buckets (see `bucket_kind`), counted from the later of the period's first day
    and the Item's first sale in the data, and **every bucket counts, including the ones where it sold
    nothing**. That is the whole point: an Item that sells forty units in one week of the quarter and
    nothing in the other twelve is the definition of erratic, and averaging only its selling weeks would
    call it perfectly steady. Starting from the first sale stops an Item launched last month from being
    called erratic for all the months before it existed.
    """
    start, end = _day(start), _day(end)
    kind = bucket_kind(start, end)
    days = await _trading_days(start, end, branch_code)

    where = ["ps.day >= ?", "ps.day <= ?"]
    params: list = [str(start), str(end)]
    _scope(where, params, branch_code)
    daily = await _q(f"""
        SELECT ps.product_sku AS sku, ps.day AS day,
               MAX(ps.product_name) AS name, MAX(ps.category) AS category, MAX(ps.brand) AS brand,
               SUM(CAST(ps.qty AS REAL)) AS qty, SUM(CAST(ps.net_sales AS REAL)) AS net_sales
        FROM branch_product_stats ps
        JOIN branches b ON b.id = ps.branch_id
        WHERE {' AND '.join(where)}
        GROUP BY ps.product_sku, ps.day
    """, *params)
    if not daily:
        return [], Coverage(days, 0, 0, 0, "Nothing sold in this period.", kind)

    # The first day each Item sold anywhere in the data (or at this branch), not just in the window.
    first_where = ["ps.day <= ?", "CAST(ps.qty AS REAL) > 0"]
    first_params: list = [str(end)]
    _scope(first_where, first_params, branch_code)
    first_sale = {r["sku"]: _day(r["first_day"]) for r in await _q(f"""
        SELECT ps.product_sku AS sku, MIN(ps.day) AS first_day
        FROM branch_product_stats ps
        JOIN branches b ON b.id = ps.branch_id
        WHERE {' AND '.join(first_where)}
        GROUP BY ps.product_sku
    """, *first_params)}

    items: dict[str, dict] = {}
    for r in daily:
        item = items.setdefault(r["sku"], {"sku": r["sku"], "name": r["name"], "category": r["category"],
                                           "brand": r["brand"], "qty": D0, "net": D0, "days": 0, "points": {}})
        qty = _d(r["qty"])
        item["qty"] += qty
        item["net"] += _d(r["net_sales"])
        if qty > 0:
            item["days"] += 1
            i = bucket_index(_day(r["day"]), end, kind)
            item["points"][i] = item["points"].get(i, D0) + qty

    out: list[dict] = []
    for item in items.values():
        if item["qty"] <= 0:
            continue
        since = max(start, first_sale.get(item["sku"], start))
        n = bucket_index(since, end, kind) + 1
        series = [item["points"].get(i, D0) for i in range(n)]
        klass, cv = xyz_class(series)
        sold_in = sum(1 for x in series if x > 0)
        out.append({
            "sku": item["sku"], "name": item["name"], "category": item["category"], "brand": item["brand"],
            "netSales": str(item["net"].quantize(Decimal("0.01"))),
            "totalQty": str(item["qty"].quantize(Decimal("0.001")).normalize()),
            "sellingDays": item["days"],
            "since": since.isoformat(),
            "bucket": kind,
            "buckets": n,
            "bucketsSold": sold_in,
            "coveragePct": float(Decimal(sold_in) / n * 100) if n else 0.0,
            "avgPerBucket": float(item["qty"] / n) if n else 0.0,
            "cv": float(cv) if cv is not None else None,
            "xyzClass": klass,
        })
    out.sort(key=lambda r: (r["cv"] is None, r["cv"] if r["cv"] is not None else 0, r["name"] or ""))
    new = sum(1 for r in out if r["xyzClass"] == TOO_NEW)
    return out, Coverage(days, len(out), len(out) - new, new, None, kind)


# ── the 9 boxes ────────────────────────────────────────────────────────────────────────────────

XYZ_COLUMNS = ("X", "Y", "Z", TOO_NEW)

# One line a buyer can act on, per box. AX is the line never to run out of; CZ is the line to question.
ADVICE = {
    "AX": "Your steady best sellers. Never let these run out, and reorder on a fixed routine.",
    "AY": "Big earners with ups and downs. Keep extra stock ahead of the busy spells.",
    "AZ": "Big earners that sell in bursts. Watch them closely and order little and often.",
    "AN": "Big earners too new to judge for steadiness. Keep them in stock and look again later.",
    "BX": "Steady middle sellers. Reorder on a routine and keep stock lean.",
    "BY": "Middle sellers that go up and down. Check their stock every week or two.",
    "BZ": "Middle sellers with patchy demand. Order when needed rather than holding a lot.",
    "BN": "Middle sellers too new to judge. See how they settle before changing orders.",
    "CX": "Small but steady. Keep a little on the shelf and reorder on a routine.",
    "CY": "Small sellers that go up and down. Keep stock low and ask if they earn their space.",
    "CZ": "Small and unpredictable. Order only when asked for, or think about dropping them.",
    "CN": "Small sellers too new to judge. Give them time before deciding on them.",
}


def cell_code(abc_klass: str, xyz_klass: str | None) -> str:
    return f"{abc_klass}{'N' if xyz_klass in (None, TOO_NEW) else xyz_klass}"


async def combined(start, end, branch_code: str | None = None, basis: str = "sales") -> tuple[list[dict], Coverage, Coverage]:
    """Every Item carrying both its value class and its steadiness class, and the box they put it in.

    AX is the line to never run out of. CZ is the line to question. The interesting boxes are the corners
    nobody expects: AZ, an Item you depend on and cannot forecast.
    """
    abc_rows, abc_cov = await abc(start, end, branch_code, basis)
    xyz_rows, xyz_cov = await xyz(start, end, branch_code)
    return merge_classes(abc_rows, xyz_rows), abc_cov, xyz_cov


def merge_classes(abc_rows: list[dict], xyz_rows: list[dict]) -> list[dict]:
    """ABC rows with their XYZ reading alongside. An Item with no units to measure (a sale with value and
    no quantity) has no steadiness reading, and sits with the ones too new to tell."""
    xyz_by_sku = {r["sku"]: r for r in xyz_rows}
    merged = []
    for r in abc_rows:
        x = xyz_by_sku.get(r["sku"])
        klass = x["xyzClass"] if x else TOO_NEW
        merged.append({
            **r,
            "xyzClass": klass,
            "cv": x["cv"] if x else None,
            "bucket": x["bucket"] if x else None,
            "buckets": x["buckets"] if x else 0,
            "bucketsSold": x["bucketsSold"] if x else 0,
            "cell": cell_code(r["abcClass"], klass),
        })
    return merged


def matrix_cells(rows: list[dict]) -> list[dict]:
    """The 9 boxes plus a "too new to tell" column, in reading order, with every box present even when
    empty: a missing box and an empty one look the same on a grid, and they are not the same fact."""
    total_sales = sum((Decimal(r["netSales"]) for r in rows), D0)
    total_basis = sum((max(Decimal(r["basisValue"]), D0) for r in rows), D0)
    cells = []
    for a in ("A", "B", "C"):
        for x in XYZ_COLUMNS:
            code = cell_code(a, x)
            members = [r for r in rows if r["cell"] == code]
            sales = sum((Decimal(r["netSales"]) for r in members), D0)
            basis = sum((max(Decimal(r["basisValue"]), D0) for r in members), D0)
            cells.append({
                "cell": code, "abc": a, "xyz": x, "items": len(members),
                "netSales": str(sales.quantize(Decimal("0.01"))),
                "salesPct": float(sales / total_sales * 100) if total_sales else 0.0,
                "basisPct": float(basis / total_basis * 100) if total_basis else 0.0,
                "advice": ADVICE[code],
            })
    return cells


# ── sold least ─────────────────────────────────────────────────────────────────────────────────

async def sold_least(start, end, branch_code: str | None = None, basis: str = "sales",
                     limit: int = 500) -> dict:
    """The bottom of the list, which a ranking by best seller never shows.

    Two lists, because they are two different problems. Items that sold, but least (fewest units, or
    least value or profit on the chosen basis): the slow lines. And Items with stock on the shelf that did
    not sell at all in the period, biggest money first: those never appear in a sales figure, so they
    are invisible to every other view that starts from sales.
    """
    basis = basis_of(basis)
    rows, _, _ = await combined(start, end, branch_code, basis)

    clause, params = await _stock_filter()
    where = [clause, "CAST(s.qty AS REAL) > 0"]
    _scope(where, params, branch_code)
    stock = {r["sku"]: r for r in await _q(f"""
        SELECT s.product_sku AS sku, MAX(s.product_name) AS name, MAX(s.category) AS category, MAX(s.brand) AS brand,
               SUM(CAST(s.qty AS REAL)) AS qty,
               SUM(CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL)) AS value_cost,
               SUM(CAST(s.qty AS REAL) * CAST(s.price AS REAL)) AS value_retail,
               MAX(s.last_sold_at) AS last_sold_at,
               GROUP_CONCAT(DISTINCT b.code) AS branches
        FROM branch_product_stock s
        JOIN branches b ON b.id = s.branch_id
        WHERE {' AND '.join(where)}
        GROUP BY s.product_sku
    """, *params)}

    cents = Decimal("0.01")

    def on_hand(sku: str) -> dict:
        st = stock.get(sku)
        return {
            "onHand": str(_d(st["qty"]).quantize(Decimal("0.001")).normalize()) if st else "0",
            "valueCost": str(_d(st["value_cost"]).quantize(cents)) if st else "0.00",
            "valueRetail": str(_d(st["value_retail"]).quantize(cents)) if st else "0.00",
            "lastSoldAt": st["last_sold_at"] if st else None,
            "branches": ", ".join(sorted((st["branches"] or "").split(","))) if st else "",
        }

    sold = [{**r, **on_hand(r["sku"])} for r in rows]
    sold.sort(key=lambda r: (Decimal(r["basisValue"]), -Decimal(r["valueCost"]), r["name"] or ""))

    sold_skus = {r["sku"] for r in rows}
    unsold = []
    for sku, st in stock.items():
        if sku in sold_skus:
            continue
        unsold.append({
            "sku": sku, "name": st["name"], "category": st["category"], "brand": st["brand"],
            "qty": "0", "netSales": "0.00", "grossProfit": "0.00", **on_hand(sku),
        })
    unsold.sort(key=lambda r: (-Decimal(r["valueCost"]), r["name"] or ""))

    return {
        "sold": sold[:limit], "soldCount": len(sold),
        "unsold": unsold[:limit], "unsoldCount": len(unsold),
        "unsoldValue": str(sum((Decimal(r["valueCost"]) for r in unsold), D0).quantize(cents)),
        "unsoldRetail": str(sum((Decimal(r["valueRetail"]) for r in unsold), D0).quantize(cents)),
    }


async def unsold_on_shelf(start, end, branch_code: str | None = None) -> tuple[int, Decimal]:
    """Just the count and cost of Items holding stock that did not sell in the period, in SQL, for the tile."""
    clause, params = await _stock_filter()
    where = [clause, "CAST(s.qty AS REAL) > 0", "sold.sku IS NULL"]
    _scope(where, params, branch_code)
    # What sold, gathered once and joined, rather than looked up again for each of tens of thousands of
    # stock rows. At one branch it is that branch's sales; for the company, a sale anywhere counts.
    same_branch = "AND sold.branch_id = s.branch_id" if branch_code else ""
    rows = await _q(f"""
        WITH sold AS (
            SELECT DISTINCT product_sku AS sku, {'branch_id' if branch_code else 'NULL'} AS branch_id
            FROM branch_product_stats
            WHERE day >= ? AND day <= ? AND CAST(qty AS REAL) > 0
        )
        SELECT COUNT(DISTINCT s.product_sku) AS items,
               SUM(CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL)) AS value_cost
        FROM branch_product_stock s
        JOIN branches b ON b.id = s.branch_id
        LEFT JOIN sold ON sold.sku = s.product_sku {same_branch}
        WHERE {' AND '.join(where)}
    """, str(start), str(end), *params)
    r = rows[0] if rows else {}
    return int(r.get("items") or 0), _d(r.get("value_cost")).quantize(Decimal("0.01"))


# ── dead / slow stock ──────────────────────────────────────────────────────────────────────────

async def dead_stock_summary(branch_code: str | None = None) -> dict:
    """Just the band totals, banded in SQL.

    A separate path from `dead_stock()` on purpose: the dashboard tile needs three numbers, and
    getting them by pulling 47,000 rows into Python and parsing a timestamp on each one is most of
    a second of every dashboard load, for figures that fit on one line. The detail view still
    materializes rows, because there it is actually showing them.
    """
    clause, params = await _stock_filter()
    where = [clause, "CAST(s.qty AS REAL) > 0"]
    if branch_code:
        where.append("b.code = ?")
        params = params + [branch_code.upper()]

    rows = await _q(f"""
        SELECT CASE
                 WHEN s.last_sold_at IS NULL THEN 'never-sold'
                 WHEN julianday('now') - julianday(s.last_sold_at) >= {DEAD_DAYS} THEN 'dead'
                 WHEN julianday('now') - julianday(s.last_sold_at) >= {STALE_DAYS} THEN 'stale'
                 ELSE 'live'
               END AS band,
               COUNT(*) AS lines,
               SUM(CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL)) AS value_cost
        FROM branch_product_stock s
        JOIN branches b ON b.id = s.branch_id
        WHERE {' AND '.join(where)}
        GROUP BY band
    """, *params)

    summary = {b: {"lines": 0, "value": "0.00"} for b in ("never-sold", "dead", "stale")}
    for r in rows:
        if r["band"] in summary:
            summary[r["band"]] = {
                "lines": int(r["lines"] or 0),
                "value": str(_d(r["value_cost"]).quantize(Decimal("0.01"))),
            }
    return summary


async def dead_stock(branch_code: str | None = None) -> tuple[list[dict], dict]:
    """Items sitting on a shelf that nothing is pulling through.

    `last_sold_at` comes from the branch, over its *entire* sales history — not from the Cloud's
    window of daily stats. That distinction matters: an item last sold eighteen months ago is
    invisible to a thirty-day dataset and completely obvious to the branch, so the branch computes
    it and ships the answer.
    """
    clause, params = await _stock_filter()
    where = [clause, "CAST(s.qty AS REAL) > 0"]
    if branch_code:
        where.append("b.code = ?")
        params = params + [branch_code.upper()]

    rows = await _q(f"""
        SELECT b.code AS branch_code, b.name AS branch_name,
               s.product_sku AS sku, s.product_name AS name,
               s.category, s.brand, s.department,
               CAST(s.qty AS REAL)                          AS qty,
               CAST(s.qty AS REAL) * CAST(s.avg_cost AS REAL) AS value_cost,
               CAST(s.qty AS REAL) * CAST(s.price AS REAL)    AS value_retail,
               s.last_sold_at, s.last_received_at, s.days_with_sales
        FROM branch_product_stock s
        JOIN branches b ON b.id = s.branch_id
        WHERE {' AND '.join(where)}
        ORDER BY value_cost DESC
    """, *params)

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        last = r["last_sold_at"]
        if isinstance(last, str):
            try:
                last = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                last = None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = None if last is None else (now - last).days
        if last is None:
            band = "never-sold"
        elif age >= DEAD_DAYS:
            band = "dead"
        elif age >= STALE_DAYS:
            band = "stale"
        else:
            continue  # sold recently enough to be a live line, not a problem
        out.append({
            "branchCode": r["branch_code"], "branchName": r["branch_name"],
            "sku": r["sku"], "name": r["name"], "category": r["category"],
            "brand": r["brand"], "department": r["department"],
            "qty": str(_d(r["qty"])),
            "valueCost": str(_d(r["value_cost"]).quantize(Decimal("0.01"))),
            "valueRetail": str(_d(r["value_retail"]).quantize(Decimal("0.01"))),
            "lastSoldAt": last.isoformat() if last else None,
            "daysSinceSale": age,
            "band": band,
        })

    summary = {"never-sold": {"lines": 0, "value": D0}, "dead": {"lines": 0, "value": D0}, "stale": {"lines": 0, "value": D0}}
    for r in out:
        s = summary[r["band"]]
        s["lines"] += 1
        s["value"] += _d(r["valueCost"])
    return out, {k: {"lines": v["lines"], "value": str(v["value"].quantize(Decimal("0.01")))} for k, v in summary.items()}


# ── movement bands ─────────────────────────────────────────────────────────────────────────────

async def movement(start, end, branch_code: str | None = None) -> tuple[list[dict], Coverage]:
    """Velocity: units sold per trading day, banded.

    Deliberately separate from ABC. A cheap item can be the fastest thing in the shop and still be
    C-class by value, and a shelf-space conversation is about the first while a buying conversation
    is about the second.
    """
    rows, cov = await abc(start, end, branch_code)
    days = cov.trading_days or 1
    for r in rows:
        r["perDay"] = float(_d(r["qty"]) / Decimal(days))
    ranked = sorted(rows, key=lambda r: r["perDay"], reverse=True)
    n = len(ranked)
    for i, r in enumerate(ranked):
        pct = (i + 1) / n if n else 1
        r["band"] = "fast" if pct <= 0.2 else ("medium" if pct <= 0.5 else "slow")
    return ranked, cov


# ── shared ─────────────────────────────────────────────────────────────────────────────────────

async def _trading_days(start, end, branch_code: str | None) -> int:
    """Distinct days any branch traded in the window — the denominator every rate here divides by.

    Distinct *days*, not rows: two branches trading the same Tuesday is one trading day, and
    counting it twice would halve every per-day figure on the page.
    """
    where = ["day >= ?", "day <= ?"]
    params: list = [str(start), str(end)]
    if branch_code:
        where.append("b.code = ?")
        params.append(branch_code.upper())
    rows = await _q(f"""
        SELECT COUNT(DISTINCT d.day) AS days
        FROM branch_daily_stats d JOIN branches b ON b.id = d.branch_id
        WHERE {' AND '.join(w.replace('day', 'd.day') if w.startswith('day') else w for w in where)}
    """, *params)
    return int(rows[0]["days"] or 0) if rows else 0


async def branch_options() -> list[dict]:
    return [{"code": b.code, "name": b.name} for b in await Branch.all().order_by("name")]
