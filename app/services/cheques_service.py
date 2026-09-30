"""Cheques at head office, and the post dated cheque register the accountant actually runs.

A cheque is the one document a shop's books cannot get right on their own. It is written today, dated next month,
paid against four invoices, and may bounce. Between those moments it is neither money nor nothing, and an accountant
spends real time on exactly that gap. The old software gives it a whole register with eight filters; we had a list.

**Two questions their register asks that a single status cannot answer.** *Posted* is whether the cheque is in the
books. *Finalized* is whether it has cleared. A cheque can be entered and not posted, and posted and not cleared,
and the middle state is precisely the one somebody chases. So they are two flags, not two values of one.

**Four dates, four different questions.** Recorded (when it was written down), due (the date on its face), cleared
(when the money actually moved) and cancelled. Their register lets you filter on any one of them, because "what did
we write this month" and "what falls due this month" are different months.

**A cheque belongs to a book.** Head office's own, or a branch's, sent up. The register reads one book or all of
them, like every other report here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from tortoise.transactions import atomic

from app.models import Account, Cheque, ChequeLine, User, next_value

ZERO = Decimal("0")
CENT = Decimal("0.01")

DIRECTIONS = ("received", "issued")
STATUSES = ("pending", "cleared", "bounced", "cancelled")
# Which date the register's window applies to. Their four radio buttons, in our column names.
DATE_BASIS = {"record": "received_on", "due": "cheque_date", "finalize": "cleared_on", "cancel": "cancelled_on"}
SORTS = {"due": "cheque_date", "record": "received_on", "amount": "-amount", "party": "party_account__name",
         "number": "number"}
# Y / N / Both, as their dialog puts it. `both` means do not filter on it at all.
TRISTATE = ("yes", "no", "both")


class ChequeError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def money(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except (InvalidOperation, ValueError) as exc:
        raise ChequeError(f"'{value}' isn't an amount.") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tristate(name: str, value: str | None) -> str:
    wanted = (value or "both").strip().lower()
    if wanted not in TRISTATE:
        raise ChequeError(f"{name} is yes, no or both, not '{value}'.")
    return wanted


async def _account(account_id, *, kinds: tuple[str, ...] | None = None, what: str = "account") -> Account:
    account = await Account.get_or_none(id=account_id)
    if account is None:
        raise ChequeError(f"That {what} doesn't exist.")
    if kinds and account.kind not in kinds:
        raise ChequeError(f"{account.name} isn't a {what}.")
    return account


def payload(cheque: Cheque, lines: list[ChequeLine] | None = None) -> dict:
    party = getattr(cheque, "party_account", None)
    bank = getattr(cheque, "bank_account", None)
    today = date.today()
    return {
        "id": str(cheque.id), "book": cheque.book, "number": cheque.number, "direction": cheque.direction,
        "partyAccountId": str(cheque.party_account_id), "partyName": party.name if party else None,
        "partyCode": party.party_code if party else None,
        "bankAccountId": str(cheque.bank_account_id) if cheque.bank_account_id else None,
        "bankAccountName": bank.name if bank else None,
        "chequeNo": cheque.cheque_no, "drawnOn": cheque.drawn_on, "amount": format(money(cheque.amount), "f"),
        "recordedOn": cheque.received_on.isoformat(),
        "dueOn": cheque.cheque_date.isoformat(),
        "clearedOn": cheque.cleared_on.isoformat() if cheque.cleared_on else None,
        "cancelledOn": cheque.cancelled_on.isoformat() if cheque.cancelled_on else None,
        "bouncedOn": cheque.bounced_on.isoformat() if cheque.bounced_on else None,
        "depositedOn": cheque.deposited_on.isoformat() if cheque.deposited_on else None,
        "status": cheque.status, "posted": cheque.posted, "finalized": cheque.finalized,
        # Post dated means dated later than today and not yet settled: it is money promised, not money held.
        "postDated": cheque.status == "pending" and cheque.cheque_date > today,
        "daysToDue": (cheque.cheque_date - today).days,
        "refNo1": cheque.ref_no_1, "refNo2": cheque.ref_no_2, "note": cheque.note,
        "redepositOf": cheque.redeposit_of_id, "createdBy": cheque.created_by_name,
        "lines": [
            {
                "lineNo": l.line_no, "invoiceNo": l.invoice_no,
                "invoiceDate": l.invoice_date.isoformat() if l.invoice_date else None,
                "invoiceAmount": format(money(l.invoice_amount), "f"),
                "outstandingBefore": format(money(l.outstanding_before), "f"),
                "returnAmount": format(money(l.return_amount), "f"),
                "paidAmount": format(money(l.paid_amount), "f"), "note": l.note,
            }
            for l in sorted(lines or [], key=lambda l: l.line_no)
        ],
    }


# ── the register ─────────────────────────────────────────────────────────────────────────────────

async def register(books: list[str], direction: str | None = None, date_basis: str = "due",
                   start: date | None = None, end: date | None = None, posted: str | None = None,
                   finalized: str | None = None, cancelled: bool | None = None, status: str | None = None,
                   party_ids: list[str] | None = None, bank_ids: list[str] | None = None,
                   ref_no_1: str | None = None, ref_no_2: str | None = None, remarks: str | None = None,
                   user: str | None = None, sort_on: str = "due", limit: int = 500, offset: int = 0) -> dict:
    """Their Post Dated Cheque Payable and Receivable reports, which are one register read two ways.

    Every filter on their dialog is here: which of the four dates the window applies to, posted yes/no/both,
    finalized yes/no/both, cancelled or not, both reference numbers, the remarks, the user, the party and the bank
    account, and what to sort on.
    """
    if direction is not None and direction not in DIRECTIONS:
        raise ChequeError("A cheque register is of cheques received or cheques issued.")
    if date_basis not in DATE_BASIS:
        raise ChequeError(f"The window applies to one of: {', '.join(DATE_BASIS)}.")
    if sort_on not in SORTS:
        raise ChequeError(f"Sort on one of: {', '.join(SORTS)}.")
    posted, finalized = _tristate("Posted", posted), _tristate("Finalized", finalized)

    qs = Cheque.filter(book__in=books).prefetch_related("party_account", "bank_account", "lines")
    if direction:
        qs = qs.filter(direction=direction)
    column = DATE_BASIS[date_basis]
    if start:
        qs = qs.filter(**{f"{column}__gte": start})
    if end:
        qs = qs.filter(**{f"{column}__lte": end})
    if posted != "both":
        qs = qs.filter(posted=posted == "yes")
    if finalized != "both":
        qs = qs.filter(finalized=finalized == "yes")
    if cancelled is True:
        qs = qs.filter(status="cancelled")
    elif cancelled is False:
        qs = qs.exclude(status="cancelled")
    if status:
        if status not in STATUSES:
            raise ChequeError(f"A cheque is {', '.join(STATUSES)}, not '{status}'.")
        qs = qs.filter(status=status)
    if party_ids:
        qs = qs.filter(party_account_id__in=party_ids)
    if bank_ids:
        qs = qs.filter(bank_account_id__in=bank_ids)
    for field, value in (("ref_no_1__icontains", ref_no_1), ("ref_no_2__icontains", ref_no_2),
                         ("note__icontains", remarks), ("created_by_name__icontains", user)):
        if value:
            qs = qs.filter(**{field: value})

    rows = await qs.order_by(SORTS[sort_on], "number")
    total = sum((money(c.amount) for c in rows), ZERO)
    # The two figures somebody reads this report for: what is still to come, and what is already overdue.
    today = date.today()
    outstanding = sum((money(c.amount) for c in rows if c.status == "pending"), ZERO)
    overdue = sum((money(c.amount) for c in rows if c.status == "pending" and c.cheque_date < today), ZERO)
    return {
        "direction": direction, "dateBasis": date_basis, "sortOn": sort_on,
        "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
        "count": len(rows), "rows": [payload(c, list(c.lines)) for c in rows[offset:offset + limit]],
        "totals": {"amount": format(total, "f"), "outstanding": format(outstanding, "f"),
                   "overdue": format(overdue, "f")},
    }


async def one(cheque_id) -> dict:
    cheque = await Cheque.get_or_none(id=cheque_id).prefetch_related("party_account", "bank_account", "lines")
    if cheque is None:
        raise ChequeError("That cheque doesn't exist.")
    return payload(cheque, list(cheque.lines))


# ── writing one ──────────────────────────────────────────────────────────────────────────────────

def _lines_must_add_up(amount: Decimal, lines: list[dict]) -> None:
    """A cheque with lines has lines that add up to it. Their data keeps this on all 3,616 cheques that have them,
    and a cheque whose lines do not add up is one nobody can explain a year later."""
    if not lines:
        return
    total = sum((money(l.get("paidAmount")) for l in lines), ZERO)
    if total != amount:
        raise ChequeError(
            f"The invoices on this cheque come to Rs {total:,.2f} and the cheque is Rs {amount:,.2f}. "
            f"They have to match, or leave the invoices off.")


@atomic()
async def create(user: User, data: dict, book: str | None = None) -> dict:
    direction = (data.get("direction") or "received").strip().lower()
    if direction not in DIRECTIONS:
        raise ChequeError("A cheque is received from somebody or issued to somebody.")
    party = await _account(data.get("partyAccountId"), kinds=("customer", "supplier", "general"),
                           what="customer or supplier")
    bank = await _account(data.get("bankAccountId"), kinds=("bank", "wallet"), what="bank account") \
        if data.get("bankAccountId") else None
    if bank is not None and bank.book != party.book:
        raise ChequeError(f"{bank.name} and {party.name} are in different books.")
    amount = money(data.get("amount"))
    if amount <= ZERO:
        raise ChequeError("A cheque is for more than nothing.")
    cheque_no = (data.get("chequeNo") or "").strip()
    if not cheque_no:
        raise ChequeError("Put the cheque's own number on it.")
    lines = list(data.get("lines") or [])
    _lines_must_add_up(amount, lines)

    due = _day(data.get("dueOn"), "the date on the cheque")
    recorded = _day(data.get("recordedOn"), "the date it was written down", default=date.today())
    seq = await next_value("ho-cheque", 1)
    cheque = await Cheque.create(
        book=book or party.book, number=f"CHQ-{seq:06d}", direction=direction, party_account=party,
        bank_account=bank, cheque_no=cheque_no[:30], drawn_on=(data.get("drawnOn") or "").strip()[:80] or None,
        amount=amount, received_on=recorded, cheque_date=due,
        ref_no_1=(data.get("refNo1") or "").strip()[:60] or None,
        ref_no_2=(data.get("refNo2") or "").strip()[:60] or None,
        note=(data.get("note") or "").strip()[:255] or None, created_by_name=user.name,
    )
    await _write_lines(cheque, lines)
    return await one(cheque.id)


def _day(value, what: str, default: date | None = None) -> date:
    if value in (None, ""):
        if default is not None:
            return default
        raise ChequeError(f"Give {what}.")
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ChequeError(f"'{value}' isn't a date.") from exc


async def _write_lines(cheque: Cheque, lines: list[dict]) -> None:
    await ChequeLine.filter(cheque=cheque).delete()
    for i, line in enumerate(lines, start=1):
        invoice_no = (line.get("invoiceNo") or "").strip()
        if not invoice_no:
            raise ChequeError("Each invoice on a cheque needs its number.")
        await ChequeLine.create(
            cheque=cheque, line_no=i, invoice_no=invoice_no[:60],
            invoice_date=_day(line.get("invoiceDate"), "", default=None) if line.get("invoiceDate") else None,
            invoice_amount=money(line.get("invoiceAmount")),
            outstanding_before=money(line.get("outstandingBefore")),
            return_amount=money(line.get("returnAmount")), paid_amount=money(line.get("paidAmount")),
            note=(line.get("note") or "").strip()[:255] or None,
        )


async def _settled(cheque_id) -> Cheque:
    cheque = await Cheque.get_or_none(id=cheque_id)
    if cheque is None:
        raise ChequeError("That cheque doesn't exist.")
    if cheque.status != "pending":
        raise ChequeError(f"That cheque is already {cheque.status}. Undo it first if that was a mistake.")
    return cheque


@atomic()
async def clear(user: User, cheque_id, on: date | None = None) -> dict:
    """It cleared. That is both "finalized" in their words and the moment the money is real."""
    cheque = await _settled(cheque_id)
    cheque.status, cheque.cleared_on, cheque.finalized = "cleared", on or date.today(), True
    cheque.created_by_name = cheque.created_by_name or user.name
    await cheque.save(update_fields=["status", "cleared_on", "finalized", "created_by_name", "updated_at"])
    return await one(cheque.id)


@atomic()
async def bounce(user: User, cheque_id, on: date | None = None, reason: str | None = None) -> dict:
    cheque = await _settled(cheque_id)
    cheque.status, cheque.bounced_on = "bounced", on or date.today()
    if reason:
        cheque.note = ((cheque.note + " · ") if cheque.note else "") + f"Bounced: {reason.strip()[:120]}"
    await cheque.save(update_fields=["status", "bounced_on", "note", "updated_at"])
    return await one(cheque.id)


@atomic()
async def cancel(user: User, cheque_id, on: date | None = None, reason: str | None = None) -> dict:
    cheque = await _settled(cheque_id)
    cheque.status, cheque.cancelled_on = "cancelled", on or date.today()
    if reason:
        cheque.note = ((cheque.note + " · ") if cheque.note else "") + f"Cancelled by {user.name}: {reason.strip()[:120]}"
    await cheque.save(update_fields=["status", "cancelled_on", "note", "updated_at"])
    return await one(cheque.id)


@atomic()
async def redeposit(user: User, cheque_id, due: date, note: str | None = None) -> dict:
    """A bounced cheque presented again is its own row, naming the one that bounced. Editing the bounced one would
    lose the fact that it bounced, which is the one thing anybody wants to know about it."""
    old = await Cheque.get_or_none(id=cheque_id).prefetch_related("lines")
    if old is None:
        raise ChequeError("That cheque doesn't exist.")
    if old.status != "bounced":
        raise ChequeError("Only a bounced cheque is deposited again.")
    seq = await next_value("ho-cheque", 1)
    fresh = await Cheque.create(
        book=old.book, number=f"CHQ-{seq:06d}", direction=old.direction, party_account_id=old.party_account_id,
        bank_account_id=old.bank_account_id, cheque_no=old.cheque_no, drawn_on=old.drawn_on, amount=old.amount,
        received_on=date.today(), cheque_date=due, ref_no_1=old.ref_no_1, ref_no_2=old.ref_no_2,
        note=(note or "").strip()[:255] or f"Deposited again after {old.number} bounced",
        redeposit_of_id=str(old.id), created_by_name=user.name,
    )
    for line in old.lines:
        await ChequeLine.create(
            cheque=fresh, line_no=line.line_no, invoice_no=line.invoice_no, invoice_date=line.invoice_date,
            invoice_amount=line.invoice_amount, outstanding_before=line.outstanding_before,
            return_amount=line.return_amount, paid_amount=line.paid_amount, note=line.note,
        )
    return await one(fresh.id)


# ── a branch's cheque arriving ───────────────────────────────────────────────────────────────────

@atomic()
async def apply_from_branch(book: str, data: dict) -> str:
    """One cheque as a branch recorded it. Keyed on `source`, so the same cheque twice updates rather than doubles.

    A branch's cheque has no cancelled date and no posted or finalized flag of its own, because the branch has one
    status where head office has a status and two flags. Those are derived here rather than left empty: cleared
    means finalized, and a branch only records a cheque once it is in its own books.
    """
    key = f"{book}:{data.get('id')}"
    party_id = data.get("partyAccountId")
    if not party_id or not await Account.exists(id=party_id, book=book):
        raise ChequeError(f"A cheque from {book} names an account that hasn't arrived yet.")
    status = (data.get("status") or "pending").strip().lower()
    if status not in STATUSES:
        status = "pending"
    cleared = _day(data.get("clearedOn"), "", default=None) if data.get("clearedOn") else None
    fields = dict(
        book=book, direction=(data.get("direction") or "received").strip().lower(),
        party_account_id=party_id,
        bank_account_id=data.get("bankAccountId") if data.get("bankAccountId") and await Account.exists(
            id=data.get("bankAccountId"), book=book) else None,
        cheque_no=str(data.get("chequeNo") or "")[:30], drawn_on=(data.get("drawnOn") or None),
        amount=money(data.get("amount")),
        received_on=_day(data.get("recordedOn") or data.get("receivedOn"), "the date it was written down",
                         default=date.today()),
        cheque_date=_day(data.get("dueOn") or data.get("chequeDate"), "the date on the cheque"),
        cleared_on=cleared,
        bounced_on=_day(data.get("bouncedOn"), "", default=None) if data.get("bouncedOn") else None,
        cancelled_on=_day(data.get("cancelledOn"), "", default=None) if data.get("cancelledOn") else None,
        status=status, posted=True, finalized=status == "cleared",
        ref_no_1=(data.get("refNo1") or None), ref_no_2=(data.get("refNo2") or None),
        note=(data.get("note") or None), redeposit_of_id=data.get("redepositOf"),
        created_by_name=data.get("createdBy"),
    )
    existing = await Cheque.get_or_none(source=key)
    if existing is not None:
        await Cheque.filter(id=existing.id).update(**fields)
        cheque = await Cheque.get(id=existing.id)
    else:
        number = str(data.get("number") or "")[:30] or f"CHQ-{await next_value('ho-cheque', 1):06d}"
        if await Cheque.exists(book=book, number=number):
            number = f"{number}-{await next_value('ho-cheque', 1)}"
        cheque = await Cheque.create(source=key, number=number, **fields)
    await _write_lines_from_branch(cheque, list(data.get("lines") or []))
    return f"{cheque.number} {'updated' if existing else 'filed'}"


async def _write_lines_from_branch(cheque: Cheque, lines: list[dict]) -> None:
    """Lines are replaced whole, never merged: a cheque that comes back with fewer invoices has fewer invoices."""
    await ChequeLine.filter(cheque=cheque).delete()
    for i, line in enumerate(lines, start=1):
        await ChequeLine.create(
            cheque=cheque, line_no=int(line.get("lineNo") or i), invoice_no=str(line.get("invoiceNo") or "")[:60],
            invoice_date=_day(line.get("invoiceDate"), "", default=None) if line.get("invoiceDate") else None,
            invoice_amount=money(line.get("invoiceAmount")), outstanding_before=money(line.get("outstandingBefore")),
            return_amount=money(line.get("returnAmount")), paid_amount=money(line.get("paidAmount")),
            note=(line.get("note") or None),
        )
