"""Head office's books over HTTP — its own (book HO), any branch's as the branch sent it, or all of them together (book ALL)."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.middlewares.auth import get_current_user, require_any_permission, require_permission
from app.models import HEAD_OFFICE_BOOK, Account, User, Voucher
from app.services import accounts_areas, accounts_chart_service, accounts_posting_service, accounts_reports_service, vouchers_service
from app.services.accounts_areas import access_of
from app.services.accounts_reports_service import shop_day
from app.services.rbac_service import has_permission

Day = date

router = APIRouter(prefix="/accounts", tags=["accounts"])

# A resource per screen and action (core/abilities.py). Whole-book reports need only their own tick; the ledger, the
# chart and vouchers are also limited to the accounts in the person's areas (services/accounts_areas.py). A branch's
# books, or all of them together, also need accounts.branch-books.
_desk = require_permission("accounts.desk", "R")
_post_now = require_permission("accounts.desk", "X")
_trial_balance = require_permission("accounts.trial-balance", "R")
_income_statement = require_permission("accounts.income-statement", "R")
_balance_sheet = require_permission("accounts.balance-sheet", "R")
_month_by_month = require_permission("accounts.month-by-month", "R")
_day_book = require_permission("accounts.day-book", "R")
_ledger = require_permission("accounts.ledger", "R")
_vouchers = require_permission("accounts.vouchers", "R")
_voucher_open = require_any_permission(("accounts.vouchers", "R"), ("accounts.day-book", "R"))
# Which of the two a voucher needs depends on its type: the opening balances have their own tick.
_write = require_any_permission(("accounts.vouchers", "W"), ("accounts.opening-balances", "W"))
_post = require_permission("accounts.vouchers.post", "X")
_reverse = require_permission("accounts.vouchers.reverse", "X")
_opening = require_permission("accounts.opening-balances", "W")
_chart = require_permission("accounts.chart", "W")
_settings_read = require_permission("accounts.settings", "R")
_settings = require_permission("accounts.settings", "W")
_period = require_permission("accounts.period", "X")
_statement = require_any_permission(("accounts.receivables", "R"), ("accounts.payables", "R"), ("accounts.ledger", "R"))
# Every screen that picks an account reads the chart.
_chart_read = require_any_permission(
    ("accounts.chart", "R"), ("accounts.ledger", "R"), ("accounts.vouchers", "R"), ("accounts.opening-balances", "R"),
    ("accounts.settings", "R"), ("accounts.receivables", "R"),
)
_lookup = require_any_permission(("accounts.chart", "R"), ("accounts.vouchers", "W"), ("accounts.opening-balances", "W"))
# Every accounts screen names the books it can switch between.
_any_screen = require_any_permission(*[(resource, "R") for resource in (
    "accounts.desk", "accounts.trial-balance", "accounts.income-statement", "accounts.balance-sheet", "accounts.month-by-month",
    "accounts.day-book", "accounts.ledger", "accounts.vouchers", "accounts.opening-balances", "accounts.chart", "accounts.receivables",
    "accounts.payables", "accounts.fixed-assets", "accounts.tax", "accounts.settings",
)])

ERRORS = (vouchers_service.VoucherError, accounts_chart_service.ChartError)


async def _books_for(user: User, book: str | None) -> tuple[list[str], bool]:
    """Head office's own book needs only the screen's tick; a branch's, or all of them, needs accounts.branch-books too."""
    books, together = await accounts_reports_service.resolve_books(book)
    if books != [HEAD_OFFICE_BOOK] and not await has_permission(user, "accounts.branch-books", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seeing branches' books needs the right to see branch books.")
    return books, together


async def _guard(call: Callable, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except accounts_areas.AreaRefused as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.message)
    except ERRORS as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _may_write(user: User, vtype: str | None) -> None:
    if (vtype or "").upper() == "OB":
        if not await has_permission(user, "accounts.opening-balances", "W"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Writing the opening balances needs its own access. Ask your manager for it.")
    elif not await has_permission(user, "accounts.vouchers", "W"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Writing vouchers needs its own access. Ask your manager for it.")


def _range(from_: date | None, to: date | None, default_days: int = 0) -> tuple[date | None, date]:
    end = to or shop_day()
    start = from_ if from_ else (end - timedelta(days=default_days) if default_days else None)
    if start and start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The start date is after the end date.")
    return start, end


# ── chart ──────────────────────────────────────────────────────────────────────────────────────

class GroupIn(BaseModel):
    categoryCode: str | None = None
    name: str | None = None
    priority: int | None = None
    manualCode: str | None = None


class SubGroupIn(BaseModel):
    groupCode: str | None = None
    name: str


class AccountIn(BaseModel):
    groupCode: str | None = None
    subGroupCode: str | None = None
    name: str | None = None
    kind: str | None = None
    active: bool | None = None
    restricted: bool | None = None
    checkLimit: bool | None = None
    balanceLimit: Decimal | None = None
    bankName: str | None = None
    bankAccountNo: str | None = None
    manualCode: str | None = None
    remarks: str | None = None


def _account_fields(payload: AccountIn) -> dict:
    data = payload.model_dump(exclude_unset=True)
    mapping = {"groupCode": "group_code", "subGroupCode": "sub_group_code", "checkLimit": "check_limit", "balanceLimit": "balance_limit",
               "bankName": "bank_name", "bankAccountNo": "bank_account_no", "manualCode": "manual_code"}
    return {mapping.get(k, k): v for k, v in data.items()}


@router.get("/books")
async def books(user: User = Depends(_any_screen)) -> list[dict]:
    """Head office's book and every branch's, with how each stands."""
    codes = await accounts_reports_service.all_books()
    if not await has_permission(user, "accounts.branch-books", "R"):
        codes = [HEAD_OFFICE_BOOK]
    from app.models import Branch

    names = {b.code: b.name for b in await Branch.all()}
    today = shop_day()
    out = []
    for code in codes:
        summary = await accounts_reports_service.book_summary(code, today)
        out.append({**summary, "name": "Head Office" if code == HEAD_OFFICE_BOOK else names.get(code, code), "readOnly": code != HEAD_OFFICE_BOOK})
    return out


@router.get("/chart")
async def chart(book: str | None = None, user: User = Depends(_chart_read)) -> dict:
    """One book's chart, as far as the person's areas go."""
    books, together = await _books_for(user, book)
    if together:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick one book's chart.")
    if books == [HEAD_OFFICE_BOOK]:
        await accounts_chart_service.ensure_supplier_accounts()
        await accounts_chart_service.ensure_branch_accounts()
    return await accounts_chart_service.tree(books[0], await access_of(user))


@router.get("/lookup")
async def lookup(q: str | None = None, kinds: str | None = None, limit: int = 30, book: str | None = None, user: User = Depends(_lookup)) -> list[dict]:
    """Accounts to pick in a voucher line (those the person may use), by code or name."""
    books, _ = await _books_for(user, book)
    qs = Account.filter(active=True, book__in=books)
    access = await access_of(user)
    if not access.uses_everything:
        areas = await accounts_areas.areas_by_account(books)
        qs = qs.filter(id__in=[i for i, (area, key) in areas.items() if access.can_use(area, key)])
    if kinds:
        qs = qs.filter(kind__in=[k.strip() for k in kinds.split(",") if k.strip()])
    if q and q.strip():
        from tortoise.expressions import Q

        term = q.strip()
        qs = qs.filter(Q(name__icontains=term) | Q(code__startswith=term) | Q(manual_code__icontains=term))
    rows = await qs.order_by("code").limit(min(max(limit, 1), 200)).prefetch_related("group")
    return [{"id": str(a.id), "book": a.book, "code": a.code, "name": a.name, "kind": a.kind, "groupCode": a.group.code, "groupName": a.group.name,
             "restricted": a.restricted, "systemKey": a.system_key} for a in rows]


@router.post("/groups")
async def create_group(payload: GroupIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), category_code=payload.categoryCode)
    group = await _guard(accounts_chart_service.create_group, payload.categoryCode or "", payload.name or "", payload.priority or 0, payload.manualCode)
    return accounts_chart_service.group_payload(group)


@router.patch("/groups/{code}")
async def update_group(code: str, payload: GroupIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), group_code=code)
    group = await _guard(accounts_chart_service.update_group, code, payload.name, payload.priority, payload.manualCode)
    return accounts_chart_service.group_payload(group)


@router.post("/sub-groups")
async def create_sub_group(payload: SubGroupIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), group_code=payload.groupCode)
    sub = await _guard(accounts_chart_service.create_sub_group, payload.groupCode or "", payload.name)
    return {"code": sub.code, "name": sub.name, "groupCode": sub.group_id.split(":", 1)[1]}


