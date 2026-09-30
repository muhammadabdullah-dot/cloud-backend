"""Agreeing a bank account's books with the bank's statement, at head office, for any book.

The whole feature is one piece of arithmetic, so it is worth stating plainly before any code.

Everything the books say about a bank account up to a date is its **book balance**. What the bank says on that date
is the **statement balance**. They differ by the entries the books know about and the statement does not: cheques
written that nobody has presented yet, and money paid in that has not landed. So:

    book balance  -  what is not on the statement  =  statement balance

Ticking an entry says "this one is on the statement". When the ticked entries come to the statement balance the
account is agreed, and what is left unticked is a list a person can read and act on rather than a number nobody can
explain. That is why the close refuses until the two agree: a reconciliation that does not reconcile is worse than
none, because it says the account was checked.

Two rules that follow from the arithmetic rather than from taste:

  * **Ticked entries are counted from the beginning, not from the last reconciliation.** A statement balance is a
    balance, not a movement, so the ticks that make it up are every ticked entry there has ever been. That also
    means an earlier reconciliation left untouched keeps holding its own ticks, which is what makes this month's
    work only this month's.
  * **An entry dated after the statement's date cannot be on it.** It is left for the next one, whatever it is.

This is the branch's service moved to where the work happens. The accountant sits at head office; nobody at a branch
opens the books. So an account here belongs to a book, and the reconciliation belongs to the same book, and the
statement report can be run for head office, for one branch, or for all of them, like every other report here.

Their own software has this screen and its table is empty: not one of their 1,330 accounts was ever reconciled in
it, while its statement report runs to 4,727 pages of card sales all marked UnClear. That is the state this exists
to make visible.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import atomic

from app.models import Account, BankReconciliation, User, VoucherLine, next_value

ZERO = Decimal("0")
CENT = Decimal("0.01")
POSTED = ("posted",)
# A cash flow can be agreed against a statement for anything the bank issues one for. A wallet (Easypaisa, JazzCash)
# issues one too, and leaving it out would mean the one account a shop most often loses track of cannot be checked.
BANKABLE = ("bank", "wallet")


class BankRecError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


async def _bank(account_id) -> Account:
    account = await Account.get_or_none(id=account_id)
    if account is None:
        raise BankRecError("That account doesn't exist.")
    if account.kind not in BANKABLE:
        raise BankRecError(f"{account.name} isn't a bank account, so there's no statement to agree it with.")
    return account


def _lines(account: Account, up_to: date | None = None):
    """Posted voucher lines on this account. A draft or cancelled voucher is not money yet, so it is not on a
    statement and not in the book balance either. For listing entries, not for totalling them: see `_sum`."""
    qs = VoucherLine.filter(account_id=account.id, voucher__status__in=POSTED)
    return qs.filter(voucher__date__lte=up_to) if up_to else qs


async def _sum(account_id, up_to: date, ticked: bool | None = None) -> Decimal:
    """Money in less money out on this account up to the day, in one query.

    Raw SQL, the way `accounts_reports_service._sums` does it, and for the reason that service does: Tortoise's
    `annotate(Sum(...)).values(...)` groups by the row rather than over the set, so it hands back one line's figures
    dressed as a total. It reads like a sum and is not one. The branch's copy of this was written the wrong way
    first and its check caught it, reporting a 50,000 balance on an account holding 100,000.
    """
    where = ["l.account_id = ?", "v.status = 'posted'", "v.date <= ?"]
    params: list = [str(account_id), up_to.isoformat()]
    if ticked is True:
        where.append("l.reconciliation_id IS NOT NULL")
    elif ticked is False:
        where.append("l.reconciliation_id IS NULL")
    rows = await Tortoise.get_connection("default").execute_query_dict(
        f"SELECT SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l "
        f"JOIN acc_vouchers v ON v.id = l.voucher_id WHERE {' AND '.join(where)}", params,
    )
    if not rows:
        return ZERO
    return money(rows[0]["dr"] or 0) - money(rows[0]["cr"] or 0)


async def book_balance(account: Account, up_to: date) -> Decimal:
    return await _sum(account.id, up_to)


async def ticked_total(account: Account, up_to: date) -> Decimal:
    """Every entry ever ticked onto any reconciliation of this account, up to the day. This is what the statement
    balance is compared with."""
    return await _sum(account.id, up_to, ticked=True)


@atomic()
async def open_reconciliation(user: User, account_id, up_to: date, statement_balance, note: str | None = None) -> BankReconciliation:
    account = await _bank(account_id)
    if await BankReconciliation.filter(account_id=account.id, status="open").exists():
        raise _refuse_open(account)
    later = await BankReconciliation.filter(account_id=account.id, up_to__gte=up_to, status="closed").first()
    if later is not None:
        raise BankRecError(f"{account.name} is already agreed up to {later.up_to:%d %b %Y}. Pick a later day than that.")
    seq = await next_value("bankrec", 1)
    return await BankReconciliation.create(
        book=account.book, number=f"BR-{seq:05d}", account=account, up_to=up_to,
        statement_balance=money(statement_balance), note=(note or "").strip()[:255] or None, created_by_name=user.name,
    )


def _refuse_open(account: Account) -> BankRecError:
    return BankRecError(
        f"{account.name} already has a reconciliation open. Finish or discard that one first, so two people are not "
        f"ticking the same entries.")


async def _open(reconciliation_id) -> BankReconciliation:
    rec = await BankReconciliation.get_or_none(id=reconciliation_id).prefetch_related("account")
    if rec is None:
        raise BankRecError("That reconciliation doesn't exist.")
    if rec.status != "open":
        raise BankRecError(f"{rec.number} is finished. Reopen it to change what is ticked.")
    return rec


@atomic()
async def tick(reconciliation_id, line_ids: list[str], on: bool) -> int:
    """Say that these entries are on the statement, or that they are not after all."""
    rec = await _open(reconciliation_id)
    if not line_ids:
        return 0
    # Which lines are eligible is decided by a query that joins the voucher (for its date and status), and SQLite
    # cannot UPDATE across a join. So the ids are settled first and the update touches them by id alone.
    mine = _lines(rec.account, rec.up_to).filter(id__in=line_ids)
    # Never take an entry off somebody else's reconciliation: a line already ticked elsewhere stays there.
    wanted = mine.filter(reconciliation_id__isnull=True) if on else mine.filter(reconciliation_id=rec.id)
    ids = [str(x) for x in await wanted.values_list("id", flat=True)]
    if not ids:
        return 0
    return await VoucherLine.filter(id__in=ids).update(reconciliation_id=rec.id if on else None)


async def summary(reconciliation_id) -> dict:
    """The four figures, and what is left to explain."""
    rec = await BankReconciliation.get_or_none(id=reconciliation_id).prefetch_related("account")
    if rec is None:
        raise BankRecError("That reconciliation doesn't exist.")
    book = await book_balance(rec.account, rec.up_to)
    ticked = await ticked_total(rec.account, rec.up_to)
    statement = money(rec.statement_balance)
    unticked = await _sum(rec.account_id, rec.up_to, ticked=False)
    return {
        "id": str(rec.id), "book": rec.book, "number": rec.number, "accountId": str(rec.account_id),
        "accountName": rec.account.name, "upTo": rec.up_to.isoformat(), "status": rec.status,
        "statementBalance": format(statement, "f"),
        "bookBalance": format(money(rec.book_balance) if rec.book_balance is not None else book, "f"),
        "tickedTotal": format(ticked, "f"),
        # What the books hold that the statement does not: unpresented cheques and money not yet landed.
        "notOnTheStatement": format(unticked, "f"),
        # Zero means it agrees. Anything else is what nobody has explained yet.
        "difference": format(ticked - statement, "f"),
        "agrees": ticked == statement,
        "note": rec.note, "createdBy": rec.created_by_name,
        "closedAt": rec.closed_at.isoformat() if rec.closed_at else None, "closedBy": rec.closed_by_name,
    }


async def entries(reconciliation_id) -> list[dict]:
    """Every posted entry on the account up to the day, ticked or not, newest first."""
    rec = await BankReconciliation.get_or_none(id=reconciliation_id).prefetch_related("account")
    if rec is None:
        raise BankRecError("That reconciliation doesn't exist.")
    rows = await _lines(rec.account, rec.up_to).prefetch_related("voucher").order_by("-voucher__date", "voucher__number")
    return [
        {
            "id": str(l.id), "date": l.voucher.date.isoformat(), "voucherNumber": l.voucher.number,
            "voucherId": str(l.voucher_id), "description": l.description or l.voucher.description,
            "referenceNo": l.reference_no, "chequeNo": l.voucher.cheque_no,
            "moneyIn": format(money(l.debit), "f"), "moneyOut": format(money(l.credit), "f"),
            "ticked": str(l.reconciliation_id) == str(rec.id),
            # Ticked onto an earlier reconciliation: it counts towards the statement balance and cannot be unticked here.
            "tickedEarlier": bool(l.reconciliation_id) and str(l.reconciliation_id) != str(rec.id),
        }
        for l in rows
    ]


@atomic()
async def close_reconciliation(user: User, reconciliation_id) -> BankReconciliation:
    """Finish it, but only when it actually reconciles."""
    rec = await _open(reconciliation_id)
    ticked = await ticked_total(rec.account, rec.up_to)
    statement = money(rec.statement_balance)
    if ticked != statement:
        gap = ticked - statement
        raise BankRecError(
            f"This doesn't agree yet. What you have ticked comes to Rs {ticked:,.2f} and the statement says "
            f"Rs {statement:,.2f}, a difference of Rs {abs(gap):,.2f}. "
            + ("Tick what else is on the statement" if gap < 0 else "Untick what is not on it")
            + ", or correct the statement balance.")
    rec.book_balance = await book_balance(rec.account, rec.up_to)
    rec.status = "closed"
    rec.closed_at = datetime.now(timezone.utc)
    rec.closed_by_name = user.name
    await rec.save()
    return rec


@atomic()
async def reopen(user: User, reconciliation_id) -> BankReconciliation:
    rec = await BankReconciliation.get_or_none(id=reconciliation_id).prefetch_related("account")
    if rec is None:
        raise BankRecError("That reconciliation doesn't exist.")
    if rec.status == "open":
        return rec
    later = await BankReconciliation.filter(account_id=rec.account_id, status="closed", up_to__gt=rec.up_to).first()
    if later is not None:
        raise BankRecError(
            f"{later.number} agrees this account up to {later.up_to:%d %b %Y}. Reopen that one first, or this one's "
            f"ticks would be changed underneath it.")
    if await BankReconciliation.filter(account_id=rec.account_id, status="open").exclude(id=rec.id).exists():
        raise _refuse_open(rec.account)
    rec.status, rec.closed_at, rec.closed_by_name = "open", None, None
    rec.note = ((rec.note + " · ") if rec.note else "") + f"Reopened by {user.name}"
    await rec.save()
    return rec


@atomic()
async def discard(reconciliation_id) -> None:
    """Throw away an unfinished one, putting its ticks back."""
    rec = await _open(reconciliation_id)
    await VoucherLine.filter(reconciliation_id=rec.id).update(reconciliation_id=None)
    await rec.delete()


async def listing(books: list[str], account_id: str | None = None, limit: int = 50) -> list[dict]:
    qs = BankReconciliation.filter(book__in=books).prefetch_related("account")
    if account_id:
        qs = qs.filter(account_id=account_id)
    rows = await qs.order_by("-up_to", "-created_at").limit(limit)
    return [
        {
            "id": str(r.id), "book": r.book, "number": r.number, "accountId": str(r.account_id),
            "accountName": r.account.name, "upTo": r.up_to.isoformat(), "status": r.status,
            "statementBalance": format(money(r.statement_balance), "f"),
            "bookBalance": format(money(r.book_balance), "f") if r.book_balance is not None else None,
            "closedAt": r.closed_at.isoformat() if r.closed_at else None, "closedBy": r.closed_by_name,
            "createdBy": r.created_by_name,
        }
        for r in rows
    ]


async def bank_accounts(books: list[str]) -> list[dict]:
    """The accounts this can be run for, with what the books say each holds today."""
    today = date.today()
    out = []
    for account in await Account.filter(book__in=books, kind__in=BANKABLE, active=True).order_by("book", "code"):
        open_one = await BankReconciliation.filter(account_id=account.id, status="open").first()
        out.append({
            "id": str(account.id), "book": account.book, "code": account.code, "name": account.name,
            "kind": account.kind, "bankName": account.bank_name, "bankAccountNo": account.bank_account_no,
            "bookBalance": format(await book_balance(account, today), "f"),
            "openReconciliation": str(open_one.id) if open_one else None,
            "openNumber": open_one.number if open_one else None,
        })
    return out


# ── the statement report ─────────────────────────────────────────────────────────────────────────

async def statement_report(books: list[str], start: date | None, end: date,
                           account_ids: list[str] | None = None, status: str = "all") -> dict:
    """Their Bank Reconciliation Statement: every entry on every bank account, with Clear or UnClear beside it.

    This is not the reconciling screen; it is the printout somebody reads to see what has never been agreed. Theirs
    runs to 4,727 pages because every card sale posts its own line to a card settlement account and not one has ever
    been ticked, which is exactly the thing the report is for. `status` narrows it to the half that matters:
    `unclear` is the working list, `clear` is the audit trail.
    """
    if status not in ("all", "clear", "unclear"):
        raise BankRecError("Show all entries, the cleared ones, or the uncleared ones.")
    accounts = await Account.filter(book__in=books, kind__in=BANKABLE).order_by("book", "code")
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if str(a.id) in wanted]
    if not accounts:
        return {"start": start.isoformat() if start else None, "end": end.isoformat(), "status": status,
                "accounts": [], "totals": {"debit": "0.00", "credit": "0.00", "movement": "0.00", "entries": 0}}

    where = ["v.status = 'posted'", "v.date <= ?"]
    params: list = [end.isoformat()]
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    if status == "clear":
        where.append("l.reconciliation_id IS NOT NULL")
    elif status == "unclear":
        where.append("l.reconciliation_id IS NULL")

    out = []
    grand_dr = grand_cr = ZERO
    grand_rows = 0
    for account in accounts:
        rows = await Tortoise.get_connection("default").execute_query_dict(
            "SELECT v.date AS day, v.vtype AS vtype, v.number AS number, v.cheque_no AS cheque_no, "
            "v.description AS description, l.id AS line_id, l.debit AS debit, l.credit AS credit, "
            "l.description AS line_note, l.reference_no AS reference_no, l.reconciliation_id AS rec_id, "
            "r.number AS rec_number "
            "FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
            "LEFT JOIN acc_bank_reconciliations r ON r.id = l.reconciliation_id "
            f"WHERE l.account_id = ? AND {' AND '.join(where)} ORDER BY v.date, v.number",
            [str(account.id), *params],
        )
        if not rows:
            continue
        dr = sum((money(r["debit"]) for r in rows), ZERO)
        cr = sum((money(r["credit"]) for r in rows), ZERO)
        out.append({
            "accountId": str(account.id), "book": account.book, "code": account.code, "name": account.name,
            "bankName": account.bank_name, "bankAccountNo": account.bank_account_no,
            "rows": [
                {
                    "lineId": str(r["line_id"]), "date": str(r["day"])[:10], "type": r["vtype"],
                    "number": r["number"], "chequeNo": r["cheque_no"],
                    "description": r["line_note"] or r["description"], "reference": r["reference_no"],
                    "debit": format(money(r["debit"]), "f"), "credit": format(money(r["credit"]), "f"),
                    "status": "Clear" if r["rec_id"] else "UnClear", "reconciliation": r["rec_number"],
                }
                for r in rows
            ],
            "totals": {"debit": format(dr, "f"), "credit": format(cr, "f"), "movement": format(dr - cr, "f"),
                       "entries": len(rows)},
        })
        grand_dr += dr
        grand_cr += cr
        grand_rows += len(rows)
    return {
        "start": start.isoformat() if start else None, "end": end.isoformat(), "status": status, "accounts": out,
        "totals": {"debit": format(grand_dr, "f"), "credit": format(grand_cr, "f"),
                   "movement": format(grand_dr - grand_cr, "f"), "entries": grand_rows},
    }
