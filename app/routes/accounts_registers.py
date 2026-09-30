"""The registers, the cheque book and the bank reconciliation, over HTTP.

Kept apart from `routes/accounts.py` because they are a different kind of thing. That file serves the statements the
books produce; this one serves the reports an accountant runs against them, plus the two pieces of daily work that
used to live only on the branch server and belong here, where the accountant sits.

Every filter on the old software's own dialogs is a query parameter here, named for what it does rather than for
what their form calls it, and the crosswalk document records which is which.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.middlewares.auth import require_any_permission, require_permission
from app.models import Account, User
from app.routes.accounts import _books_for, _guard, _range
from app.services import accounts_posting_service, accounts_registers_service, bank_rec_service, cheques_service
from app.services.accounts_reports_service import shop_day

Day = date

router = APIRouter(prefix="/accounts", tags=["accounts"])

_registers = require_permission("accounts.registers", "R")
_cheques_read = require_permission("accounts.cheques", "R")
_cheques_write = require_permission("accounts.cheques", "W")
_cheques_act = require_permission("accounts.cheques", "X")
_rec_read = require_any_permission(("accounts.bank-reconciliation", "R"), ("accounts.registers", "R"))
_rec_write = require_permission("accounts.bank-reconciliation", "W")

ERRORS = (accounts_registers_service.RegisterError, cheques_service.ChequeError, bank_rec_service.BankRecError)


async def _run(call, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except ERRORS as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _ids(value: str | None) -> list[str] | None:
    """A comma-separated list of account ids, as every multi-select on their dialogs sends one."""
    if not value:
        return None
    out = [part.strip() for part in value.split(",") if part.strip()]
    return out or None


def _amount(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{value}' isn't an amount.")


# ── the registers ────────────────────────────────────────────────────────────────────────────────

@router.get("/registers/options")
async def options(book: str | None = None, user: User = Depends(_registers)) -> dict:
    """What the filter dialogs offer: the areas, sub-areas and party categories a book actually uses, and the ten
    voucher kinds. Built from the data rather than from a list somebody keeps up to date by hand."""
    books, _ = await _books_for(user, book)
    rows = await Account.filter(book__in=books, kind__in=("customer", "supplier")).values(
        "party_area", "party_sub_area", "party_category", "kind")
    areas = sorted({r["party_area"] for r in rows if r["party_area"]})
    return {
        "areas": areas,
        "subAreas": sorted({r["party_sub_area"] for r in rows if r["party_sub_area"]}),
        "areaSubAreas": sorted({(r["party_area"] or "", r["party_sub_area"] or "") for r in rows}),
        "categories": sorted({r["party_category"] for r in rows if r["party_category"]}),
        "voucherKinds": [{"key": k, "label": label}
                         for k, label in accounts_registers_service.VOUCHER_KIND_LABELS.items()],
        "pivots": list(accounts_registers_service.PIVOTS),
        "operators": list(accounts_registers_service.OPERATORS),
        "customers": await _party_options(books, "customer"),
        "suppliers": await _party_options(books, "supplier"),
        "moneyAccounts": await _money_options(books),
    }


async def _party_options(books: list[str], kind: str) -> list[dict]:
    return [
        {"id": str(a["id"]), "book": a["book"], "code": a["code"], "name": a["name"],
         "partyCode": a["party_code"], "area": a["party_area"], "subArea": a["party_sub_area"],
         "category": a["party_category"]}
        for a in await Account.filter(book__in=books, kind=kind, active=True).order_by("name").values(
            "id", "book", "code", "name", "party_code", "party_area", "party_sub_area", "party_category")
    ]


async def _money_options(books: list[str]) -> list[dict]:
    return [
        {"id": str(a["id"]), "book": a["book"], "code": a["code"], "name": a["name"], "kind": a["kind"]}
        for a in await Account.filter(book__in=books, kind__in=("cash", "bank", "wallet"), active=True)
        .order_by("kind", "name").values("id", "book", "code", "name", "kind")
    ]


@router.get("/registers/cash-flow")
async def cash_flow(from_: Day | None = Query(None, alias="from"), to: Day | None = None, mode: str = "both",
                    accounts: str | None = None, book: str | None = None, user: User = Depends(_registers)) -> dict:
    """Their Cash Flow Statement. `mode` is their three radio buttons: cash, bank, or both."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to, default_days=30)
    return await _run(accounts_registers_service.cash_flow, books, start or end, end, mode, _ids(accounts))


