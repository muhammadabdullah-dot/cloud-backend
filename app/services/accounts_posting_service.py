"""Head office's posting run — the godown's records become automatic vouchers in book "HO".

  grn:<id>                     PV   goods received into the godown: stock at cost, GST input, advance tax — owed to the supplier
  stock-corrections:<day>      STV  approved cycle counts at cost
  transfer-out:<id>            TRV  stock leaves the godown for a branch: on the road, at cost
  transfer-out-received:<id>   TRV  the branch received it: the branch now owes head office for what arrived
  transfer-out-settled:<id>    TRV  a short receipt's dispute was settled: the shortfall is a transit loss
  transfer-relay:<id>          TRV  one branch sent stock to another: the receiver owes head office, head office owes the sender

The branch posts its own side of every transfer in its own books against its HEAD OFFICE CURRENT ACCOUNT, so head
office's current account with a branch and the branch's with head office agree, and cancel in the company's books.
"""
import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from tortoise.transactions import in_transaction

from app.models import GRN, HEAD_OFFICE_BOOK, Account, Product, StockMovement, Transfer, Voucher
from app.services import vouchers_service
from app.services.accounts_chart_service import Resolver
from app.services.accounts_reports_service import PKT, shop_day

ZERO = Decimal("0")
WINDOW_DAYS = 10
_lock = asyncio.Lock()


def bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(start, time.min, tzinfo=PKT).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=PKT).astimezone(timezone.utc))


def _cost(qty, unit_cost, product: Product | None) -> Decimal:
    cost = unit_cost if unit_cost is not None else (product.avg_cost if product and product.avg_cost is not None else ZERO)
    return Decimal(qty or 0) * Decimal(cost)


class Run:
    def __init__(self, settings, start: date, end: date) -> None:
        self.settings, self.start, self.end = settings, start, end
        self.accounts = Resolver(HEAD_OFFICE_BOOK)
        self.counts: Counter = Counter()
        self.problems: list[str] = []
        self.seen: set[str] = set()

    async def put(self, source: str, vtype: str, day: date, rows: list, description: str, reference: str | None = None, header: Account | None = None) -> None:
        self.seen.add(source)
        if day < self.settings.books_start:
            return
        try:
            async with in_transaction():
                outcome = await vouchers_service.upsert_auto(source, vtype, day, rows, description, reference, header, self.settings)
        except vouchers_service.VoucherError as exc:
            self.problems.append(exc.message)
            return
        self.counts[outcome] += 1
        if outcome == "locked":
            self.problems.append(f"{source} changed after its month was closed — the closed books keep the old figures.")

    async def drop(self, source: str) -> None:
        async with in_transaction():
            outcome = await vouchers_service.delete_auto(source, self.settings)
        if outcome != "empty":
            self.counts[outcome] += 1


async def _grns(run: Run) -> None:
    lo, hi = bounds(run.start, run.end)
    acc = run.accounts
    for grn in await GRN.filter(at__gte=lo, at__lt=hi).prefetch_related("lines", "supplier"):
        net = sum((Decimal(l.qty) * Decimal(l.unit_price) * (Decimal("1") - Decimal(l.disc_percent or 0) / Decimal("100")) for l in grn.lines), ZERO)
        gst = sum((Decimal(l.qty) * Decimal(l.unit_price) * (Decimal("1") - Decimal(l.disc_percent or 0) / Decimal("100")) * Decimal(l.tax_rate or 0) / Decimal("100")
                   for l in grn.lines), ZERO)
        advance = Decimal(grn.advance_tax or 0)
        rows = [
            (await acc.key("stock.main"), net, ZERO, "Goods received into the godown"),
            (await acc.key("tax.gst_input"), gst, ZERO, "GST on purchases"),
            (await acc.key("tax.advance"), advance, ZERO, "Advance tax"),
            (await acc.supplier(grn.supplier), ZERO, net + gst + advance, f"Bill {grn.party_inv_no}" if grn.party_inv_no else "Goods received"),
        ]
        await run.put(f"grn:{grn.id}", "PV", shop_day(grn.at), rows,
                      f"{grn.grn_number} from {grn.supplier.name}" + (f", their bill {grn.party_inv_no}" if grn.party_inv_no else ""), grn.party_inv_no or grn.grn_number)


