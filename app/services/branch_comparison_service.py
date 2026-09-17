"""The same Item across branches: how fast it sells at each, how much each holds, and how long that would last.

From that, a plain rule finds stock sitting at one branch that another branch would sell: "slow here, selling there".
Every suggestion says how much to move and why, and head office can turn one into a branch to branch transfer request,
which then runs exactly like one a branch started (transfer_sync_service): the receiving branch agrees, the sending
branch dispatches, the receiving branch counts it in.

Figures are what the branches last reported: units sold from their daily Item figures, stock from their latest complete
stock picture. A branch without both is not reporting, and a comparison needs two that are.
"""
import math
from datetime import datetime, timezone
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import atomic

from app.models import Branch, BranchSnapshotRun, Product, Transfer, TransferLine, User, next_value
from app.services import executive_service as ex

D0 = Decimal("0")
CENTS = Decimal("0.01")
RECEIVE_DAYS = 30
KEEP_DAYS = 60
LIMIT = 200
MANAGERS = [("warehouse.transfers.manage", "X")]
EXECUTIVE = [("executive.dashboard", "R")]


class ComparisonError(Exception):
    def __init__(self, message: str):
        self.message = message


async def _q(sql: str, *args) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, list(args))


def _s(v) -> str:
    return str(Decimal(str(v or 0)).quantize(CENTS))


def _qty(v) -> str:
    return f"{Decimal(str(v or 0)).quantize(Decimal('0.001')).normalize():f}"


def rule_text(receive_days: int, keep_days: int) -> str:
    return (f"Move enough to bring the receiving branch up to {receive_days} days of cover, without taking the sending branch "
            f"below {keep_days} days. An Item is slow at a branch when its stock there would last more than {keep_days} days at "
            f"that branch's own rate of sale (or it didn't sell there at all). It is selling at another branch when that branch's "
            f"stock would last under {receive_days} days and it sells more of it per day than the slow branch.")


async def _latest_runs() -> dict[str, BranchSnapshotRun]:
    latest: dict[str, BranchSnapshotRun] = {}
    for run in await BranchSnapshotRun.filter(status="complete").order_by("-completed_at"):
        latest.setdefault(str(run.branch_id), run)
    return latest


async def _branches(period: ex.Period) -> list[dict]:
    """Every registered branch, and whether it is reporting enough to be compared."""
    runs = await _latest_runs()
    days = {str(r["branch_id"]): int(r["days"] or 0) for r in await _q(
        "SELECT branch_id, COUNT(DISTINCT day) AS days FROM branch_daily_stats WHERE day >= ? AND day <= ? GROUP BY branch_id",
        str(period.start), str(period.end))}
    out = []
    for b in await Branch.all().order_by("name"):
        bid = str(b.id)
        run = runs.get(bid)
        trading = days.get(bid, 0)
        why = None
        if b.status != "active":
            why = "switched off"
        elif not run and not trading:
            why = "has not reported stock or sales"
        elif not run:
            why = "has not reported its stock yet"
        elif not trading:
            why = "reported no sales in these dates"
        out.append({
            "id": bid, "code": b.code, "name": b.name, "hasServer": b.verified_at is not None, "tradingDays": trading,
            "stockAsOf": run.completed_at.isoformat() if run and run.completed_at else None, "snapshotId": run.snapshot_id if run else None,
            "reporting": why is None, "why": why,
        })
    return out


async def _open_requests() -> dict[tuple[str, str, str], str]:
    """Branch to branch transfers not yet sent, by sending branch, receiving branch and Item code."""
    out: dict[tuple[str, str, str], str] = {}
    for t in await Transfer.filter(status__in=["requested", "approved"], source_branch_id__isnull=False).prefetch_related("lines__product"):
        for line in t.lines:
            out[(str(t.source_branch_id), str(t.branch_id), line.product.sku)] = t.transfer_number
    return out


def _cover(on_hand: float, per_day: float) -> float | None:
    """Days the stock would last at this rate; None when nothing sells, however much is held."""
    if per_day <= 0:
        return None
    return max(on_hand, 0) / per_day


