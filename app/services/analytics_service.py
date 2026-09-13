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
would be reading noise as a finding. So the coverage figures travel with the result, the minimum
window is enforced rather than assumed, and the caller is told plainly when there is not enough
history to say anything. A classification that quietly degrades into nonsense as data thins is
worse than one that refuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.models import Branch, BranchSnapshotRun

D0 = Decimal("0")

# Pareto, in the form nearly every ERP and textbook uses: A carries to 80% of cumulative value,
# B to 95%, C the remainder. Fixed rather than configurable — a threshold somebody can tune is a
# threshold nobody can compare across periods.
ABC_A_CUT = Decimal("0.80")
ABC_B_CUT = Decimal("0.95")

# Coefficient of variation bands. 0.5 and 1.0 are the conventional cuts: below half, demand is
# steady enough to plan on; above one, the standard deviation exceeds the mean and "average demand"
# has stopped being a useful sentence.
XYZ_X_CUT = Decimal("0.5")
XYZ_Y_CUT = Decimal("1.0")

# Below this many trading days, a coefficient of variation is arithmetic rather than information.
MIN_DAYS_FOR_XYZ = 14

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

async def abc(start, end, branch_code: str | None = None) -> tuple[list[dict], Coverage]:
    """Rank every product that sold in the window by net sales, then cut the cumulative curve.

    Value, not volume. A pharmacy line selling four boxes a week at Rs 3,000 matters more than a
    sweet selling four hundred at Rs 5, and a ranking by units would say the opposite.
    """
    where = ["ps.day >= ?", "ps.day <= ?"]
    params: list = [str(start), str(end)]
    if branch_code:
        where.append("b.code = ?")
        params.append(branch_code.upper())

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
        HAVING SUM(CAST(ps.net_sales AS REAL)) > 0
        ORDER BY net_sales DESC
    """, *params)

    days = await _trading_days(start, end, branch_code)
    total = sum(_d(r["net_sales"]) for r in rows)
    if total <= 0:
        return [], Coverage(days, 0, 0, 0, "Nothing sold in this period.")

    out: list[dict] = []
    running = D0
    for rank, r in enumerate(rows, start=1):
        value = _d(r["net_sales"])
        running += value
        share = running / total
        klass = "A" if share <= ABC_A_CUT else ("B" if share <= ABC_B_CUT else "C")
        margin = value - _d(r["cogs"])
        out.append({
            "rank": rank, "sku": r["sku"], "name": r["name"],
            "category": r["category"], "brand": r["brand"], "department": r["department"],
            "netSales": str(value.quantize(Decimal("0.01"))),
            "grossProfit": str(margin.quantize(Decimal("0.01"))),
            "marginPct": float(margin / value * 100) if value else 0.0,
            "qty": str(_d(r["qty"])),
            "sellingDays": r["selling_days"],
            "sharePct": float(value / total * 100),
            "cumulativePct": float(share * 100),
            "abcClass": klass,
        })
    return out, Coverage(days, len(out), len(out), 0)


# ── XYZ ────────────────────────────────────────────────────────────────────────────────────────

async def xyz(start, end, branch_code: str | None = None) -> tuple[list[dict], Coverage]:
    """Classify each product by how predictable its daily demand is.

    The coefficient of variation is computed over **every trading day in the window, including the
    days a product sold nothing**. That is the whole point: an item that sells forty units on one
    day of the month and nothing on the other thirty is the definition of erratic, and averaging
    only its selling days would hide exactly that and report it as perfectly steady.
    """
    days = await _trading_days(start, end, branch_code)
    if days < MIN_DAYS_FOR_XYZ:
        return [], Coverage(
            days, 0, 0, 0,
            f"XYZ needs at least {MIN_DAYS_FOR_XYZ} trading days to mean anything; this period has {days}.",
        )

    where = ["ps.day >= ?", "ps.day <= ?"]
    params: list = [str(start), str(end)]
    if branch_code:
        where.append("b.code = ?")
        params.append(branch_code.upper())

    # Sum of demand and sum of demand-squared per product, over its selling days. The zero days are
    # folded in afterwards in Python, because they are absences — there is no row to aggregate.
    rows = await _q(f"""
        SELECT ps.product_sku AS sku, MAX(ps.product_name) AS name,
               MAX(ps.category) AS category, MAX(ps.brand) AS brand,
               SUM(CAST(ps.qty AS REAL))                        AS total_qty,
               SUM(CAST(ps.qty AS REAL) * CAST(ps.qty AS REAL)) AS sum_sq,
               SUM(CAST(ps.net_sales AS REAL))                  AS net_sales,
               COUNT(DISTINCT ps.day)                           AS selling_days
        FROM branch_product_stats ps
        JOIN branches b ON b.id = ps.branch_id
        WHERE {' AND '.join(where)}
        GROUP BY ps.product_sku
        HAVING SUM(CAST(ps.qty AS REAL)) > 0
    """, *params)

    n = Decimal(days)
    out: list[dict] = []
    for r in rows:
        total = _d(r["total_qty"])
        sum_sq = _d(r["sum_sq"])
        mean = total / n
        if mean <= 0:
            continue
        # Population variance across all trading days: E[x²] − (E[x])². The zero days contribute
        # nothing to either sum but everything to the denominator, which is the correct treatment.
        variance = (sum_sq / n) - (mean * mean)
        if variance < 0:
            variance = D0  # floating-point dust on a near-constant series
        cv = variance.sqrt() / mean
        klass = "X" if cv <= XYZ_X_CUT else ("Y" if cv <= XYZ_Y_CUT else "Z")
        out.append({
            "sku": r["sku"], "name": r["name"], "category": r["category"], "brand": r["brand"],
            "netSales": str(_d(r["net_sales"]).quantize(Decimal("0.01"))),
            "totalQty": str(total), "sellingDays": r["selling_days"],
            "coveragePct": float(Decimal(r["selling_days"]) / n * 100),
            "avgDaily": float(mean),
            "cv": float(cv),
            "xyzClass": klass,
        })
    out.sort(key=lambda r: r["cv"])
    return out, Coverage(days, len(out), len(out), 0)


async def combined(start, end, branch_code: str | None = None) -> tuple[list[dict], Coverage, Coverage]:
    """The 9-box: every product carrying both its value class and its predictability class.

    AX is the line to never run out of. CZ is the line to stop carrying. The interesting cells are
    the corners nobody expects — AZ, an item you depend on and cannot forecast.
    """
    abc_rows, abc_cov = await abc(start, end, branch_code)
    xyz_rows, xyz_cov = await xyz(start, end, branch_code)
    xyz_by_sku = {r["sku"]: r for r in xyz_rows}
    merged = []
    for r in abc_rows:
        x = xyz_by_sku.get(r["sku"])
        merged.append({
            **r,
            "xyzClass": x["xyzClass"] if x else None,
            "cv": x["cv"] if x else None,
            "cell": f"{r['abcClass']}{x['xyzClass']}" if x else None,
        })
    return merged, abc_cov, xyz_cov


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