async def _stock_corrections(run: Run) -> None:
    lo, hi = bounds(run.start, run.end)
    acc = run.accounts
    stock = await acc.key("stock.main")
    days: dict[date, list] = defaultdict(list)
    items: Counter = Counter()
    for m in await StockMovement.filter(at__gte=lo, at__lt=hi, kind="count-correction").prefetch_related("product"):
        value = _cost(m.qty, m.unit_cost, m.product)
        if not value:
            continue
        day = shop_day(m.at)
        items[day] += 1
        if value < 0:
            days[day] += [(await acc.key("loss.count"), -value, ZERO, "Count shortage"), (stock, ZERO, -value, "Count shortage")]
        else:
            days[day] += [(stock, value, ZERO, "Found in count"), (await acc.key("income.stock_gain"), ZERO, value, "Found in count")]
    for day, rows in days.items():
        await run.put(f"stock-corrections:{day.isoformat()}", "STV", day, rows, f"Godown cycle counts on {day:%d %b %Y}: {items[day]} item{'s' if items[day] != 1 else ''} at cost")


async def _transfers(run: Run) -> None:
    acc = run.accounts
    stock, transit = await acc.key("stock.main"), await acc.key("stock.transit")
    lo, _ = bounds(run.settings.books_start, run.end)
    for transfer in await Transfer.exclude(status="cancelled").prefetch_related("lines__product", "branch", "source_branch"):
        lines = list(transfer.lines)
        received = transfer.status in ("received", "received_short")
        if transfer.source_branch_id:
            if received and transfer.received_at and transfer.received_at >= lo:
                value = sum((_cost(l.qty_received, l.unit_cost, l.product) for l in lines), ZERO)
                await run.put(f"transfer-relay:{transfer.id}", "TRV", shop_day(transfer.received_at),
                              [(await acc.branch(transfer.branch), value, ZERO, f"Received from {transfer.source_branch.name}"),
                               (await acc.branch(transfer.source_branch), ZERO, value, f"Sent to {transfer.branch.name}")],
                              f"{transfer.transfer_number}: {transfer.source_branch.name} to {transfer.branch.name}", transfer.transfer_number)
            continue
        if transfer.dispatched_at and transfer.dispatched_at >= lo:
            sent = sum((_cost(l.qty_sent, l.unit_cost, l.product) for l in lines), ZERO)
            await run.put(f"transfer-out:{transfer.id}", "TRV", shop_day(transfer.dispatched_at),
                          [(transit, sent, ZERO, "On the road"), (stock, ZERO, sent, "Stock sent from the godown")],
                          f"{transfer.transfer_number} dispatched to {transfer.branch.name}", transfer.transfer_number)
            if received:
                arrived = sum((_cost(l.qty_received, l.unit_cost, l.product) for l in lines), ZERO)
                await run.put(f"transfer-out-received:{transfer.id}", "TRV", shop_day(transfer.received_at or transfer.dispatched_at),
                              [(await acc.branch(transfer.branch), arrived, ZERO, f"Received by {transfer.branch.name}"), (transit, ZERO, arrived, "Arrived")],
                              f"{transfer.transfer_number} received by {transfer.branch.name}", transfer.transfer_number)
                missing = sum((_cost(Decimal(l.qty_sent) - Decimal(l.qty_received or 0), l.unit_cost, l.product) for l in lines), ZERO)
                if missing > 0 and not transfer.dispute_open:
                    await run.put(f"transfer-out-settled:{transfer.id}", "TRV", shop_day(transfer.received_at or transfer.dispatched_at),
                                  [(await acc.key("loss.transit"), missing, ZERO, "Short on arrival"), (transit, ZERO, missing, "Short on arrival")],
                                  f"{transfer.transfer_number}: shortfall written off when the dispute was settled", transfer.transfer_number)