@router.patch("/sub-groups/{code}")
async def update_sub_group(code: str, payload: SubGroupIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), sub_group_code=code)
    sub = await _guard(accounts_chart_service.update_sub_group, code, payload.name)
    return {"code": sub.code, "name": sub.name, "groupCode": sub.group_id.split(":", 1)[1]}


@router.delete("/groups/{code}")
async def delete_group(code: str, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), group_code=code)
    await _guard(accounts_chart_service.delete_group, code)
    return {"deleted": True}


@router.delete("/sub-groups/{code}")
async def delete_sub_group(code: str, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_group_change, await access_of(user), sub_group_code=code)
    await _guard(accounts_chart_service.delete_sub_group, code)
    return {"deleted": True}


@router.post("/accounts")
async def create_account(payload: AccountIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_account_change, await access_of(user), None, group_code=payload.groupCode, kind=payload.kind)
    account = await _guard(accounts_chart_service.create_account, _account_fields(payload))
    return accounts_chart_service.account_payload(account)


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, payload: AccountIn, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_account_change, await access_of(user), await Account.get_or_none(id=account_id, book=HEAD_OFFICE_BOOK),
                 group_code=payload.groupCode, kind=payload.kind)
    account = await _guard(accounts_chart_service.update_account, account_id, _account_fields(payload))
    return accounts_chart_service.account_payload(account)


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, user: User = Depends(_chart)) -> dict:
    await _guard(accounts_areas.check_account_change, await access_of(user), await Account.get_or_none(id=account_id, book=HEAD_OFFICE_BOOK))
    await _guard(accounts_chart_service.delete_account, account_id)
    return {"deleted": True}


