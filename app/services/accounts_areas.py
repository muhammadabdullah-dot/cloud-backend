"""Which accounts a person may see and use.

Every account belongs to exactly one area (cash and bank, tax, expenses…), decided here and nowhere else: by its
kind first (a customer's account is what customers owe whatever group it sits in), then by its group, then by its
category, so a group somebody adds still lands somewhere sensible. A person's area ticks limit the ledger, the chart
and vouchers: seeing an area (R) shows its accounts, using it (W) lets them go on a voucher line. Whole-book reports
don't look at areas at all. The areas are the same in every book, head office's and each branch's: a group is known by
its plain code, whichever book it's in.

The opening balances' balancing account belongs to equity, but whoever may write the opening balances may use it
there: the balancing line is the software's, not a choice the person makes.
"""
from app.core.abilities import AREA_LABELS
from app.core.resources import AREA_KEYS
from app.models import Account, AccountGroup, User

KIND_AREAS = {
    "customer": "receivables", "supplier": "payables", "interoffice": "inter-office",
    "cash": "cash-bank", "bank": "cash-bank", "wallet": "cash-bank",
}
GROUP_AREAS = {
    "1101": "cash-bank", "1102": "cash-bank", "1103": "cash-bank",
    "1104": "receivables", "1105": "stock", "1106": "advances", "1107": "tax", "2102": "tax",
    "1201": "fixed-assets", "1202": "fixed-assets", "1301": "inter-office", "2101": "payables",
    "2103": "customer-balances", "2104": "other-liabilities", "2201": "other-liabilities",
    "3101": "equity", "3102": "equity", "4101": "income", "4102": "income", "4201": "income",
    "5101": "cost-of-sales", "5102": "cost-of-sales",
    "5201": "expenses", "5202": "expenses", "5203": "expenses", "5204": "expenses", "5205": "expenses", "5206": "expenses",
    "5301": "expenses",
}
CATEGORY_AREAS = {
    "11": "advances", "12": "fixed-assets", "13": "inter-office", "21": "other-liabilities", "22": "other-liabilities",
    "31": "equity", "41": "income", "42": "income", "51": "cost-of-sales", "52": "expenses", "53": "expenses",
}
# Accounts that sit in one group but belong with another area's work.
SYSTEM_KEY_AREAS = {"expense.depreciation": "fixed-assets", "income.asset_disposal": "fixed-assets"}
OPENING_EQUITY_KEY = "equity.opening"
ALL_AREAS = frozenset(AREA_KEYS)


def area_of(kind: str | None, group_code: str | None, category_code: str | None, system_key: str | None = None) -> str | None:
    if system_key in SYSTEM_KEY_AREAS:
        return SYSTEM_KEY_AREAS[system_key]
    if kind in KIND_AREAS:
        return KIND_AREAS[kind]
    if group_code in GROUP_AREAS:
        return GROUP_AREAS[group_code]
    category = category_code or (group_code or "")[:2]
    return CATEGORY_AREAS.get(category)


def area_label(area: str | None) -> str:
    return AREA_LABELS.get(area or "", "every kind of account")


class AreaRefused(Exception):
    """The person asked for an account outside their areas. The message names the account and the access to ask for."""

    def __init__(self, message: str):
        self.message = message


class Access:
    """What one person may see and use, worked out once for a request."""

    def __init__(self, readable: set[str], writable: set[str], opening_read: bool = False, opening_write: bool = False):
        self.writable = set(writable)
        self.readable = set(readable) | self.writable
        self.opening_write = opening_write
        self.opening_read = opening_read or opening_write

    @property
    def sees_everything(self) -> bool:
        return self.readable >= ALL_AREAS

    @property
    def uses_everything(self) -> bool:
        return self.writable >= ALL_AREAS

    def can_see(self, area: str | None, system_key: str | None = None) -> bool:
        if system_key == OPENING_EQUITY_KEY and self.opening_read:
            return True
        # An account no area claims is only for someone who sees every area.
        return area in self.readable if area else self.sees_everything

    def can_use(self, area: str | None, system_key: str | None = None) -> bool:
        if system_key == OPENING_EQUITY_KEY and self.opening_write:
            return True
        return area in self.writable if area else self.uses_everything


EVERYTHING = Access(set(ALL_AREAS), set(ALL_AREAS), True, True)


async def access_of(user: User) -> Access:
    from app.services.rbac_service import grants_of

    grants = await grants_of(user)
    readable = {key for key in AREA_KEYS if "R" in grants.get(f"accounts.area.{key}", set())}
    writable = {key for key in AREA_KEYS if "W" in grants.get(f"accounts.area.{key}", set())}
    opening = grants.get("accounts.opening-balances", set())
    return Access(readable, writable, "R" in opening, "W" in opening)