SOURCES = (_grns, _stock_corrections, _transfers)


async def first_record_day() -> date | None:
    firsts = []
    for model, field in ((GRN, "at"), (Transfer, "requested_at")):
        row = await model.all().order_by(field).first()
        if row:
            firsts.append(shop_day(getattr(row, field)))
    return min(firsts) if firsts else None


async def run(full: bool = False) -> dict:
    async with _lock:
        settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
        if settings.books_start is None:
            settings.books_start = await first_record_day() or shop_day()
            await settings.save()
            full = True
        today = shop_day()
        start = settings.books_start if full else max(settings.books_start, today - timedelta(days=WINDOW_DAYS))
        job = Run(settings, start, today)
        for source in SOURCES:
            try:
                await source(job)
            except Exception as exc:  # noqa: BLE001
                job.problems.append(f"{source.__name__.strip('_').replace('_', ' ')}: {type(exc).__name__}: {exc}")
        if full:
            for voucher in await Voucher.filter(book=HEAD_OFFICE_BOOK, auto=True).only("id", "source", "date"):
                if voucher.source and (voucher.date < settings.books_start or (voucher.source.startswith(("grn:", "stock-corrections:")) and voucher.source not in job.seen)):
                    await job.drop(voucher.source)
        settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
        settings.last_posting_at = datetime.now(timezone.utc)
        changed = {k: v for k, v in job.counts.items() if k != "unchanged" and v}
        settings.last_posting_note = (("Everything re-posted from the books' start. " if full else "")
                                      + (", ".join(f"{v} {k}" for k, v in sorted(changed.items())) if changed else "No changes."))[:255]
        settings.posting_problems = job.problems[:50]
        await settings.save()
        return {"full": full, "from": start.isoformat(), "to": today.isoformat(), "counts": dict(job.counts), "problems": job.problems[:50], "note": settings.last_posting_note}


async def ensure_recent(max_age_seconds: int = 90) -> None:
    settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
    if settings.last_posting_at and (datetime.now(timezone.utc) - settings.last_posting_at).total_seconds() < max_age_seconds:
        return
    if _lock.locked():
        return
    await run(full=False)


async def opening_suggestion() -> dict:
    """Godown stock at cost the books don't hold yet — so head office's books agree with the godown's stock value today."""
    from app.services import accounts_reports_service

    await run(full=False)
    settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
    acc = Resolver(HEAD_OFFICE_BOOK)
    stock = await acc.key("stock.main")
    rows = await accounts_reports_service._query(
        "SELECT COALESCE(SUM(b.qty * COALESCE(p.avg_cost, 0)), 0) AS v FROM (SELECT product_id, SUM(qty) AS qty FROM warehouse_stock_movements GROUP BY product_id) b "
        "JOIN products p ON p.id = b.product_id", [])
    value_now = Decimal(str(rows[0]["v"] or 0)) if rows else ZERO
    moved = await accounts_reports_service._query(
        "SELECT SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        "WHERE v.status = 'posted' AND v.vtype != 'OB' AND l.account_id = ? AND v.date >= ?", [str(stock.id), settings.books_start.isoformat()])
    moved_value = (Decimal(str(moved[0]["dr"] or 0)) - Decimal(str(moved[0]["cr"] or 0))) if moved else ZERO
    opening = (value_now - moved_value).quantize(Decimal("0.01"))
    lines = []
    if opening:
        lines.append({"accountId": str(stock.id), "accountCode": stock.code, "accountName": stock.name,
                      "debit": format(max(opening, ZERO), "f"), "credit": format(max(-opening, ZERO), "f"),
                      "description": "Godown stock at cost on the books' start (so the books agree with today's godown stock)"})
    return {"booksStart": settings.books_start.isoformat(), "date": (settings.books_start - timedelta(days=1)).isoformat(), "lines": lines,
            "note": "Cash, bank balances, supplier dues from before, fixed assets, loans and capital aren't in head office's records — add them."}