# ── vouchers ───────────────────────────────────────────────────────────────────────────────────

class VoucherLineIn(BaseModel):
    accountId: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str | None = None
    referenceNo: str | None = None


class VoucherIn(BaseModel):
    vtype: str
    date: Day | None = None
    headerAccountId: str | None = None
    referenceNo: str | None = None
    description: str | None = None
    chequeNo: str | None = None
    chequeDate: Day | None = None
    lines: list[VoucherLineIn] = []
    # Save and post in one go, for someone who may post.
    post: bool = False


class ReasonIn(BaseModel):
    reason: str | None = None
    date: Day | None = None


@router.get("/vouchers")
async def list_vouchers(
    type: str | None = None, status_: str | None = Query(None, alias="status"), auto: bool | None = None,
    from_: date | None = Query(None, alias="from"), to: date | None = None, q: str | None = None, accountId: str | None = None,
    accountKind: str | None = None, limit: int = 50, offset: int = 0, book: str | None = None, user: User = Depends(_vouchers),
) -> dict:
    """Only vouchers whose every line is in the person's areas. The Day Book is the whole book."""
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    items, total = await vouchers_service.list_vouchers(books, type, status_, auto, from_, to, q, accountId, min(max(limit, 1), 500), max(offset, 0),
                                                        access=await access_of(user), account_kind=accountKind)
    return {"items": items, "total": total}


@router.get("/vouchers/{voucher_id}")
async def get_voucher(voucher_id: str, user: User = Depends(_voucher_open)) -> dict:
    voucher = await _guard(vouchers_service.get, voucher_id)
    await _books_for(user, voucher.book)
    # The Day Book already shows every voucher in full, so opening one from it isn't limited to the reader's areas.
    if not await has_permission(user, "accounts.day-book", "R"):
        await _guard(vouchers_service.check_seen, await access_of(user), voucher, "open")
    return await vouchers_service.voucher_out(voucher, with_source=True)


async def _can_post(user: User) -> bool:
    return await has_permission(user, "accounts.vouchers.post", "X")


