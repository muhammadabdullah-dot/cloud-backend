"""Putting a branch's documents into head office, and reading them back out.

Two callers, one door. A live branch sends one document at a time as it sells, through the event stream that already
runs; the legacy pipeline brings them in bulk out of the old software. Both arrive here in the same shape, and both
are safe to repeat: a document is identified by its branch and the branch's own number, so sending the same bill
twice updates it rather than making a second one. That is what lets a retried event and a replayed window both be
harmless.

**Nothing is recomputed.** The money on a bill is what the branch worked out and the customer paid. Head office
adding up the lines again would produce a second opinion about a receipt somebody is holding, and the reconciliation
that proves our copy matches the source can only do that if we copied rather than recalculated.

**The day is the branch's own trading day**, not the calendar day, because every figure beside these is filed that
way and two answers to "which day was this" is how a screen starts contradicting itself.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from tortoise.transactions import in_transaction

from app.models import (
    Branch,
    BranchFbrInvoice,
    BranchFbrInvoiceLine,
    BranchReturnLine,
    BranchReturnRecord,
    BranchSale,
    BranchSaleLine,
    BranchSaleTender,
)

ZERO = Decimal("0")


class DocumentError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _dec(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return ZERO


def _at(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        raise DocumentError("A document needs the time it was made.")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _day(value, at: datetime) -> date:
    """The branch's own trading day. It sends one; where an older branch does not, the calendar day of `at` stands
    in, which is right for every shop that shuts before midnight and wrong only for one that does not, and that is
    the branch's to tell us."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value:
        return date.fromisoformat(str(value)[:10])
    return at.date()


def _text(value, limit: int) -> str | None:
    text = (str(value).strip() if value is not None else "")[:limit]
    return text or None


# ── a bill ───────────────────────────────────────────────────────────────────────────────────────
async def apply_sale(branch: Branch, data: dict) -> str:
    """One bill with its lines and its tenders. Repeatable: the same bill twice updates in place."""
    invoice = _text(data.get("invoiceNumber"), 30)
    if not invoice:
        raise DocumentError("A bill with no invoice number cannot be filed.")
    at = _at(data.get("at"))
    columns = {
        "at": at, "day": _day(data.get("day"), at),
        "cashier_name": _text(data.get("cashierName"), 120),
        "party_name": _text(data.get("partyName"), 160), "party_code": _text(data.get("partyCode"), 40),
        "till_session_number": _text(data.get("tillSessionNumber"), 20),
        "counter_name": _text(data.get("counterName"), 80),
        "gross": _dec(data.get("gross")), "disc_total": _dec(data.get("discTotal")),
        "fare": _dec(data.get("fare")), "gst": _dec(data.get("gst")),
        "grand_total": _dec(data.get("grandTotal")), "net_value": _dec(data.get("netValue")),
        "received": _dec(data.get("received")), "cash_back": _dec(data.get("cashBack")),
        "cogs": _dec(data.get("cogs")),
        "is_credit_sale": bool(data.get("isCreditSale")), "voided": bool(data.get("voided")),
        "fbr_invoice_number": _text(data.get("fbrInvoiceNumber"), 60),
        "member_code": _text(data.get("memberCode"), 40),
        "earned_points": int(data.get("earnedPoints") or 0),
        "source": _text(data.get("source"), 10) or "branch",
    }

    async with in_transaction():
        sale, made = await BranchSale.get_or_create(
            branch=branch, invoice_number=invoice, defaults=columns)
        if not made:
            for column, value in columns.items():
                setattr(sale, column, value)
            await sale.save()
            # Replaced whole rather than merged: a bill that came back with one line fewer has one line fewer, and a
            # merge would leave the missing one behind for ever.
            await BranchSaleLine.filter(sale_id=sale.id).delete()
            await BranchSaleTender.filter(sale_id=sale.id).delete()

        lines = data.get("lines") or []
        await BranchSaleLine.bulk_create([
            BranchSaleLine(
                sale=sale, line_no=int(l.get("lineNo") or i + 1),
                product_sku=_text(l.get("productSku"), 40) or "?",
                product_name=_text(l.get("productName"), 200), department=_text(l.get("department"), 80),
                qty=_dec(l.get("qty")), unit_price=_dec(l.get("unitPrice")),
                disc_amount=_dec(l.get("discAmount")), tax_amount=_dec(l.get("taxAmount")),
                unit_cost=_dec(l.get("unitCost")), is_return=bool(l.get("isReturn")),
                alias_code=_text(l.get("aliasCode"), 60), promotion_code=_text(l.get("promotionCode"), 20),
                sell_level=_text(l.get("sellLevel"), 20),
            ) for i, l in enumerate(lines)
        ], batch_size=500)

        tenders = data.get("tenders") or []
        await BranchSaleTender.bulk_create([
            BranchSaleTender(
                sale=sale, code=_text(t.get("code"), 20) or "?", name=_text(t.get("name"), 80),
                amount=_dec(t.get("amount")), reference=_text(t.get("reference"), 120),
            ) for t in tenders
        ], batch_size=500)

    return (f"{invoice} {'filed' if made else 'updated'}: {len(lines)} line(s), "
            f"{len(tenders)} tender(s), {columns['net_value']}")