async def compare(period: ex.Period, *, focus: str | None = None, q: str | None = None,
                  receive_days: int = RECEIVE_DAYS, keep_days: int = KEEP_DAYS, limit: int = LIMIT) -> dict:
    receive_days = max(1, min(int(receive_days or RECEIVE_DAYS), 365))
    keep_days = max(0, min(int(keep_days or KEEP_DAYS), 730))
    branches = await _branches(period)
    reporting = [b for b in branches if b["reporting"]]
    base = {
        "rule": {"receiveDays": receive_days, "keepDays": keep_days, "text": rule_text(receive_days, keep_days)},
        "branches": [{k: v for k, v in b.items() if k not in ("id", "snapshotId")} for b in branches],
        "reportingCount": len(reporting), "enough": len(reporting) >= 2,
        "suggestions": [], "suggestionsTotal": 0, "items": [], "itemsTotal": 0,
        "columns": [{"code": b["code"], "name": b["name"]} for b in reporting],
        "notes": [
            "Units per day are units sold divided by the days that branch traded in the period.",
            "Days of cover is stock on hand divided by units per day. An Item that didn't sell at a branch has no days of cover there.",
            "Stock is each branch's latest complete stock picture; sales are what it reported for these dates.",
        ],
    }
    if len(reporting) < 2:
        return base

    by_id = {b["id"]: b for b in reporting}
    pairs = " OR ".join("(s.branch_id = ? AND s.snapshot_id = ?)" for _ in reporting)
    stock_params: list = []
    for b in reporting:
        stock_params += [b["id"], b["snapshotId"]]
    ids = [b["id"] for b in reporting]
    marks = ",".join("?" for _ in ids)
    rows = await _q(f"""
        WITH st AS (
            SELECT s.branch_id AS branch_id, s.product_sku AS sku, MAX(s.product_name) AS name, MAX(s.category) AS category,
                   SUM(CAST(s.qty AS REAL)) AS on_hand, MAX(CAST(s.avg_cost AS REAL)) AS avg_cost
            FROM branch_product_stock s WHERE {pairs} GROUP BY s.branch_id, s.product_sku
        ),
        sa AS (
            SELECT p.branch_id AS branch_id, p.product_sku AS sku, MAX(p.product_name) AS name, MAX(p.category) AS category,
                   SUM(CAST(p.qty AS REAL)) AS units
            FROM branch_product_stats p WHERE p.day >= ? AND p.day <= ? AND p.branch_id IN ({marks}) GROUP BY p.branch_id, p.product_sku
        ),
        k AS (SELECT branch_id, sku FROM st UNION SELECT branch_id, sku FROM sa),
        multi AS (SELECT sku FROM k GROUP BY sku HAVING COUNT(DISTINCT branch_id) >= 2)
        SELECT k.branch_id AS branch_id, k.sku AS sku, COALESCE(st.name, sa.name) AS name, COALESCE(st.category, sa.category) AS category,
               COALESCE(st.on_hand, 0) AS on_hand, COALESCE(st.avg_cost, 0) AS avg_cost, COALESCE(sa.units, 0) AS units
        FROM k JOIN multi ON multi.sku = k.sku
        LEFT JOIN st ON st.branch_id = k.branch_id AND st.sku = k.sku
        LEFT JOIN sa ON sa.branch_id = k.branch_id AND sa.sku = k.sku
    """, *stock_params, str(period.start), str(period.end), *ids)

    items: dict[str, dict] = {}
    for r in rows:
        b = by_id.get(str(r["branch_id"]))
        if not b:
            continue
        item = items.setdefault(r["sku"], {"sku": r["sku"], "name": r["name"], "category": r["category"], "at": {}})
        on_hand = float(r["on_hand"] or 0)
        per_day = float(r["units"] or 0) / b["tradingDays"] if b["tradingDays"] else 0.0
        item["at"][b["code"]] = {"code": b["code"], "branchId": b["id"], "name": b["name"], "hasServer": b["hasServer"], "onHand": on_hand,
                                 "units": float(r["units"] or 0), "perDay": per_day, "cover": _cover(on_hand, per_day),
                                 "avgCost": float(r["avg_cost"] or 0)}

    open_requests = await _open_requests()
    suggestions: list[dict] = []
    focus = (focus or "").strip().upper() or None
    for item in items.values():
        cells = list(item["at"].values())
        senders = [c for c in cells if c["onHand"] >= 1 and (c["cover"] is None or c["cover"] > keep_days)]
        receivers = [c for c in cells if c["perDay"] > 0 and c["cover"] is not None and c["cover"] < receive_days]
        if not senders or not receivers:
            continue
        spare = {c["code"]: math.floor(c["onHand"] - keep_days * c["perDay"]) for c in senders}
        need = {c["code"]: math.ceil(receive_days * c["perDay"] - max(c["onHand"], 0)) for c in receivers}
        # The most urgent receiver first, fed from the slowest sender first.
        for to in sorted(receivers, key=lambda c: c["cover"]):
            for frm in sorted(senders, key=lambda c: (c["cover"] is not None, -(c["cover"] or 0))):
                if frm["code"] == to["code"] or to["perDay"] <= frm["perDay"]:
                    continue
                qty = min(need[to["code"]], spare[frm["code"]])
                if qty < 1:
                    continue
                need[to["code"]] -= qty
                spare[frm["code"]] -= qty
                after_from = _cover(frm["onHand"] - qty, frm["perDay"])
                after_to = _cover(to["onHand"] + qty, to["perDay"])
                open_number = open_requests.get((frm["branchId"], to["branchId"], item["sku"]))
                suggestions.append({
                    "sku": item["sku"], "name": item["name"], "category": item["category"], "qty": qty,
                    "valueCost": _s(Decimal(str(qty)) * Decimal(str(frm["avgCost"]))),
                    "from": _side(frm, after_from), "to": _side(to, after_to),
                    "canRequest": frm["hasServer"] and not open_number,
                    "whyNot": (f"{open_number} already asks for this" if open_number else
                               None if frm["hasServer"] else f"{frm['name']} has no branch server of its own, so nobody there can dispatch it"),
                    "openRequest": open_number,
                })
                item["suggested"] = item.get("suggested", D0) + Decimal(suggestions[-1]["valueCost"])
                if need[to["code"]] < 1:
                    break

    if focus:
        suggestions = [s for s in suggestions if focus in (s["from"]["code"], s["to"]["code"])]
    suggestions.sort(key=lambda s: (-Decimal(s["valueCost"]), s["name"] or ""))

    wanted = (q or "").strip().lower()
    listed = [i for i in items.values() if not wanted or wanted in (i["name"] or "").lower() or wanted in i["sku"].lower()]
    if focus:
        listed = [i for i in listed if focus in i["at"]]

    def spread(i: dict) -> float:
        covers = [c["cover"] if c["cover"] is not None else (10000.0 if c["onHand"] > 0 else 0.0) for c in i["at"].values()]
        return max(covers) - min(covers) if covers else 0.0

    listed.sort(key=lambda i: (-(i.get("suggested") or D0), -spread(i), i["name"] or ""))
    codes = [b["code"] for b in reporting]
    return {
        **base,
        "suggestions": suggestions[:limit], "suggestionsTotal": len(suggestions),
        "items": [{
            "sku": i["sku"], "name": i["name"], "category": i["category"], "suggested": "suggested" in i,
            "at": {code: _cell(i["at"].get(code)) for code in codes},
        } for i in listed[:limit]],
        "itemsTotal": len(listed),
    }