@router.get("/registers/account-summary")
async def account_summary(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                          pivot: str = "account", balanceOp: str | None = None, balanceValue: str | None = None,
                          accounts: str | None = None, includeZero: bool = False, book: str | None = None,
                          user: User = Depends(_registers)) -> dict:
    """Their AccountSummary: the trial balance pivoted to account, group, category or type, with a balance filter."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await _run(accounts_registers_service.account_summary, books, start, end, pivot,
                      balanceOp, _amount(balanceValue), _ids(accounts), includeZero)


@router.get("/registers/voucher-detail")
async def voucher_detail(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                         kinds: str | None = None, forUser: str | None = None, limit: int = 500, offset: int = 0,
                         book: str | None = None, user: User = Depends(_registers)) -> dict:
    """Their Voucher Detail Report. `kinds` is their ten tickboxes, comma separated; leaving it off means all ten."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to, default_days=30)
    picked = [k.strip() for k in (kinds or "").split(",") if k.strip()] or None
    return await _run(accounts_registers_service.voucher_detail, books, start or end, end, picked, forUser,
                      min(max(limit, 1), 5000), max(offset, 0))


@router.get("/registers/date-wise-payment")
async def date_wise_payment(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                            suppliers: str | None = None, book: str | None = None,
                            user: User = Depends(_registers)) -> dict:
    """Their Date Wise Payment: what went out to suppliers, cash kept apart from cheque."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to, default_days=30)
    return await _run(accounts_registers_service.date_wise_money, books, start or end, end, "payment", _ids(suppliers))


@router.get("/registers/date-wise-receipt")
async def date_wise_receipt(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                            customers: str | None = None, area: str | None = None, subArea: str | None = None,
                            category: str | None = None, book: str | None = None,
                            user: User = Depends(_registers)) -> dict:
    """Their DateWise Receipt: what came in from customers, by area if a recovery round is being checked."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to, default_days=30)
    return await _run(accounts_registers_service.date_wise_money, books, start or end, end, "receipt",
                      _ids(customers), area, subArea, category)


@router.get("/registers/receivable-summary")
async def receivable_summary(asOf: Day | None = None, groupBy: str = "none", balanceOp: str | None = None,
                             balanceValue: str | None = None, area: str | None = None, subArea: str | None = None,
                             customers: str | None = None, book: str | None = None,
                             user: User = Depends(require_any_permission(("accounts.registers", "R"), ("accounts.receivables", "R")))) -> dict:
    """Their Customer Receivable Summary: balance, post dated cheques in hand, and what is left."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return await _run(accounts_registers_service.party_summary, books, "customer", asOf or shop_day(), groupBy,
                      balanceOp, _amount(balanceValue), area, subArea, _ids(customers))


@router.get("/registers/payable-summary")
async def payable_summary(asOf: Day | None = None, groupBy: str = "none", balanceOp: str | None = None,
                          balanceValue: str | None = None, suppliers: str | None = None, book: str | None = None,
                          user: User = Depends(require_any_permission(("accounts.registers", "R"), ("accounts.payables", "R")))) -> dict:
    """Their Supplier Payable Summary, the same report pointed the other way."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return await _run(accounts_registers_service.party_summary, books, "supplier", asOf or shop_day(), groupBy,
                      balanceOp, _amount(balanceValue), None, None, _ids(suppliers))