@router.post("/vouchers")
async def create_voucher(payload: VoucherIn, user: User = Depends(_write)) -> dict:
    await _may_write(user, payload.vtype)
    if payload.post and not await _can_post(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can save vouchers but not post them. Save it, and someone who posts will.")
    access = await access_of(user)
    data = payload.model_dump(mode="json")
    voucher = await _guard(vouchers_service.create_draft, user, data, access)
    if payload.post:
        voucher = await _guard(vouchers_service.post, user, str(voucher.id), access)
    return await vouchers_service.voucher_out(voucher)


@router.put("/vouchers/{voucher_id}")
async def update_voucher(voucher_id: str, payload: VoucherIn, user: User = Depends(_write)) -> dict:
    existing = await Voucher.get_or_none(id=voucher_id)
    await _may_write(user, existing.vtype if existing else payload.vtype)
    if payload.post and not await _can_post(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can save vouchers but not post them.")
    access = await access_of(user)
    voucher = await _guard(vouchers_service.update_draft, user, voucher_id, payload.model_dump(mode="json"), access)
    if payload.post:
        voucher = await _guard(vouchers_service.post, user, voucher_id, access)
    return await vouchers_service.voucher_out(voucher)


@router.post("/vouchers/{voucher_id}/post")
async def post_voucher(voucher_id: str, user: User = Depends(_post)) -> dict:
    return await vouchers_service.voucher_out(await _guard(vouchers_service.post, user, voucher_id, await access_of(user)))


@router.post("/vouchers/{voucher_id}/cancel")
async def cancel_voucher(voucher_id: str, payload: ReasonIn, user: User = Depends(_write)) -> dict:
    existing = await Voucher.get_or_none(id=voucher_id)
    await _may_write(user, existing.vtype if existing else None)
    return await vouchers_service.voucher_out(await _guard(vouchers_service.cancel_draft, user, voucher_id, payload.reason, await access_of(user)))


@router.post("/vouchers/{voucher_id}/reverse")
async def reverse_voucher(voucher_id: str, payload: ReasonIn, user: User = Depends(_reverse)) -> dict:
    reversal = await _guard(vouchers_service.reverse, user, voucher_id, payload.date, payload.reason or "", await access_of(user))
    return await vouchers_service.voucher_out(reversal)


# ── reports ────────────────────────────────────────────────────────────────────────────────────

@router.get("/ledger")
async def ledger(accountId: str, from_: date | None = Query(None, alias="from"), to: date | None = None, pdc: bool = False, user: User = Depends(_ledger)) -> dict:
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    account = await Account.get_or_none(id=accountId)
    if account:
        await _books_for(user, account.book)
        await _guard(accounts_areas.check_ledger, await access_of(user), account)
    return await _guard(accounts_reports_service.ledger, accountId, start, end, pdc)


@router.get("/trial-balance")
async def trial_balance(from_: date | None = Query(None, alias="from"), to: date | None = None, group: str | None = None, zero: bool = False,
                        category: str | None = None, book: str | None = None, user: User = Depends(_trial_balance)) -> dict:
    """`group` and `category` each take one code or several separated by commas."""
    books, together = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await accounts_reports_service.trial_balance(books, together, start, end, group, zero, category)


@router.get("/income-statement")
async def income_statement(from_: date | None = Query(None, alias="from"), to: date | None = None, book: str | None = None, user: User = Depends(_income_statement)) -> dict:
    books, together = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    end = to or shop_day()
    start = from_ or end.replace(day=1)
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The start date is after the end date.")
    return await accounts_reports_service.income_statement(books, together, start, end)


@router.get("/month-by-month")
async def month_by_month(from_: date | None = Query(None, alias="from"), to: date | None = None, book: str | None = None, user: User = Depends(_month_by_month)) -> dict:
    books, _together = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    end = to or shop_day()
    if from_:
        start = from_
    else:
        starts = [row.books_start for row in [await vouchers_service.settings(b) for b in books] if row.books_start]
        start = min(starts) if starts else end.replace(day=1)
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The start date is after the end date.")
    if (end.year - start.year) * 12 + end.month - start.month >= 24:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick up to 24 months.")
    return await accounts_reports_service.month_by_month(books, start, end)


@router.get("/balance-sheet")
async def balance_sheet(asOf: date | None = None, book: str | None = None, user: User = Depends(_balance_sheet)) -> dict:
    books, together = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
    return await accounts_reports_service.balance_sheet(books, together, asOf or shop_day(), settings.fiscal_start_month)


@router.get("/day-book")
async def day_book(from_: date | None = Query(None, alias="from"), to: date | None = None, type: str | None = None, limit: int = 100, offset: int = 0,
                   book: str | None = None, user: User = Depends(_day_book)) -> dict:
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    end = to or shop_day()
    start = from_ or end
    return await accounts_reports_service.day_book(books, start, end, type, min(max(limit, 1), 500), max(offset, 0))


@router.get("/statement")
async def statement(accountId: str, from_: date | None = Query(None, alias="from"), to: date | None = None, user: User = Depends(_statement)) -> dict:
    """A customer's or supplier's statement of account: Receivables or Payables (or the ledger), the book, and the party's area."""
    account = await Account.get_or_none(id=accountId)
    if not account:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That account doesn't exist.")
    screen = "accounts.receivables" if account.kind == "customer" else "accounts.payables"
    if not (await has_permission(user, screen, "R") or await has_permission(user, "accounts.ledger", "R")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "A statement for this party needs access to " + ("Receivables." if account.kind == "customer" else "Payables."))
    await _books_for(user, account.book)
    await _guard(accounts_areas.check_ledger, await access_of(user), account)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await _guard(accounts_reports_service.statement, accountId, start, end)


@router.get("/ageing")
async def ageing(kind: str = "supplier", asOf: date | None = None, book: str | None = None, user: User = Depends(get_current_user)) -> dict:
    if kind == "supplier" and not await has_permission(user, "accounts.payables", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seeing what suppliers are owed needs access to Payables.")
    if kind != "supplier" and not await has_permission(user, "accounts.receivables", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seeing what customers owe needs access to Receivables.")
    books, _ = await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return await _guard(accounts_reports_service.ageing, books, kind, asOf or shop_day())


@router.get("/dashboard")
async def dashboard(book: str | None = None, user: User = Depends(_desk)) -> dict:
    await _books_for(user, book)
    await accounts_posting_service.ensure_recent()
    return await accounts_reports_service.dashboard(book)


# ── settings, month end, posting ───────────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    fiscalStartMonth: int | None = None
    booksStart: Day | None = None
    tenderAccounts: dict[str, str | None] | None = None


class CloseIn(BaseModel):
    until: Day | None = None
    reason: str | None = None


def _settings_out(row) -> dict:
    return {
        "fiscalStartMonth": row.fiscal_start_month, "booksStart": row.books_start.isoformat() if row.books_start else None,
        "lockedUntil": row.locked_until.isoformat() if row.locked_until else None, "tenderAccounts": row.tender_accounts or {},
        "lastPostingAt": row.last_posting_at.isoformat() if row.last_posting_at else None, "lastPostingNote": row.last_posting_note,
        "postingProblems": row.posting_problems or [], "tenderDefaults": {},
    }


@router.get("/settings")
async def get_settings(book: str | None = None, user: User = Depends(_settings_read)) -> dict:
    books, together = await _books_for(user, book)
    return {**_settings_out(await vouchers_service.settings(books[0] if not together else HEAD_OFFICE_BOOK)), "book": books[0] if not together else HEAD_OFFICE_BOOK}


@router.patch("/settings")
async def update_settings(payload: SettingsIn, user: User = Depends(_settings)) -> dict:
    row = await vouchers_service.settings()
    repost = False
    if payload.fiscalStartMonth is not None:
        if not 1 <= payload.fiscalStartMonth <= 12:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The financial year starts in a month from 1 to 12.")
        row.fiscal_start_month = payload.fiscalStartMonth
    if payload.booksStart is not None and payload.booksStart != row.books_start:
        if row.locked_until:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Months are closed. Reopen them before moving the books' start.")
        if payload.booksStart > shop_day():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The books can't start in the future.")
        row.books_start = payload.booksStart
        repost = True
    if payload.tenderAccounts is not None:
        cleaned = {}
        for code, account_id in payload.tenderAccounts.items():
            if not account_id:
                continue
            account = await Account.get_or_none(id=account_id)
            if not account or account.book != HEAD_OFFICE_BOOK or account.kind not in ("cash", "bank", "wallet"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{code} money has to land in a cash, bank or wallet account.")
            cleaned[code.upper()] = account_id
        if cleaned != (row.tender_accounts or {}):
            row.tender_accounts = cleaned
            repost = True
    await row.save()
    if repost:
        await accounts_posting_service.run(full=True)
    return _settings_out(await vouchers_service.settings())


@router.post("/close-month")
async def close_month(payload: CloseIn, user: User = Depends(_period)) -> dict:
    row = await vouchers_service.settings()
    until = payload.until
    if until is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick the last day to close.")
    if until >= shop_day():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only days that are over can be closed.")
    if row.locked_until and until <= row.locked_until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The books are already closed up to {row.locked_until:%d %b %Y}.")
    drafts = await Voucher.filter(book=HEAD_OFFICE_BOOK, status="draft", date__lte=until).count()
    if drafts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{drafts} draft voucher{'s are' if drafts != 1 else ' is'} dated in that period. Post or cancel them first.")
    await accounts_posting_service.run(full=False)
    row = await vouchers_service.settings()
    row.locked_until = until
    await row.save()
    await _record_period(user, f"Closed the books up to {until:%d %b %Y}")
    return _settings_out(row)


@router.post("/reopen")
async def reopen(payload: CloseIn, user: User = Depends(_period)) -> dict:
    row = await vouchers_service.settings()
    if not row.locked_until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No month is closed.")
    if not (payload.reason or "").strip() or len(payload.reason.strip()) < 10:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Say why the books are being reopened. It stays on the record.")
    until = payload.until
    if until is not None and until >= row.locked_until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reopening keeps the books closed up to an earlier day, or none.")
    old = row.locked_until
    row.locked_until = until
    await row.save()
    await _record_period(user, f"Reopened the books (were closed to {old:%d %b %Y}, now {'open' if until is None else 'closed to ' + until.strftime('%d %b %Y')}): {payload.reason.strip()}")
    await accounts_posting_service.run(full=True)
    return _settings_out(await vouchers_service.settings())


@router.get("/period-history")
async def period_history(book: str | None = None, user: User = Depends(_settings_read)) -> list[dict]:
    """Head office's closes and reopens, newest first, from the notices sent when they happened. A branch keeps its own."""
    books, together = await _books_for(user, book)
    if not together and books != [HEAD_OFFICE_BOOK]:
        return []
    from app.models import Notice

    rows = await Notice.filter(kind="accounts.period").order_by("-at").limit(200)
    return [{"at": n.at.isoformat(), "what": n.title, "by": (n.body or "").removeprefix("By ") or None,
             "reopened": n.title.startswith("Reopened")} for n in rows]


async def _record_period(user: User, text: str) -> None:
    from app.services import alerts_service

    await alerts_service.notify("accounts.period", text, body=f"By {user.name}", link="/accounts/settings",
                                audience_any=[("accounts.period", "X"), ("accounts.settings", "R")], tone="warning")


class RunIn(BaseModel):
    full: bool = False


@router.post("/posting/run")
async def run_posting(payload: RunIn, user: User = Depends(_post_now)) -> dict:
    if payload.full:
        if not await has_permission(user, "accounts.period", "X"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Re-posting everything needs the right to close months.")
    return await accounts_posting_service.run(full=payload.full)


@router.get("/opening-suggestion")
async def opening_suggestion(user: User = Depends(_opening)) -> dict:
    """Only lines for accounts the person may use; the rest is theirs to leave to someone who can."""
    suggestion = await accounts_posting_service.opening_suggestion()
    access = await access_of(user)
    if not access.uses_everything:
        areas = await accounts_areas.areas_by_account([HEAD_OFFICE_BOOK])
        suggestion["lines"] = [line for line in suggestion["lines"] if access.can_use(*areas.get(line["accountId"], (None, None)))]
    return suggestion