def _round(v: float | None, places: int = 1) -> float | None:
    return None if v is None else round(v, places)


def _side(c: dict, cover_after: float | None) -> dict:
    return {"code": c["code"], "name": c["name"], "onHand": _qty(c["onHand"]), "perDay": _round(c["perDay"], 2),
            "cover": _round(c["cover"]), "coverAfter": _round(cover_after)}


def _cell(c: dict | None) -> dict | None:
    if c is None:
        return None
    return {"onHand": _qty(c["onHand"]), "units": _qty(c["units"]), "perDay": _round(c["perDay"], 2), "cover": _round(c["cover"])}


# ── turning a suggestion into a transfer request ─────────────────────────────────────────────
async def _held_at(branch: Branch, sku: str) -> tuple[Decimal | None, dict | None]:
    run = await BranchSnapshotRun.filter(branch_id=branch.id, status="complete").order_by("-completed_at").first()
    if not run:
        return None, None
    rows = await _q("""
        SELECT SUM(CAST(qty AS REAL)) AS qty, MAX(product_name) AS name, MAX(CAST(price AS REAL)) AS price, MAX(CAST(avg_cost AS REAL)) AS avg_cost
        FROM branch_product_stock WHERE branch_id = ? AND snapshot_id = ? AND product_sku = ?
    """, str(branch.id), run.snapshot_id, sku)
    r = rows[0] if rows else None
    if not r or r["qty"] is None:
        return D0, None
    return Decimal(str(r["qty"])), r