# ── a return ─────────────────────────────────────────────────────────────────────────────────────
async def apply_return(branch: Branch, data: dict) -> str:
    number = _text(data.get("number"), 30)
    if not number:
        raise DocumentError("A return with no number cannot be filed.")
    at = _at(data.get("at"))
    columns = {
        "at": at, "day": _day(data.get("day"), at),
        "against_invoice": _text(data.get("againstInvoice"), 30),
        "cashier_name": _text(data.get("cashierName"), 120),
        "refund_total": _dec(data.get("refundTotal")), "tax_total": _dec(data.get("taxTotal")),
        "refund_method": _text(data.get("refundMethod"), 20), "reason": _text(data.get("reason"), 160),
        "note": _text(data.get("note"), 255),
        "till_session_number": _text(data.get("tillSessionNumber"), 20),
        "source": _text(data.get("source"), 10) or "branch",
    }
    async with in_transaction():
        record, made = await BranchReturnRecord.get_or_create(
            branch=branch, number=number, defaults=columns)
        if not made:
            for column, value in columns.items():
                setattr(record, column, value)
            await record.save()
            await BranchReturnLine.filter(return_record_id=record.id).delete()
        lines = data.get("lines") or []
        await BranchReturnLine.bulk_create([
            BranchReturnLine(
                return_record=record, line_no=int(l.get("lineNo") or i + 1),
                product_sku=_text(l.get("productSku"), 40) or "?",
                product_name=_text(l.get("productName"), 200),
                qty=_dec(l.get("qty")), unit_price=_dec(l.get("unitPrice")),
                tax_amount=_dec(l.get("taxAmount")), unit_cost=_dec(l.get("unitCost")),
            ) for i, l in enumerate(lines)
        ], batch_size=500)
    return f"{number} {'filed' if made else 'updated'}: {len(lines)} line(s) back, {columns['refund_total']}"


# ── what FBR was told ────────────────────────────────────────────────────────────────────────────
async def apply_fbr_invoice(branch: Branch, data: dict) -> str:
    invoice = _text(data.get("invoiceNumber"), 30)
    if not invoice:
        raise DocumentError("An FBR invoice has to say which bill it is for.")
    kind = (_text(data.get("kind"), 10) or "sale").lower()
    at = _at(data.get("at"))
    columns = {
        "at": at, "day": _day(data.get("day"), at),
        "fbr_invoice_number": _text(data.get("fbrInvoiceNumber"), 60),
        "status": _text(data.get("status"), 20) or "sent",
        "pos_id": _text(data.get("posId"), 40),
        "total": _dec(data.get("total")), "tax_total": _dec(data.get("taxTotal")),
        "message": _text(data.get("message"), 255),
        "buyer_name": _text(data.get("buyerName"), 160), "buyer_ntn": _text(data.get("buyerNtn"), 40),
        "buyer_cnic": _text(data.get("buyerCnic"), 40),
        "source": _text(data.get("source"), 10) or "branch",
    }
    async with in_transaction():
        row, made = await BranchFbrInvoice.get_or_create(
            branch=branch, invoice_number=invoice, kind=kind, defaults=columns)
        if not made:
            for column, value in columns.items():
                setattr(row, column, value)
            await row.save()
            await BranchFbrInvoiceLine.filter(invoice_id=row.id).delete()
        lines = data.get("lines") or []
        await BranchFbrInvoiceLine.bulk_create([
            BranchFbrInvoiceLine(
                invoice=row, line_no=int(l.get("lineNo") or i + 1),
                product_sku=_text(l.get("productSku"), 40), product_name=_text(l.get("productName"), 200),
                hs_code=_text(l.get("hsCode"), 40), qty=_dec(l.get("qty")),
                unit_price=_dec(l.get("unitPrice")), disc_amount=_dec(l.get("discAmount")),
                tax_rate=_dec(l.get("taxRate")), tax_amount=_dec(l.get("taxAmount")),
                total=_dec(l.get("total")),
            ) for i, l in enumerate(lines)
        ], batch_size=500)
    return (f"{invoice} to FBR {'filed' if made else 'updated'}: {columns['status']}"
            + (f", their number {columns['fbr_invoice_number']}" if columns["fbr_invoice_number"] else ""))


# ── loading a window in bulk, for the pipeline ───────────────────────────────────────────────────
async def replace_window(branch: Branch, start: date, end: date, sales: list[dict], returns: list[dict],
                         fbr: list[dict]) -> dict:
    """Everything this branch did between two days, replacing whatever was there.

    A window rather than a merge, because a document deleted at the source has to disappear here too, and the only
    way to know it is gone is that it did not come back. The same reasoning as
    `snapshot_service.apply_aggregates(window=...)`, and the same danger: anything outside the window is left alone,
    so a payload carrying a stray day would otherwise write days the delete never cleared. Those are counted and
    skipped instead.
    """
    def inside(rows: list[dict]) -> tuple[list[dict], int]:
        keep, skipped = [], 0
        for row in rows:
            at = _at(row.get("at"))
            if start <= _day(row.get("day"), at) <= end:
                keep.append(row)
            else:
                skipped += 1
        return keep, skipped

    sales, skipped_sales = inside(sales)
    returns, skipped_returns = inside(returns)
    fbr, skipped_fbr = inside(fbr)

    async with in_transaction():
        # The lines and tenders go with their documents: every child is ON DELETE CASCADE.
        await BranchSale.filter(branch=branch, day__gte=start, day__lte=end).delete()
        await BranchReturnRecord.filter(branch=branch, day__gte=start, day__lte=end).delete()
        await BranchFbrInvoice.filter(branch=branch, day__gte=start, day__lte=end).delete()

    for row in sales:
        await apply_sale(branch, row)
    for row in returns:
        await apply_return(branch, row)
    for row in fbr:
        await apply_fbr_invoice(branch, row)

    return {
        "branch": branch.code, "from": start.isoformat(), "to": end.isoformat(),
        "sales": len(sales), "returns": len(returns), "fbrInvoices": len(fbr),
        "skippedOutsideWindow": skipped_sales + skipped_returns + skipped_fbr,
    }
