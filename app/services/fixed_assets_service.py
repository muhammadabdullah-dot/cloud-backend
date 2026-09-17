"""Head office's fixed asset register (book HO), its schedules, the monthly depreciation run and disposals. The same rules
as a branch's register; a branch keeps its own register at the branch.

How depreciation is worked out, in the words the screen uses:
- Life counts from the month the asset was bought. Depreciation charged before the month depreciation starts here is
  its opening accumulated depreciation (for something owned before the books started).
- Straight line: cost less salvage value less opening accumulated depreciation, spread evenly over the months of life
  left, each month rounded to the paisa and the last month taking the difference.
- Reducing balance: each year (twelve months from when depreciation starts here) charges the net book value at the
  start of that year times the yearly rate, in twelve equal months.
- A full month's charge from the month depreciation starts, whatever day it was bought. Nothing in the month it is
  disposed of, or after.
- Never below salvage value, and nothing after the end of its life.

The register never writes into the books by itself: a month's run and a disposal are draft journals that someone who
may post checks and posts. Whether they count is read from those vouchers (refresh): posted counts; a cancelled or
reversed voucher frees the month, or puts the asset back in use.
"""
from datetime import date, timedelta
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import atomic

from app.models import (
    HEAD_OFFICE_BOOK,
    Account,
    AccountCategory,
    AccountGroup,
    DepreciationRun,
    DepreciationRunLine,
    FixedAsset,
    User,
    Voucher,
    next_value,
)
from app.services import accounts_areas, accounts_chart_service, vouchers_service
from app.services.accounts_chart_service import gid, money
from app.services.accounts_reports_service import shop_day

ZERO = Decimal("0")
BOOK = HEAD_OFFICE_BOOK
METHODS = {"straight": "Straight line", "reducing": "Reducing balance"}
DEPRECIATION_KEY = "expense.depreciation"
DISPOSAL_KEY = "income.asset_disposal"
DISPOSAL_NAME = "GAIN OR LOSS ON DISPOSAL OF ASSETS"
FIXED_GROUP, ACCUMULATED_GROUP, EXPENSE_GROUP, OTHER_INCOME_GROUP = "1201", "1202", "5201", "4201"
MONEY_KINDS = ("cash", "bank", "wallet")
# What can still change on an asset once depreciation has been charged on it.
TEXT_FIELDS = ("name", "location", "supplier_ref", "notes")


class AssetError(Exception):
    def __init__(self, message: str):
        self.message = message


# ── months ───────────────────────────────────────────────────────────────────────────────────────

def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, count: int) -> date:
    index = day.year * 12 + day.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def months_between(first: date, later: date) -> int:
    return (later.year - first.year) * 12 + later.month - first.month


def month_end(day: date) -> date:
    return add_months(month_start(day), 1) - timedelta(days=1)


def label(day: date) -> str:
    return f"{day:%b %Y}"


def parse_month(value) -> date:
    if isinstance(value, date):
        return month_start(value)
    text = str(value or "").strip()
    try:
        return month_start(date.fromisoformat(text[:10] if len(text) >= 10 else f"{text[:7]}-01"))
    except ValueError as exc:
        raise AssetError("Pick a month.") from exc