@atomic()
async def create_transfer_request(user: User, from_code: str, to_code: str, lines: list[tuple[str, Decimal]], note: str | None) -> Transfer:
    """Head office asks one branch to send stock to another. Nothing moves yet: the receiving branch is asked to agree
    (or, with no branch server of its own, head office receives for it), then the sending branch dispatches it."""
    from app.services import alerts_service, transfer_sync_service

    sender = await ex.branch_by_code((from_code or "").strip())
    receiver = await ex.branch_by_code((to_code or "").strip())
    if not sender or not receiver:
        raise ComparisonError("Choose both branches from the branch list.")
    if sender.id == receiver.id:
        raise ComparisonError("A branch can't send stock to itself.")
    for b in (sender, receiver):
        if b.status != "active":
            raise ComparisonError(f"{b.name} is switched off, so it can't send or receive stock.")
    if sender.verified_at is None:
        raise ComparisonError(f"{sender.name} has no branch server of its own, so nobody there can dispatch it. Send it from the godown instead.")
    if not lines:
        raise ComparisonError("Add at least one Item to send.")

    seen: set[str] = set()
    products: list[tuple[Product, Decimal, Decimal | None]] = []
    open_requests = await _open_requests()
    for sku, qty in lines:
        sku = (sku or "").strip()
        if not sku or sku in seen:
            raise ComparisonError("Each Item goes on the request once, with its whole quantity.")
        seen.add(sku)
        if qty is None or qty <= D0:
            raise ComparisonError("Enter a quantity above zero.")
        held, row = await _held_at(sender, sku)
        name = (row or {}).get("name") or sku
        if held is None:
            raise ComparisonError(f"{sender.name} hasn't reported its stock yet, so there's nothing to check the quantity against.")
        if held < qty:
            raise ComparisonError(f"{sender.name} last reported {held.normalize():f} of {name}; it can't send {qty.normalize():f}.")
        existing = open_requests.get((str(sender.id), str(receiver.id), sku))
        if existing:
            raise ComparisonError(f"{existing} already asks {sender.name} to send {name} to {receiver.name}. Wait for it, or cancel it under Transfers.")
        product = await Product.get_or_none(sku=sku) or await transfer_sync_service._product_for(
            {"sku": sku, "name": name, "price": (row or {}).get("price") or 0})
        unit_cost = Decimal(str((row or {}).get("avg_cost") or 0)).quantize(Decimal("0.0001"))
        products.append((product, qty, unit_cost))

    now = datetime.now(timezone.utc)
    seq = await next_value("transfer", 45)
    # The receiving branch says whether it can take the stock, as for any transfer. One without a server of its own is
    # received for at head office, so there is nobody to ask.
    asks = receiver.verified_at is not None
    written = (note or "").strip()
    transfer = await Transfer.create(
        transfer_number=f"TR-{seq:04d}", branch=receiver, source_branch=sender,
        status="requested" if asks else "approved", requested_at=now, approved_at=None if asks else now, dispute_open=False,
        notes=f"Asked by head office ({user.name}){f': {written}' if written else ': slow at the sending branch, selling at the receiving branch'}"[:255],
        ack_status="awaiting" if asks else "skipped", ack_requested_at=now if asks else None,
        ack_note=None if asks else f"{receiver.name} has no branch server; head office receives for it",
    )
    for product, qty, unit_cost in products:
        await TransferLine.create(transfer=transfer, product=product, qty_sent=qty, qty_received=None, unit_cost=unit_cost or None)
    await transfer_sync_service.publish(transfer)
    await alerts_service.notify(
        "transfer.requested", f"{transfer.transfer_number}: head office asks {sender.name} to send stock to {receiver.name}",
        body=f"Asked by {user.name}. " + (f"Waiting for {receiver.name} to agree, then {sender.name} dispatches it." if asks
                                           else f"{sender.name} dispatches it; head office receives it for {receiver.name}."),
        link="/warehouse/transfers", audience_any=MANAGERS + EXECUTIVE, subject=("transfer", str(transfer.id)),
    )
    await transfer.fetch_related("lines")
    return transfer