@router.get("/registers/balance-detail")
async def balance_detail(kind: str = "supplier", asOf: Day | None = None, parties: str | None = None,
                         onlyOpen: bool = False, book: str | None = None, user: User = Depends(_registers)) -> dict:
    """Their Supplier Balance Detail: invoice by invoice, aged against that party's own credit days. `kind` points it
    at customers instead, which their software has no equivalent of and an accountant asks for the moment they see
    the supplier one."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return await _run(accounts_registers_service.supplier_balance_detail, books, asOf or shop_day(),
                      _ids(parties), kind, onlyOpen)


@router.get("/registers/daily-operation")
async def daily_operation(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                          suppliers: str | None = None, book: str | None = None,
                          user: User = Depends(_registers)) -> dict:
    """Their Daily Operation Report: the deliveries in a window, what has been paid against each, what is left."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to, default_days=30)
    return await _run(accounts_registers_service.daily_operation, books, start or end, end, _ids(suppliers))


# ── cheques and the post dated register ──────────────────────────────────────────────────────────

class ChequeLineIn(BaseModel):
    invoiceNo: str
    invoiceDate: Day | None = None
    invoiceAmount: str | None = None
    outstandingBefore: str | None = None
    returnAmount: str | None = None
    paidAmount: str


class ChequeIn(BaseModel):
    direction: str = "received"
    partyAccountId: str
    bankAccountId: str | None = None
    chequeNo: str
    drawnOn: str | None = None
    amount: str
    dueOn: Day
    recordedOn: Day | None = None
    refNo1: str | None = None
    refNo2: str | None = None
    note: str | None = None
    lines: list[ChequeLineIn] = []


class ChequeActIn(BaseModel):
    on: Day | None = None
    reason: str | None = None


class RedepositIn(BaseModel):
    dueOn: Day
    note: str | None = None


@router.get("/cheques")
async def cheque_register(direction: str | None = None, dateBasis: str = "due",
                          from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                          posted: str | None = None, finalized: str | None = None, cancelled: bool | None = None,
                          chequeStatus: str | None = None, parties: str | None = None, banks: str | None = None,
                          refNo1: str | None = None, refNo2: str | None = None, remarks: str | None = None,
                          forUser: str | None = None, sortOn: str = "due", limit: int = 500, offset: int = 0,
                          book: str | None = None, user: User = Depends(_cheques_read)) -> dict:
    """The post dated cheque register, which is their Post Date Cheque Payable and Receivable in one endpoint.

    `direction` picks which of their two reports: `issued` is Payable, `received` is Receivable, neither is both.
    `dateBasis` is their four radio buttons: record, due, finalize, cancel.
    """
    books, _ = await _books_for(user, book)
    return await _run(cheques_service.register, books, direction, dateBasis, from_, to, posted, finalized,
                      cancelled, chequeStatus, _ids(parties), _ids(banks), refNo1, refNo2, remarks, forUser,
                      sortOn, min(max(limit, 1), 5000), max(offset, 0))


@router.post("/cheques")
async def add_cheque(body: ChequeIn, user: User = Depends(_cheques_write)) -> dict:
    return await _run(cheques_service.create, user, body.model_dump())


@router.get("/cheques/{cheque_id}")
async def one_cheque(cheque_id: str, user: User = Depends(_cheques_read)) -> dict:
    return await _run(cheques_service.one, cheque_id)


@router.post("/cheques/{cheque_id}/clear")
async def clear_cheque(cheque_id: str, body: ChequeActIn, user: User = Depends(_cheques_act)) -> dict:
    return await _run(cheques_service.clear, user, cheque_id, body.on)


@router.post("/cheques/{cheque_id}/bounce")
async def bounce_cheque(cheque_id: str, body: ChequeActIn, user: User = Depends(_cheques_act)) -> dict:
    return await _run(cheques_service.bounce, user, cheque_id, body.on, body.reason)