def parse_day(value, what: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise AssetError(f"Enter the {what}.") from exc


def _s(value) -> str:
    return format(money(value), "f")


def _ref(account: Account | None) -> dict | None:
    return {"id": str(account.id), "code": account.code, "name": account.name} if account else None


# ── the accounts the register needs ──────────────────────────────────────────────────────────────

async def _ensure_key(key: str, name: str, group: str) -> Account:
    account = await Account.get_or_none(book=BOOK, system_key=key)
    if account is not None:
        return account
    await accounts_chart_service.ensure_standard_chart(BOOK)
    account = await Account.filter(book=BOOK, system_key=None, group_id=gid(BOOK, group), name__iexact=name).order_by("code").first()
    if account is None:
        return await Account.create(
            book=BOOK, code=await accounts_chart_service.next_account_code(BOOK, group), name=name, group_id=gid(BOOK, group),
            sub_group_id=gid(BOOK, f"{group}01"), system_key=key, standard=True,
        )
    account.system_key = key
    await account.save(update_fields=["system_key", "updated_at"])
    return account


async def ensure_accounts() -> tuple[Account, Account]:
    """DEPRECIATION carries the key the register posts to, and there is an account for gains and losses on disposal,
    in head office's book. Branches do the same in theirs, and their charts arrive here by sync."""
    return (await _ensure_key(DEPRECIATION_KEY, "DEPRECIATION", EXPENSE_GROUP),
            await _ensure_key(DISPOSAL_KEY, DISPOSAL_NAME, OTHER_INCOME_GROUP))


async def _group_types() -> dict[str, str]:
    """Group id ("HO:1201") -> account type (1 assets … 5 expenses)."""
    types = {c.code: c.type_id for c in await AccountCategory.all()}
    return {g.id: types.get(g.category_id, "") for g in await AccountGroup.filter(book=BOOK)}


def _group_code(account: Account) -> str:
    return (account.group_id or "").split(":", 1)[-1]


async def pick_lists(access: accounts_areas.Access) -> dict:
    """The accounts the add, dispose and run screens offer, as far as the person's areas go."""
    types = await _group_types()
    areas = await accounts_areas.areas_by_account([BOOK])
    rows = await Account.filter(book=BOOK, active=True).order_by("code")

    def seen(a: Account) -> bool:
        return access.can_see(*areas.get(str(a.id), (None, None)))

    fixed = [a for a in rows if seen(a) and areas[str(a.id)][0] == "fixed-assets" and types.get(a.group_id) == "1"]
    return {
        "categories": [_ref(a) for a in fixed if _group_code(a) != ACCUMULATED_GROUP],
        "accumulated": [_ref(a) for a in fixed if _group_code(a) == ACCUMULATED_GROUP],
        "expense": [_ref(a) for a in sorted((a for a in rows if seen(a) and types.get(a.group_id) == "5"),
                                            key=lambda a: (a.system_key != DEPRECIATION_KEY, a.code))],
        "money": [{**_ref(a), "kind": a.kind} for a in rows if seen(a) and a.kind in MONEY_KINDS],
    }


# ── the schedule ─────────────────────────────────────────────────────────────────────────────────

def schedule_rows(asset: FixedAsset) -> list[tuple[date, Decimal, Decimal, Decimal]]:
    """(month, charge, accumulated depreciation at the month's end, net book value at the month's end), month by month
    from when depreciation starts to the end of its life. Worked out from the asset alone."""
    cost, salvage, opening = money(asset.cost), money(asset.salvage_value), money(asset.opening_accumulated)
    start = month_start(asset.depreciation_start)
    left = int(asset.useful_life_months) - months_between(month_start(asset.purchase_date), start)
    accumulated = opening
    rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    if left <= 0 or cost - salvage - opening <= 0:
        return rows
    if asset.method == "reducing":
        rate = Decimal(str(asset.rate_percent or 0)) / Decimal("100")
        each = ZERO
        for index in range(left):
            if index % 12 == 0:
                each = money((cost - accumulated) * rate / Decimal("12"))
            charge = min(each, cost - salvage - accumulated)
            if charge <= 0:
                break
            accumulated += charge
            rows.append((add_months(start, index), charge, accumulated, cost - accumulated))
        return rows
    each = money((cost - salvage - opening) / Decimal(left))
    for index in range(left):
        remaining = cost - salvage - accumulated
        charge = remaining if index == left - 1 else min(each, remaining)
        if charge <= 0:
            break
        accumulated += charge
        rows.append((add_months(start, index), charge, accumulated, cost - accumulated))
    return rows


def charge_for(asset: FixedAsset, month: date) -> tuple[Decimal, Decimal]:
    """The month's charge and the accumulated depreciation at its end, by the schedule. Nothing once it's disposed of."""
    if asset.disposal_date and month_start(asset.disposal_date) <= month:
        return ZERO, ZERO
    for row_month, charge, accumulated, _nbv in schedule_rows(asset):
        if row_month == month:
            return charge, accumulated
    return ZERO, ZERO


# ── runs and disposals follow their vouchers ─────────────────────────────────────────────────────

def voucher_state(voucher: Voucher | None) -> str:
    if voucher is None or voucher.status == "cancelled":
        return "cancelled"
    if voucher.status == "posted" and voucher.reversed:
        return "reversed"
    return voucher.status


async def refresh() -> None:
    """Bring runs and disposals in line with their vouchers, which are posted, cancelled and reversed elsewhere."""
    for run in await DepreciationRun.filter(book=BOOK, status__in=["draft", "posted"]).prefetch_related("voucher"):
        state = voucher_state(run.voucher)
        if state != run.status:
            run.status = state
            await run.save(update_fields=["status", "updated_at"])
    for asset in await FixedAsset.filter(book=BOOK, disposal_date__isnull=False).prefetch_related("disposal_voucher"):
        state = voucher_state(asset.disposal_voucher)
        if state in ("cancelled", "reversed"):
            asset.status, asset.disposal_date, asset.disposal_proceeds = "in_use", None, None
            asset.disposal_account_id = None
            asset.disposal_voucher_id = None
            await asset.save()
        elif state == "posted" and asset.status != "disposed":
            asset.status = "disposed"
            await asset.save(update_fields=["status", "updated_at"])


async def _run_lines(asset_ids: list[str] | None = None) -> dict[str, list[dict]]:
    """Every live run line by asset: month, amount, the run's state and its voucher."""
    qs = DepreciationRunLine.filter(run__status__in=["draft", "posted"])
    if asset_ids is not None:
        qs = qs.filter(asset_id__in=asset_ids)
    rows = await qs.values("asset_id", "amount", "run_id", "run__month", "run__status", "run__voucher_id")
    numbers = {str(v["id"]): v["number"] for v in await Voucher.filter(id__in=list({r["run__voucher_id"] for r in rows if r["run__voucher_id"]})).values("id", "number")}
    out: dict[str, list[dict]] = {}
    for r in rows:
        month = r["run__month"] if isinstance(r["run__month"], date) else date.fromisoformat(str(r["run__month"])[:10])
        out.setdefault(str(r["asset_id"]), []).append({
            "month": month, "amount": money(r["amount"]), "status": r["run__status"], "runId": str(r["run_id"]),
            "voucherId": str(r["run__voucher_id"]) if r["run__voucher_id"] else None, "number": numbers.get(str(r["run__voucher_id"])),
        })
    return out


def booked(asset: FixedAsset, lines: list[dict]) -> Decimal:
    """Accumulated depreciation as the books have it: the opening figure and every posted run."""
    return money(asset.opening_accumulated) + sum((l["amount"] for l in lines if l["status"] == "posted"), ZERO)


def _state(asset: FixedAsset) -> str:
    if asset.status == "disposed":
        return "disposed"
    return "disposing" if asset.disposal_voucher_id else "in_use"


def asset_out(asset: FixedAsset, lines: list[dict], today: date) -> dict:
    schedule = schedule_rows(asset)
    this_month = month_start(today)
    charged = {l["month"] for l in lines}
    disposed_from = month_start(asset.disposal_date) if asset.disposal_date else None
    behind = [(m, c) for m, c, _a, _n in schedule if m < this_month and m not in charged and not (disposed_from and m >= disposed_from)]
    accumulated = booked(asset, lines)
    voucher = asset.disposal_voucher if asset.disposal_voucher_id else None
    return {
        "id": str(asset.id), "code": asset.code, "name": asset.name,
        "categoryAccount": _ref(asset.category_account), "accumulatedAccount": _ref(asset.accumulated_account), "expenseAccount": _ref(asset.expense_account),
        "purchaseDate": asset.purchase_date.isoformat(), "cost": _s(asset.cost), "salvageValue": _s(asset.salvage_value),
        "usefulLifeMonths": asset.useful_life_months, "method": asset.method, "methodLabel": METHODS.get(asset.method, asset.method),
        "ratePercent": format(Decimal(str(asset.rate_percent)), "f") if asset.rate_percent is not None else None,
        "depreciationStart": f"{asset.depreciation_start:%Y-%m}", "openingAccumulated": _s(asset.opening_accumulated),
        "location": asset.location, "supplierRef": asset.supplier_ref, "notes": asset.notes,
        "status": _state(asset), "disposalDate": asset.disposal_date.isoformat() if asset.disposal_date else None,
        "disposalProceeds": _s(asset.disposal_proceeds) if asset.disposal_proceeds is not None else None,
        "disposalAccount": _ref(asset.disposal_account) if asset.disposal_account_id else None,
        "disposalVoucher": {"id": str(voucher.id), "number": voucher.number, "status": voucher.status} if voucher else None,
        "accumulated": _s(accumulated), "netBookValue": _s(money(asset.cost) - accumulated),
        "thisMonth": _s(next((c for m, c, _a, _n in schedule if m == this_month), ZERO)) if _state(asset) == "in_use" else "0.00",
        "notCharged": _s(sum((c for _m, c in behind), ZERO)), "notChargedMonths": [label(m) for m, _c in behind][:12],
        "lifeEnds": f"{add_months(month_start(asset.purchase_date), int(asset.useful_life_months) - 1):%Y-%m}",
        "lastCharge": f"{schedule[-1][0]:%Y-%m}" if schedule else None,
        "locked": bool(lines), "createdBy": asset.created_by_name,
    }


# ── reading the register ─────────────────────────────────────────────────────────────────────────

async def _ledger(account_ids: list[str]) -> dict[str, Decimal]:
    """Debit less credit on each account, over everything posted whatever its date."""
    if not account_ids:
        return {}
    rows = await Tortoise.get_connection("default").execute_query_dict(
        "SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE v.status = 'posted' AND l.account_id IN ({','.join('?' for _ in account_ids)}) GROUP BY l.account_id", account_ids,
    )
    return {str(r["account_id"]): money(Decimal(str(r["dr"] or 0))) - money(Decimal(str(r["cr"] or 0))) for r in rows}


async def _loaded(qs) -> list[FixedAsset]:
    return await qs.prefetch_related("category_account", "accumulated_account", "expense_account", "disposal_account", "disposal_voucher")


async def register(access: accounts_areas.Access) -> dict:
    await refresh()
    await ensure_accounts()
    today = shop_day()
    areas = await accounts_areas.areas_by_account([BOOK])

    def seen(account_id) -> bool:
        return access.can_see(*areas.get(str(account_id), (None, None)))

    everything = await _loaded(FixedAsset.filter(book=BOOK).order_by("code"))
    lines = await _run_lines()
    assets = [a for a in everything if seen(a.category_account_id)]
    rows = [asset_out(a, lines.get(str(a.id), []), today) for a in assets]

    categories: dict[str, dict] = {}
    for asset, row in zip(assets, rows):
        if row["status"] == "disposed":
            continue
        cat = categories.setdefault(str(asset.category_account_id), {"account": row["categoryAccount"], "assets": 0, "cost": ZERO, "accumulated": ZERO, "netBookValue": ZERO, "thisMonth": ZERO})
        cat["assets"] += 1
        for key in ("cost", "accumulated", "netBookValue", "thisMonth"):
            cat[key] += Decimal(row[key])
    totals = {key: sum((c[key] for c in categories.values()), ZERO) for key in ("cost", "accumulated", "netBookValue", "thisMonth")}

    # The register against the books: the fixed asset accounts' balances and the accumulated depreciation accounts'.
    all_accounts = await Account.filter(book=BOOK)
    fixed_ids = {str(a.id) for a in all_accounts if _group_code(a) == FIXED_GROUP} | {str(a.category_account_id) for a in everything}
    accumulated_ids = {str(a.id) for a in all_accounts if _group_code(a) == ACCUMULATED_GROUP} | {str(a.accumulated_account_id) for a in everything}
    ledger = await _ledger(sorted(fixed_ids | accumulated_ids))
    by_id = {str(a.id): a for a in all_accounts}
    register_cost: dict[str, Decimal] = {}
    register_accumulated: dict[str, Decimal] = {}
    for asset in everything:
        if asset.status == "disposed":
            continue
        register_cost[str(asset.category_account_id)] = register_cost.get(str(asset.category_account_id), ZERO) + money(asset.cost)
        register_accumulated[str(asset.accumulated_account_id)] = register_accumulated.get(str(asset.accumulated_account_id), ZERO) + booked(asset, lines.get(str(asset.id), []))
    checks = []
    for kind, ids, on_register, sign in (("cost", fixed_ids, register_cost, 1), ("accumulated", accumulated_ids, register_accumulated, -1)):
        for account_id in sorted(ids, key=lambda i: by_id[i].code if i in by_id else ""):
            books = sign * ledger.get(account_id, ZERO)
            mine = on_register.get(account_id, ZERO)
            if (books or mine) and account_id in by_id and seen(account_id):
                checks.append({"kind": kind, "account": _ref(by_id[account_id]), "register": _s(mine), "books": _s(books),
                               "difference": _s(books - mine), "ok": books == mine})
    unregistered = [{"account": c["account"], "inBooks": c["books"], "onRegister": c["register"], "notOnRegister": c["difference"]}
                    for c in checks if c["kind"] == "cost" and Decimal(c["difference"]) > 0]
    accumulated_books = sum((Decimal(c["books"]) for c in checks if c["kind"] == "accumulated"), ZERO)
    accumulated_register = sum((Decimal(c["register"]) for c in checks if c["kind"] == "accumulated"), ZERO)
    settings = await vouchers_service.settings()
    return {
        "today": today.isoformat(), "thisMonth": f"{today:%Y-%m}",
        "assets": rows, "hiddenAssets": len(everything) - len(assets),
        "categories": [{**c, **{k: _s(c[k]) for k in ("cost", "accumulated", "netBookValue", "thisMonth")}}
                       for c in sorted(categories.values(), key=lambda c: c["account"]["code"])],
        "totals": {k: _s(v) for k, v in totals.items()},
        "checks": checks, "agrees": all(c["ok"] for c in checks),
        "unregistered": unregistered,
        "accumulatedNotOnRegister": _s(accumulated_books - accumulated_register),
        "runs": await runs_out(access),
        "pick": await pick_lists(access),
        "booksStart": settings.books_start.isoformat() if settings.books_start else None,
        "lockedUntil": settings.locked_until.isoformat() if settings.locked_until else None,
    }


async def runs_out(access: accounts_areas.Access, limit: int = 36) -> list[dict]:
    if not access.can_see("fixed-assets"):
        return []
    runs = await DepreciationRun.filter(book=BOOK).order_by("-month", "-created_at").limit(limit).prefetch_related("voucher")
    counts = {}
    for row in await DepreciationRunLine.filter(run_id__in=[r.id for r in runs]).values("run_id"):
        counts[str(row["run_id"])] = counts.get(str(row["run_id"]), 0) + 1
    return [{
        "id": str(r.id), "month": f"{r.month:%Y-%m}", "label": label(r.month), "status": r.status, "total": _s(r.total),
        "assets": counts.get(str(r.id), 0), "createdBy": r.created_by_name, "createdAt": r.created_at.isoformat() if r.created_at else None,
        "voucher": {"id": str(r.voucher.id), "number": r.voucher.number, "date": r.voucher.date.isoformat(), "status": r.voucher.status} if r.voucher else None,
    } for r in runs]


async def get_asset(asset_id: str, access: accounts_areas.Access) -> FixedAsset:
    try:
        asset = await _loaded(FixedAsset.filter(id=asset_id, book=BOOK))
    except (ValueError, TypeError):
        asset = []
    if not asset:
        raise AssetError("That asset isn't on the register.")
    area, key = (await accounts_areas.areas_by_account([BOOK])).get(str(asset[0].category_account_id), (None, None))
    if not access.can_see(area, key):
        raise accounts_areas.refuse(f"You can't see {asset[0].category_account.name}.", area)
    return asset[0]


async def detail(asset_id: str, access: accounts_areas.Access) -> dict:
    await refresh()
    asset = await get_asset(asset_id, access)
    lines = (await _run_lines([str(asset.id)])).get(str(asset.id), [])
    today = shop_day()
    return {"asset": asset_out(asset, lines, today), "schedule": schedule_out(asset, lines, today)}


def schedule_out(asset: FixedAsset, lines: list[dict], today: date) -> list[dict]:
    by_month = {l["month"]: l for l in lines}
    this_month = month_start(today)
    disposed_from = month_start(asset.disposal_date) if asset.disposal_date else None
    out = []
    for month, charge, accumulated, nbv in schedule_rows(asset):
        line = by_month.get(month)
        if line:
            state = line["status"]
        elif disposed_from and month >= disposed_from:
            state = "disposed"
        elif month <= this_month:
            state = "not-charged"
        else:
            state = "to-come"
        out.append({"month": f"{month:%Y-%m}", "label": label(month), "charge": _s(charge), "accumulated": _s(accumulated), "netBookValue": _s(nbv),
                    "state": state, "voucher": {"id": line["voucherId"], "number": line["number"]} if line and line["voucherId"] else None})
    return out


# ── adding and changing assets ───────────────────────────────────────────────────────────────────

def _text(value, limit: int) -> str | None:
    value = (str(value) if value is not None else "").strip()
    return value[:limit] if value else None


def _amount(value, what: str, allow_zero: bool = True) -> Decimal:
    try:
        amount = money(value)
    except Exception as exc:  # noqa: BLE001 - anything that isn't a number
        raise AssetError(f"Enter the {what} as a number.") from exc
    if amount < 0 or (not allow_zero and amount == 0):
        raise AssetError(f"The {what} has to be more than nothing." if not allow_zero else f"The {what} can't be negative.")
    return amount


async def _find(account_id) -> Account | None:
    try:
        return await Account.get_or_none(id=account_id, book=BOOK) if account_id else None
    except (ValueError, TypeError):
        return None


async def _account(account_id, what: str) -> Account:
    account = await _find(account_id)
    if account is None:
        raise AssetError(f"Pick the {what}.")
    if not account.active:
        raise AssetError(f"{account.name} is switched off.")
    return account


async def _check_accounts(access: accounts_areas.Access, category: Account, accumulated: Account, expense: Account) -> None:
    types = await _group_types()
    areas = await accounts_areas.areas_by_account([BOOK])
    if areas.get(str(category.id), (None,))[0] != "fixed-assets" or types.get(category.group_id) != "1" or _group_code(category) == ACCUMULATED_GROUP:
        raise AssetError(f"{category.name} isn't a fixed asset account. Pick one under FIXED ASSETS.")
    if _group_code(accumulated) != ACCUMULATED_GROUP:
        raise AssetError(f"{accumulated.name} isn't an accumulated depreciation account. Pick one under ACCUMULATED DEPRECIATION.")
    if types.get(expense.group_id) != "5":
        raise AssetError(f"{expense.name} isn't an expense account.")
    for account in (category, accumulated, expense):
        area, key = areas.get(str(account.id), (None, None))
        if not access.can_use(area, key):
            raise accounts_areas.refuse(f"You can't put assets on {account.name}.", area)


async def _fields(payload: dict, access: accounts_areas.Access, current: FixedAsset | None = None) -> dict:
    """Everything an asset is saved with, checked. A field left out keeps what the asset has."""
    def pick(key: str, attr: str):
        return payload[key] if key in payload and payload[key] is not None else (getattr(current, attr) if current else None)

    depreciation, _disposal = await ensure_accounts()
    name = _text(pick("name", "name"), 160)
    if not name:
        raise AssetError("Give the asset a name.")
    category = await _account(pick("categoryAccountId", "category_account_id"), "fixed asset account it sits in")
    accumulated_id = pick("accumulatedAccountId", "accumulated_account_id")
    if not accumulated_id:
        first = await Account.filter(book=BOOK, group_id=gid(BOOK, ACCUMULATED_GROUP), active=True).order_by("code").first()
        accumulated_id = first.id if first else None
    accumulated = await _account(accumulated_id, "accumulated depreciation account")
    expense = await _account(pick("expenseAccountId", "expense_account_id") or depreciation.id, "depreciation expense account")
    await _check_accounts(access, category, accumulated, expense)

    purchase_date = parse_day(pick("purchaseDate", "purchase_date"), "day it was bought")
    if purchase_date > shop_day():
        raise AssetError("The day it was bought can't be in the future.")
    cost = _amount(pick("cost", "cost"), "cost", allow_zero=False)
    salvage = _amount(pick("salvageValue", "salvage_value") or 0, "salvage value")
    if salvage >= cost:
        raise AssetError("The salvage value has to be less than the cost.")
    try:
        life = int(pick("usefulLifeMonths", "useful_life_months") or 0)
    except (TypeError, ValueError) as exc:
        raise AssetError("Enter its useful life in months.") from exc
    if not 1 <= life <= 1200:
        raise AssetError("Its useful life is a number of months, from 1 to 1200.")
    method = str(pick("method", "method") or "straight")
    if method not in METHODS:
        raise AssetError("Pick straight line or reducing balance.")
    rate = None
    if method == "reducing":
        try:
            rate = Decimal(str(pick("ratePercent", "rate_percent") or 0)).quantize(Decimal("0.01"))
        except Exception as exc:  # noqa: BLE001
            raise AssetError("Enter the yearly rate as a percentage.") from exc
        if not Decimal("0") < rate <= Decimal("100"):
            raise AssetError("The yearly rate for reducing balance is a percentage above 0 and up to 100.")
    start_value = payload.get("depreciationStart") if payload.get("depreciationStart") else (current.depreciation_start if current else purchase_date)
    start = parse_month(start_value)
    if start < month_start(purchase_date):
        raise AssetError("Depreciation can't start before the month it was bought.")
    opening = _amount(pick("openingAccumulated", "opening_accumulated") or 0, "opening accumulated depreciation")
    if opening > cost - salvage:
        raise AssetError("The opening accumulated depreciation can't be more than the cost less the salvage value.")
    return {
        "name": name, "category_account": category, "accumulated_account": accumulated, "expense_account": expense,
        "purchase_date": purchase_date, "cost": cost, "salvage_value": salvage, "useful_life_months": life, "method": method,
        "rate_percent": rate, "depreciation_start": start, "opening_accumulated": opening,
        "location": _text(pick("location", "location"), 120), "supplier_ref": _text(pick("supplierRef", "supplier_ref"), 120),
        "notes": _text(pick("notes", "notes"), 500),
    }


@atomic()
async def create(user: User, payload: dict, access: accounts_areas.Access) -> FixedAsset:
    fields = await _fields(payload, access)
    seq = await next_value("fixed-asset", 1)
    code = f"FA-{seq:04d}"
    while await FixedAsset.exists(book=BOOK, code=code):
        seq = await next_value("fixed-asset", 1)
        code = f"FA-{seq:04d}"
    return await FixedAsset.create(book=BOOK, code=code, created_by_name=user.name, **fields)


@atomic()
async def update(user: User, asset_id: str, payload: dict, access: accounts_areas.Access) -> FixedAsset:
    await refresh()
    asset = await get_asset(asset_id, access)
    fields = await _fields(payload, access, asset)
    lines = (await _run_lines([str(asset.id)])).get(str(asset.id), [])
    if lines or asset.disposal_voucher_id:
        changed = []
        for key, value in fields.items():
            if key in TEXT_FIELDS:
                continue
            now = getattr(asset, f"{key}_id") if key.endswith("_account") else getattr(asset, key)
            wanted = value.id if key.endswith("_account") else value
            if isinstance(now, Decimal) or isinstance(wanted, Decimal):
                same = (now is None and wanted is None) or (now is not None and wanted is not None and money(now) == money(wanted))
            else:
                same = str(now) == str(wanted)
            if not same:
                changed.append(key)
        if changed:
            where = lines[0]["number"] if lines else (asset.disposal_voucher.number if asset.disposal_voucher else "a voucher")
            raise AssetError(f"Depreciation or a disposal is already in the books for {asset.code} ({where}), so its cost, dates, life, method and "
                             "accounts stay as they are. Its name, location, supplier and notes can still change.")
    for key, value in fields.items():
        setattr(asset, key, value)
    await asset.save()
    return asset


@atomic()
async def delete(asset_id: str, access: accounts_areas.Access) -> None:
    asset = await get_asset(asset_id, access)
    if await DepreciationRunLine.exists(asset_id=asset.id) or asset.disposal_voucher_id:
        raise AssetError(f"{asset.code} has been in a depreciation run or a disposal, so it stays on the register. Dispose of it instead.")
    area, key = (await accounts_areas.areas_by_account([BOOK])).get(str(asset.category_account_id), (None, None))
    if not access.can_use(area, key):
        raise accounts_areas.refuse(f"You can't take assets off {asset.category_account.name}.", area)
    await asset.delete()


def preview_schedule(payload: dict) -> list[dict]:
    """The schedule an asset would have, before it is saved: the figures only, no accounts."""
    try:
        purchase = parse_day(payload.get("purchaseDate"), "day it was bought")
        asset = FixedAsset(
            cost=_amount(payload.get("cost"), "cost", allow_zero=False), salvage_value=_amount(payload.get("salvageValue") or 0, "salvage value"),
            useful_life_months=int(payload.get("usefulLifeMonths") or 0), method=str(payload.get("method") or "straight"),
            rate_percent=Decimal(str(payload.get("ratePercent") or 0)), purchase_date=purchase,
            depreciation_start=parse_month(payload.get("depreciationStart") or purchase), opening_accumulated=_amount(payload.get("openingAccumulated") or 0, "opening accumulated depreciation"),
        )
    except (TypeError, ValueError) as exc:
        raise AssetError("Fill in the cost, the day it was bought and its life to see the schedule.") from exc
    return [{"month": f"{m:%Y-%m}", "label": label(m), "charge": _s(c), "accumulated": _s(a), "netBookValue": _s(n), "state": "to-come", "voucher": None}
            for m, c, a, n in schedule_rows(asset)]


# ── the monthly run ──────────────────────────────────────────────────────────────────────────────

async def run_preview(month_value, access: accounts_areas.Access) -> dict:
    """What the month's depreciation voucher would hold, and anything that stops it being prepared."""
    await refresh()
    await ensure_accounts()
    month = parse_month(month_value)
    end = month_end(month)
    today = shop_day()
    settings = await vouchers_service.settings()
    blockers: list[str] = []
    if month > month_start(today):
        blockers.append(f"{label(month)} hasn't started. Depreciation is prepared for this month or earlier.")
    if settings.books_start and end < settings.books_start:
        blockers.append(f"The books start on {settings.books_start:%d %b %Y}, after {label(month)}.")
    if settings.locked_until and end <= settings.locked_until:
        blockers.append(vouchers_service.closed_message(settings.locked_until))
    existing = await DepreciationRun.filter(book=BOOK, month=month, status__in=["draft", "posted"]).prefetch_related("voucher").first()
    if existing:
        number = existing.voucher.number if existing.voucher else "a voucher"
        blockers.append(f"{label(month)} is already prepared as {number}, a draft. Post it, or cancel it to prepare the month again."
                        if existing.status == "draft" else f"{label(month)} is already posted as {number}. Reverse it first to run the month again.")
    areas = await accounts_areas.areas_by_account([BOOK])
    lines, hidden = [], 0
    for asset in await _loaded(FixedAsset.filter(book=BOOK, status="in_use", depreciation_start__lte=month, purchase_date__lte=end).order_by("code")):
        charge, accumulated = charge_for(asset, month)
        if charge <= 0:
            continue
        if not all(access.can_see(*areas.get(str(i), (None, None))) for i in (asset.category_account_id, asset.accumulated_account_id, asset.expense_account_id)):
            hidden += 1
            continue
        lines.append({
            "asset": asset, "assetId": str(asset.id), "code": asset.code, "name": asset.name, "method": METHODS[asset.method],
            "categoryAccount": _ref(asset.category_account), "accumulatedAccount": _ref(asset.accumulated_account), "expenseAccount": _ref(asset.expense_account),
            "charge": charge, "accumulatedBefore": accumulated - charge, "accumulatedAfter": accumulated, "netBookValueAfter": money(asset.cost) - accumulated,
        })
    if hidden:
        blockers.append(f"{hidden} asset{'s are' if hidden != 1 else ' is'} on accounts outside your access, so you can't prepare this month. "
                        "Ask your manager for access to Fixed assets and depreciation.")
    if not lines and not existing and not hidden:
        blockers.append(f"Nothing to charge for {label(month)}: no asset on the register is being depreciated that month.")
    debit: dict[tuple[str, str], dict] = {}
    credit: dict[tuple[str, str], dict] = {}
    for line in lines:
        asset = line["asset"]
        for side, account in ((debit, asset.expense_account), (credit, asset.accumulated_account)):
            key = (asset.category_account.code, str(account.id))
            entry = side.setdefault(key, {"category": line["categoryAccount"], "account": _ref(account), "assets": 0, "amount": ZERO})
            entry["assets"] += 1
            entry["amount"] += line["charge"]
    voucher_lines = []
    for side, entries in (("debit", debit), ("credit", credit)):
        for (_code, _id), entry in sorted(entries.items()):
            count = entry["assets"]
            voucher_lines.append({
                "accountId": entry["account"]["id"], "account": entry["account"], "category": entry["category"],
                "debit": _s(entry["amount"]) if side == "debit" else "0.00", "credit": _s(entry["amount"]) if side == "credit" else "0.00",
                "description": f"Depreciation for {label(month)}: {entry['category']['name']} ({count} asset{'s' if count != 1 else ''})"[:255],
            })
    total = sum((l["charge"] for l in lines), ZERO)
    return {
        "month": f"{month:%Y-%m}", "label": label(month), "date": end.isoformat(), "total": _s(total), "blockers": blockers,
        "existing": {"id": str(existing.id), "status": existing.status, "voucher": {"id": str(existing.voucher.id), "number": existing.voucher.number} if existing.voucher else None} if existing else None,
        "assets": [{k: (_s(v) if isinstance(v, Decimal) else v) for k, v in l.items() if k != "asset"} for l in lines],
        "voucherLines": voucher_lines,
    }


@atomic()
async def prepare_run(user: User, month_value, access: accounts_areas.Access) -> DepreciationRun:
    preview = await run_preview(month_value, access)
    if preview["blockers"]:
        raise AssetError(preview["blockers"][0])
    month = parse_month(preview["month"])
    count = len(preview["assets"])
    voucher = await vouchers_service.create_draft(user, {
        "vtype": "JV", "date": preview["date"], "referenceNo": f"Depreciation {preview['label']}",
        "description": f"Depreciation for {preview['label']} from the fixed asset register: {count} asset{'s' if count != 1 else ''}",
        "lines": [{"accountId": l["accountId"], "debit": l["debit"], "credit": l["credit"], "description": l["description"]} for l in preview["voucherLines"]],
    }, access)
    run = await DepreciationRun.create(
        book=await vouchers_service.book_prefix(), month=month, voucher=voucher, status="draft", total=Decimal(preview["total"]),
        created_by=user, created_by_name=user.name,
    )
    for line in preview["assets"]:
        await DepreciationRunLine.create(run=run, asset_id=line["assetId"], amount=Decimal(line["charge"]), accumulated_after=Decimal(line["accumulatedAfter"]))
    return run


# ── disposal ─────────────────────────────────────────────────────────────────────────────────────

async def disposal_plan(asset_id: str, payload: dict, access: accounts_areas.Access) -> dict:
    """The disposal voucher an asset would get: what it cost, the depreciation charged on it, the money received, and
    the gain or loss. The disposal takes the depreciation actually charged (opening and posted runs)."""
    await refresh()
    _depreciation, disposal_account = await ensure_accounts()
    asset = await get_asset(asset_id, access)
    blockers: list[str] = []
    warnings: list[str] = []
    today = shop_day()
    if asset.status == "disposed":
        raise AssetError(f"{asset.code} was disposed of on {asset.disposal_date:%d %b %Y}.")
    if asset.disposal_voucher_id:
        number = asset.disposal_voucher.number if asset.disposal_voucher else "a voucher"
        raise AssetError(f"{asset.code} already has a disposal waiting to be posted ({number}). Post it, or cancel it to start again.")
    day = parse_day(payload.get("date") or today, "day it was disposed of")
    proceeds = _amount(payload.get("proceeds") or 0, "money received")
    money_account = None
    if payload.get("accountId"):
        money_account = await _find(payload["accountId"])
    settings = await vouchers_service.settings()
    if day < asset.purchase_date:
        blockers.append(f"It can't be disposed of before it was bought ({asset.purchase_date:%d %b %Y}).")
    if day > today:
        blockers.append("Date the disposal today or earlier.")
    if settings.books_start and day < settings.books_start:
        blockers.append(f"The books start on {settings.books_start:%d %b %Y}. Date the disposal on or after that.")
    if settings.locked_until and day <= settings.locked_until:
        blockers.append(vouchers_service.closed_message(settings.locked_until))
    if proceeds > 0 and (money_account is None or money_account.kind not in MONEY_KINDS or not money_account.active):
        blockers.append("Pick the cash or bank account the money went into.")
    lines = (await _run_lines([str(asset.id)])).get(str(asset.id), [])
    for line in sorted(lines, key=lambda l: l["month"]):
        if line["status"] == "draft":
            blockers.append(f"The depreciation draft for {label(line['month'])} ({line['number']}) includes this asset. Post it or cancel it first.")
        elif line["month"] >= month_start(day):
            blockers.append(f"Depreciation for {label(line['month'])} was already charged on it ({line['number']}), so date the disposal in "
                            f"{label(add_months(line['month'], 1))} or later, or reverse that run.")
    charged = {l["month"] for l in lines}
    missing = [(m, c) for m, c, _a, _n in schedule_rows(asset) if m < month_start(day) and m not in charged]
    if missing:
        warnings.append(f"Depreciation for {', '.join(label(m) for m, _c in missing[:6])}{' and more' if len(missing) > 6 else ''} "
                        f"(Rs {sum((c for _m, c in missing), ZERO):,.2f}) hasn't been charged on it. The disposal takes only what was charged, "
                        "so that shows in the gain or loss. Run those months first if the books are still open.")
    accumulated = booked(asset, lines)
    cost = money(asset.cost)
    gain = proceeds - (cost - accumulated)
    what = f"{asset.code} {asset.name}"
    voucher_lines = []
    if accumulated > 0:
        voucher_lines.append((asset.accumulated_account, accumulated, ZERO, f"Depreciation charged on {what}"))
    if proceeds > 0 and money_account is not None:
        voucher_lines.append((money_account, proceeds, ZERO, f"Money received for {what}"))
    if gain < 0:
        voucher_lines.append((disposal_account, -gain, ZERO, f"Loss on disposal of {what}"))
    voucher_lines.append((asset.category_account, ZERO, cost, f"Cost of {what}"))
    if gain > 0:
        voucher_lines.append((disposal_account, ZERO, gain, f"Gain on disposal of {what}"))
    return {
        "asset": asset, "date": day.isoformat(), "cost": _s(cost), "accumulated": _s(accumulated), "netBookValue": _s(cost - accumulated),
        "proceeds": _s(proceeds), "gain": _s(gain), "moneyAccount": money_account, "disposalAccount": disposal_account,
        "blockers": blockers, "warnings": warnings,
        "lines": [{"account": _ref(a), "accountId": str(a.id), "debit": _s(d), "credit": _s(c), "description": desc[:255]} for a, d, c, desc in voucher_lines],
    }


def plan_out(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k not in ("asset", "moneyAccount", "disposalAccount")}


@atomic()
async def dispose(user: User, asset_id: str, payload: dict, access: accounts_areas.Access) -> FixedAsset:
    plan = await disposal_plan(asset_id, payload, access)
    if plan["blockers"]:
        raise AssetError(plan["blockers"][0])
    asset = plan["asset"]
    # The person uses the asset's own accounts and the account the money went into; the gain or loss line is the
    # software's balancing figure, like the opening balances' balancing line.
    areas = await accounts_areas.areas_by_account([BOOK])
    for account in (asset.category_account, asset.accumulated_account, plan["moneyAccount"]):
        if account is None:
            continue
        area, key = areas.get(str(account.id), (None, None))
        if not access.can_use(area, key):
            raise accounts_areas.refuse(f"You can't use {account.name} on a voucher.", area)
    voucher = await vouchers_service.create_draft(user, {
        "vtype": "JV", "date": plan["date"], "referenceNo": asset.code,
        "description": f"Disposal of {asset.code} {asset.name}: cost Rs {Decimal(plan['cost']):,.2f}, received Rs {Decimal(plan['proceeds']):,.2f}"[:500],
        "lines": [{"accountId": l["accountId"], "debit": l["debit"], "credit": l["credit"], "description": l["description"]} for l in plan["lines"]],
    })
    asset.disposal_date = date.fromisoformat(plan["date"])
    asset.disposal_proceeds = Decimal(plan["proceeds"])
    asset.disposal_account = plan["moneyAccount"]
    asset.disposal_voucher = voucher
    await asset.save()
    return asset