# ── two branches side by side ─────────────────────────────────────────────────────────────────
PAIR_LIMIT = 300
VIEWS = ("good-a", "good-b", "only-a", "only-b", "all")


def pair_rule_text(a: str, b: str) -> str:
    return (f"Each Item is ranked within its own branch by sales value for these dates. Selling well: class A, the Items making "
            f"up the first 80% of that branch's sales. Middling: class B, the next 15%. Selling poorly: class C, the last 5%, "
            f"or held there without a sale in these dates. Not carried: neither held nor sold there. So good at {a} and poor at "
            f"{b} is an Item in {a}'s class A that is class C or unsold at {b}, whatever the size of the two branches.")


async def _pair_side(b: dict, period: ex.Period) -> tuple[dict, dict[str, dict]]:
    """One branch's headline figures and its Items: sales for the dates, stock from its latest complete stock picture."""
    from app.services.analytics_service import abc_class

    bid = b["id"]
    day_rows = await _q("""
        SELECT COALESCE(SUM(CAST(net_sales AS REAL)),0) AS net, COALESCE(SUM(CAST(cogs AS REAL)),0) AS cogs,
               COALESCE(SUM(invoices),0) AS bills, COALESCE(SUM(CAST(items_sold AS REAL)),0) AS units,
               COALESCE(SUM(CAST(returns_value AS REAL)),0) AS returns
        FROM branch_daily_stats WHERE branch_id = ? AND day >= ? AND day <= ?
    """, bid, str(period.start), str(period.end))
    d = day_rows[0] if day_rows else {}
    items: dict[str, dict] = {}
    for r in await _q("""
        SELECT product_sku AS sku, MAX(product_name) AS name, MAX(category) AS category, SUM(CAST(qty AS REAL)) AS units,
               SUM(CAST(net_sales AS REAL)) AS net, SUM(CAST(cogs AS REAL)) AS cogs
        FROM branch_product_stats WHERE branch_id = ? AND day >= ? AND day <= ? GROUP BY product_sku
    """, bid, str(period.start), str(period.end)):
        items[r["sku"]] = {"name": r["name"], "category": r["category"], "units": float(r["units"] or 0), "net": float(r["net"] or 0),
                           "onHand": 0.0, "avgCost": 0.0, "held": False}
    stock_value = 0.0
    held_count = 0
    if b["snapshotId"]:
        for r in await _q("""
            SELECT product_sku AS sku, MAX(product_name) AS name, MAX(category) AS category, SUM(CAST(qty AS REAL)) AS qty,
                   MAX(CAST(avg_cost AS REAL)) AS avg_cost
            FROM branch_product_stock WHERE branch_id = ? AND snapshot_id = ? GROUP BY product_sku
        """, bid, b["snapshotId"]):
            qty = float(r["qty"] or 0)
            if qty <= 0 and r["sku"] not in items:
                continue
            item = items.setdefault(r["sku"], {"name": r["name"], "category": r["category"], "units": 0.0, "net": 0.0,
                                               "onHand": 0.0, "avgCost": 0.0, "held": False})
            item["onHand"] = qty
            item["avgCost"] = float(r["avg_cost"] or 0)
            item["held"] = qty > 0
            if qty > 0:
                held_count += 1
                stock_value += qty * item["avgCost"]

    # Classes within this branch, by sales value, with the same cuts as the branch's own Products tab.
    ranked = sorted((i for i in items.values() if i["net"] > 0), key=lambda i: -i["net"])
    total = sum(i["net"] for i in ranked)
    running = 0.0
    for rank, item in enumerate(ranked, start=1):
        item["klass"] = abc_class(Decimal(str(running / total)) if total else Decimal("1"))
        item["rank"] = rank
        item["share"] = item["net"] / total * 100 if total else 0.0
        running += item["net"]
    traded = b["tradingDays"]
    for item in items.values():
        item.setdefault("klass", None)
        item.setdefault("rank", None)
        item.setdefault("share", 0.0)
        item["perDay"] = item["units"] / traded if traded else 0.0
        item["cover"] = _cover(item["onHand"], item["perDay"])
        item["standing"] = ("good" if item["klass"] == "A" else "middling" if item["klass"] == "B"
                            else "poor" if item["klass"] == "C" or item["held"] else "not-carried")

    net, cogs, bills = float(d.get("net") or 0), float(d.get("cogs") or 0), int(d.get("bills") or 0)
    head = {
        "code": b["code"], "name": b["name"], "hasServer": b["hasServer"], "reporting": b["reporting"], "why": b["why"],
        "tradingDays": traded, "stockAsOf": b["stockAsOf"],
        "netSales": _s(net), "grossProfit": _s(net - cogs), "grossMargin": round((net - cogs) / net * 100, 1) if net else None,
        "bills": bills, "averageBill": _s(net / bills) if bills else None, "unitsSold": _qty(d.get("units") or 0),
        "salesPerDay": _s(net / traded) if traded else None, "returns": _s(d.get("returns") or 0),
        "itemsSold": len(ranked), "itemsHeld": held_count, "stockValue": _s(stock_value),
        "heldNotSold": sum(1 for i in items.values() if i["held"] and i["units"] <= 0),
        "classA": sum(1 for i in items.values() if i["klass"] == "A"),
    }
    return head, items