@router.post("/cheques/{cheque_id}/cancel")
async def cancel_cheque(cheque_id: str, body: ChequeActIn, user: User = Depends(_cheques_act)) -> dict:
    return await _run(cheques_service.cancel, user, cheque_id, body.on, body.reason)


@router.post("/cheques/{cheque_id}/redeposit")
async def redeposit_cheque(cheque_id: str, body: RedepositIn, user: User = Depends(_cheques_act)) -> dict:
    return await _run(cheques_service.redeposit, user, cheque_id, body.dueOn, body.note)


# ── bank reconciliation ──────────────────────────────────────────────────────────────────────────

class OpenRecIn(BaseModel):
    accountId: str
    upTo: Day
    statementBalance: str
    note: str | None = None


class TickIn(BaseModel):
    lineIds: list[str]
    on: bool = True


# Before /bank-reconciliations/{rec_id}, or "statement" and "accounts" would be read as ids.
@router.get("/bank-reconciliations/statement")
async def bank_statement(from_: Day | None = Query(None, alias="from"), to: Day | None = None,
                         accounts: str | None = None, entryStatus: str = "all", book: str | None = None,
                         user: User = Depends(_rec_read)) -> dict:
    """Their BankReconciliationStatementReport: every entry on every bank account, Clear or UnClear beside it.

    `entryStatus=unclear` is the working list, and on their data it is the whole point: 4,727 pages of card sales
    that have never been agreed with anything.
    """
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await _run(bank_rec_service.statement_report, books, start, end, _ids(accounts), entryStatus)


@router.get("/bank-reconciliations/accounts")
async def rec_accounts(book: str | None = None, user: User = Depends(_rec_read)) -> dict:
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return {"accounts": await _run(bank_rec_service.bank_accounts, books)}


@router.get("/bank-reconciliations")
async def rec_listing(accountId: str | None = None, limit: int = 50, book: str | None = None,
                      user: User = Depends(_rec_read)) -> dict:
    books, _ = await _books_for(user, book)
    return {"reconciliations": await _run(bank_rec_service.listing, books, accountId, min(max(limit, 1), 200))}


@router.post("/bank-reconciliations")
async def rec_open(body: OpenRecIn, user: User = Depends(_rec_write)) -> dict:
    await accounts_posting_service.ensure_recent()
    rec = await _run(bank_rec_service.open_reconciliation, user, body.accountId, body.upTo,
                     body.statementBalance, body.note)
    return await _run(bank_rec_service.summary, rec.id)


@router.get("/bank-reconciliations/{rec_id}")
async def rec_one(rec_id: str, user: User = Depends(_rec_read)) -> dict:
    return {"summary": await _run(bank_rec_service.summary, rec_id),
            "entries": await _run(bank_rec_service.entries, rec_id)}


@router.post("/bank-reconciliations/{rec_id}/tick")
async def rec_tick(rec_id: str, body: TickIn, user: User = Depends(_rec_write)) -> dict:
    changed = await _run(bank_rec_service.tick, rec_id, body.lineIds, body.on)
    return {"changed": changed, "summary": await _run(bank_rec_service.summary, rec_id)}


@router.post("/bank-reconciliations/{rec_id}/close")
async def rec_close(rec_id: str, user: User = Depends(_rec_write)) -> dict:
    await _run(bank_rec_service.close_reconciliation, user, rec_id)
    return await _run(bank_rec_service.summary, rec_id)


@router.post("/bank-reconciliations/{rec_id}/reopen")
async def rec_reopen(rec_id: str, user: User = Depends(_rec_write)) -> dict:
    await _run(bank_rec_service.reopen, user, rec_id)
    return await _run(bank_rec_service.summary, rec_id)


@router.delete("/bank-reconciliations/{rec_id}")
async def rec_discard(rec_id: str, user: User = Depends(_rec_write)) -> dict:
    await _run(bank_rec_service.discard, rec_id)
    return {"discarded": True}