async def _groups() -> dict[str, tuple[str, str]]:
    """Group id ("HO:1101") -> (plain code, category)."""
    return {g.id: (g.code, g.category_id) for g in await AccountGroup.all()}


def _area(kind, group_id, system_key, groups: dict[str, tuple[str, str]]) -> str | None:
    code, category = groups.get(group_id or "", ((group_id or "").split(":", 1)[-1], None))
    return area_of(kind, code, category, system_key)


async def account_area(account: Account, groups: dict[str, tuple[str, str]] | None = None) -> str | None:
    groups = groups if groups is not None else await _groups()
    return _area(account.kind, account.group_id, account.system_key, groups)


async def areas_by_account(books: list[str] | None = None) -> dict[str, tuple[str | None, str | None]]:
    """Every account's area and system key, by account id."""
    groups = await _groups()
    qs = Account.filter(book__in=books) if books else Account.all()
    rows = await qs.values("id", "kind", "group_id", "system_key")
    return {str(r["id"]): (_area(r["kind"], r["group_id"], r["system_key"], groups), r["system_key"]) for r in rows}


async def unreadable_account_ids(access: Access) -> list[str]:
    if access.sees_everything:
        return []
    return [account_id for account_id, (area, key) in (await areas_by_account()).items() if not access.can_see(area, key)]


def refuse(sentence: str, area: str | None) -> AreaRefused:
    return AreaRefused(f"{sentence} Ask your manager for access to {area_label(area)}.")


async def check_ledger(access: Access, account: Account) -> None:
    area = await account_area(account)
    if not access.can_see(area, account.system_key):
        raise refuse(f"You can't open the ledger of {account.name}.", area)


async def check_lines(access: Access, voucher_number: str, account_ids: list[str], doing: str) -> None:
    """Posting, reversing or opening a voucher needs every account on it within the person's areas."""
    if access.sees_everything:
        return
    wanted = {str(a) for a in account_ids}
    areas = await areas_by_account()
    for account_id in sorted(wanted):
        area, key = areas.get(account_id, (None, None))
        if not access.can_see(area, key):
            account = await Account.get_or_none(id=account_id)
            name = account.name if account else "an account"
            raise refuse(f"You can't {doing} {voucher_number}: it uses {name}.", area)


async def check_group_change(access: Access, *, category_code: str | None = None, group_code: str | None = None,
                             sub_group_code: str | None = None) -> None:
    """Adding or changing a group or sub group is for someone who sees the accounts it holds."""
    if access.sees_everything:
        return
    from app.models import HEAD_OFFICE_BOOK, AccountSubGroup

    if sub_group_code:
        sub = await AccountSubGroup.get_or_none(id=f"{HEAD_OFFICE_BOOK}:{sub_group_code}")
        group_code = sub.group_id.split(":", 1)[-1] if sub else group_code
    groups = await _groups()
    category = groups.get(f"{HEAD_OFFICE_BOOK}:{group_code}", (None, None))[1] if group_code else None
    area = area_of(None, group_code, category or category_code)
    if not access.can_see(area):
        raise refuse("You can't change that part of the chart.", area)


async def check_account_change(access: Access, account: Account | None, *, group_code: str | None = None, kind: str | None = None) -> None:
    """Adding, changing or deleting an account is for someone who sees it, where it is now and where it's going."""
    if access.sees_everything:
        return
    groups = await _groups()
    if account is not None:
        area = await account_area(account, groups)
        if not access.can_see(area, account.system_key):
            raise refuse(f"You can't change {account.name}.", area)
    if group_code:
        from app.models import HEAD_OFFICE_BOOK

        kind = kind or (account.kind if account is not None else None)
        area = _area(kind, f"{HEAD_OFFICE_BOOK}:{group_code}", None, groups)
        if not access.can_see(area, account.system_key if account is not None else None):
            raise refuse("You can't add or move accounts there.", area)


async def check_use(access: Access, accounts: list[tuple[int | None, Account]]) -> None:
    """Every account on a voucher being written must be one the person may use. (line number or None for the header, account)"""
    if access.uses_everything:
        return
    groups = await _groups()
    for line_no, account in accounts:
        area = await account_area(account, groups)
        if not access.can_use(area, account.system_key):
            where = f"Line {line_no}: you" if line_no else "You"
            raise refuse(f"{where} can't use {account.name} on a voucher.", area)