def _pair_cell(i: dict | None) -> dict:
    if i is None:
        return {"standing": "not-carried", "klass": None, "rank": None, "units": "0", "netSales": "0.00", "share": 0.0,
                "perDay": 0.0, "onHand": "0", "cover": None}
    return {"standing": i["standing"], "klass": i["klass"], "rank": i["rank"], "units": _qty(i["units"]), "netSales": _s(i["net"]),
            "share": round(i["share"], 2), "perDay": _round(i["perDay"], 2), "onHand": _qty(i["onHand"]), "cover": _round(i["cover"])}


async def pair(period: ex.Period, a_code: str | None, b_code: str | None, *, view: str = "good-a", q: str | None = None,
               receive_days: int = RECEIVE_DAYS, keep_days: int = KEEP_DAYS, limit: int = PAIR_LIMIT) -> dict:
    """Two branches side by side: their figures for the dates, then every Item as it does at each, sorted into what sells
    well at one and poorly (or not at all) at the other."""
    branches = await _branches(period)
    by_code = {b["code"]: b for b in branches}
    reporting_first = [b["code"] for b in branches if b["reporting"]] + [b["code"] for b in branches if not b["reporting"]]
    a_code = (a_code or "").strip().upper()
    b_code = (b_code or "").strip().upper()
    # First choice: the reporting branches, then the rest.
    if a_code not in by_code:
        a_code = reporting_first[0] if reporting_first else ""
    if b_code not in by_code or b_code == a_code:
        b_code = next((c for c in reporting_first if c != a_code), "")
    view = view if view in VIEWS else "good-a"
    base = {
        "branches": [{"code": b["code"], "name": b["name"], "reporting": b["reporting"], "why": b["why"]} for b in branches],
        "a": None, "b": None, "view": view, "counts": {v: 0 for v in VIEWS}, "items": [], "itemsTotal": 0, "rule": None,
        "notes": [
            "Sales are what each branch reported for these dates; stock is each branch's latest complete stock picture.",
            "Units a day are units sold divided by the days that branch traded in the period.",
            "Days of cover is stock held divided by units a day. An Item that didn't sell has no days of cover.",
            f"A move is offered when the poor side holds stock beyond {keep_days} days of cover and the good side would last under "
            f"{receive_days} days: enough to bring the good side up to {receive_days} days without taking the poor side below {keep_days}.",
        ],
    }
    if not a_code or not b_code:
        return base
    a, b = by_code[a_code], by_code[b_code]
    head_a, items_a = await _pair_side(a, period)
    head_b, items_b = await _pair_side(b, period)
    base.update({"a": head_a, "b": head_b, "rule": pair_rule_text(a["name"], b["name"])})

    open_requests = await _open_requests()
    wanted = (q or "").strip().lower()
    rows = []
    counts = {v: 0 for v in VIEWS}
    for sku in set(items_a) | set(items_b):
        ia, ib = items_a.get(sku), items_b.get(sku)
        sa = ia["standing"] if ia else "not-carried"
        sb = ib["standing"] if ib else "not-carried"
        if sa == "not-carried" and sb == "not-carried":
            continue
        name = (ia or ib)["name"]
        if wanted and wanted not in (name or "").lower() and wanted not in sku.lower():
            continue
        views = {"all"}
        if sa == "good" and sb == "poor":
            views.add("good-a")
        if sb == "good" and sa == "poor":
            views.add("good-b")
        if sa in ("good", "middling") and sb == "not-carried":
            views.add("only-a")
        if sb in ("good", "middling") and sa == "not-carried":
            views.add("only-b")
        for v in views:
            counts[v] += 1
        if view not in views:
            continue
        # Stock that could go from the poor side to the good side, by the same rule as "slow here, selling there".
        move = None
        if view in ("good-a", "good-b"):
            good, poor, frm, to = (ia, ib, b, a) if view == "good-a" else (ib, ia, a, b)
            if poor["onHand"] >= 1 and good["perDay"] > poor["perDay"]:
                qty = min(math.floor(poor["onHand"] - keep_days * poor["perDay"]), math.ceil(receive_days * good["perDay"] - max(good["onHand"], 0)))
                if qty >= 1:
                    open_number = open_requests.get((frm["id"], to["id"], sku))
                    move = {"from": frm["code"], "fromName": frm["name"], "to": to["code"], "toName": to["name"], "qty": qty,
                            "valueCost": _s(Decimal(str(qty)) * Decimal(str(poor["avgCost"]))), "fromHolds": _qty(poor["onHand"]),
                            "canRequest": frm["hasServer"] and not open_number, "openRequest": open_number,
                            "whyNot": (f"{open_number} already asks for this" if open_number else
                                       None if frm["hasServer"] else f"{frm['name']} has no branch server of its own, so nobody there can dispatch it")}
        order = ((ia or {}).get("net", 0.0) if view in ("good-a", "only-a") else (ib or {}).get("net", 0.0) if view in ("good-b", "only-b")
                 else (ia or {}).get("net", 0.0) + (ib or {}).get("net", 0.0))
        rows.append((order, {"sku": sku, "name": name, "category": (ia or ib)["category"], "a": _pair_cell(ia), "b": _pair_cell(ib), "move": move}))
    rows.sort(key=lambda r: (-r[0], r[1]["name"] or ""))
    base.update({"counts": counts, "items": [r for _, r in rows[:limit]], "itemsTotal": len(rows)})
    return base
