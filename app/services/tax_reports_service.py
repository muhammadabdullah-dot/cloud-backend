"""Tax reports at head office: GST month by month, withholding tax, advance income tax and income tax, read from posted
vouchers in head office's own book, one branch's, or all of them together.

The same reading as a branch's report (see the branch server's services/tax_reports_service.py): GST output tax is what
SALES TAX (GST) PAYABLE is credited with on sales vouchers less what they debit for goods returned; GST input tax is what
SALES TAX (GST) INPUT is debited with on purchase vouchers less what purchase return vouchers credit back. Anything else
on those accounts comes from a voucher someone wrote: a debit to GST payable is a payment or a set-off, a credit to GST
input is input used in a set-off. The figures by rate come from the godown's goods received, so only for head office's
own book; a branch's are on its own report at the branch.

The report is its own grant (accounts.tax) and covers only the tax accounts, so it isn't limited by account areas.
Opening a voucher from it follows the vouchers screen's own rules. The set-off voucher is written in book HO only.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import atomic

from app.models import HEAD_OFFICE_BOOK, Account, User, Voucher
from app.services import accounts_areas, vouchers_service
from app.services.accounts_chart_service import gid, money
from app.services.accounts_reports_service import PKT, shop_day

ZERO = Decimal("0")
OUTPUT_KEY, INPUT_KEY, ADVANCE_KEY, WHT_KEY = "tax.gst_output", "tax.gst_input", "tax.advance", "tax.wht_payable"
TAX_GROUPS = ("1107", "2102")
INCOME_TAX_NAME = "INCOME TAX PAYABLE"


class TaxError(Exception):
    def __init__(self, message: str):
        self.message = message


def _s(value) -> str:
    return format(money(value), "f")


def _d(value) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _ref(accounts: list[Account], together: bool = False) -> dict | None:
    """One account, or the same account in several books (no single id then)."""
    if not accounts:
        return None
    first = accounts[0]
    return {"id": str(first.id) if len(accounts) == 1 else None, "code": first.code, "name": first.name, "book": first.book if not together else None}


def _named(account: Account, together: bool) -> str:
    return f"{account.book} · {account.name}" if together else account.name


async def _query(sql: str, params: list) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, params)


def months(start: date, end: date) -> list[tuple[date, date]]:
    out, first = [], start.replace(day=1)
    while first <= end:
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(first, start), min(following - timedelta(days=1), end)))
        first = following
    return out


def month_label(start: date, end: date) -> tuple[str, bool]:
    last = ((start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    whole = start.day == 1 and end.day == last
    return (f"{start:%b %Y}" if whole else f"{start.day} to {end.day} {end:%b %Y}"), not whole


async def _keyed(books: list[str], key: str) -> list[Account]:
    return await Account.filter(book__in=books, system_key=key).order_by("book")


async def _entries(account_ids: list[str], start: date | None, end: date) -> list[dict]:
    """Posted lines on the accounts, oldest first, with their voucher."""
    if not account_ids:
        return []
    where, params = ["v.status = 'posted'", f"l.account_id IN ({','.join('?' for _ in account_ids)})", "v.date <= ?"], [*account_ids, end.isoformat()]
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    rows = await _query(
        "SELECT v.id AS voucher_id, v.book, v.number, v.vtype, v.date, v.auto, v.description AS vdesc, v.reference_no AS vref, "
        "l.account_id, l.debit, l.credit, l.description FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE {' AND '.join(where)} ORDER BY v.date, v.created_at, v.number, l.line_no", params,
    )
    for r in rows:
        r["date"] = _day(r["date"])
        r["debit"], r["credit"] = money(_d(r["debit"])), money(_d(r["credit"]))
        r["voucher_id"], r["account_id"] = str(r["voucher_id"]), str(r["account_id"])
        if r["description"] == "__header__":
            r["description"] = None
    return rows


async def _balances(account_ids: list[str], before: date | None = None, upto: date | None = None) -> dict[str, Decimal]:
    if not account_ids:
        return {}
    where, params = ["v.status = 'posted'", f"l.account_id IN ({','.join('?' for _ in account_ids)})"], list(account_ids)
    if before:
        where.append("v.date < ?")
        params.append(before.isoformat())
    if upto:
        where.append("v.date <= ?")
        params.append(upto.isoformat())
    rows = await _query(
        f"SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE {' AND '.join(where)} GROUP BY l.account_id", params,
    )
    return {str(r["account_id"]): money(_d(r["dr"])) - money(_d(r["cr"])) for r in rows}


def _entry_out(r: dict, balance: Decimal | None = None) -> dict:
    out = {
        "voucherId": r["voucher_id"], "book": r["book"], "number": r["number"], "vtype": r["vtype"], "date": r["date"].isoformat(), "auto": bool(r["auto"]),
        "description": r["description"] or r["vdesc"], "debit": _s(r["debit"]), "credit": _s(r["credit"]),
    }
    if balance is not None:
        out["balance"] = _s(balance)
    return out


# ── GST ──────────────────────────────────────────────────────────────────────────────────────────

GST_FIGURES = ("outputSales", "outputReturns", "outputOther", "output", "inputPurchases", "inputReturns", "inputOther", "input",
               "net", "paid", "inputUsed")


def _classify(r: dict, output_ids: set[str], input_ids: set[str]) -> dict[str, Decimal]:
    out = {k: ZERO for k in GST_FIGURES}
    if r["account_id"] in output_ids:
        if r["vtype"] == "SV" and r["auto"]:
            out["outputSales"] += r["credit"]
            out["outputReturns"] += r["debit"]
        else:
            out["outputOther"] += r["credit"]
            out["paid"] += r["debit"]
    elif r["account_id"] in input_ids:
        if r["vtype"] == "PV" and r["auto"]:
            out["inputPurchases"] += r["debit"] - r["credit"]
        elif r["vtype"] == "PRV" and r["auto"]:
            out["inputReturns"] += r["credit"] - r["debit"]
        else:
            out["inputOther"] += r["debit"]
            out["inputUsed"] += r["credit"]
    return out


async def gst(books: list[str], together: bool, start: date, end: date) -> dict:
    outputs, inputs = await _keyed(books, OUTPUT_KEY), await _keyed(books, INPUT_KEY)
    output_ids, input_ids = {str(a.id) for a in outputs}, {str(a.id) for a in inputs}
    ids = sorted(output_ids | input_ids)
    opening = await _balances(ids, before=start)
    opening_payable = -sum((opening.get(i, ZERO) for i in output_ids), ZERO)
    opening_input = sum((opening.get(i, ZERO) for i in input_ids), ZERO)
    rows = await _entries(ids, start, end)
    periods = months(start, end)
    figures = [{k: ZERO for k in GST_FIGURES} for _ in periods]
    vouchers: dict[str, dict] = {}
    for r in rows:
        index = next(i for i, (a, b) in enumerate(periods) if a <= r["date"] <= b)
        for k, v in _classify(r, output_ids, input_ids).items():
            figures[index][k] += v
        entry = vouchers.setdefault(r["voucher_id"], {
            "voucherId": r["voucher_id"], "book": r["book"], "number": r["number"], "vtype": r["vtype"], "date": r["date"].isoformat(), "auto": bool(r["auto"]),
            "description": r["vdesc"], "month": f"{periods[index][0]:%Y-%m}", "outputDebit": ZERO, "outputCredit": ZERO, "inputDebit": ZERO, "inputCredit": ZERO,
        })
        side = "output" if r["account_id"] in output_ids else "input"
        entry[f"{side}Debit"] += r["debit"]
        entry[f"{side}Credit"] += r["credit"]
    payable, available = opening_payable, opening_input
    month_rows = []
    for (a, b), f in zip(periods, figures):
        f["output"] = f["outputSales"] - f["outputReturns"] + f["outputOther"]
        f["input"] = f["inputPurchases"] - f["inputReturns"] + f["inputOther"]
        f["net"] = f["output"] - f["input"]
        payable += f["output"] - f["paid"]
        available += f["input"] - f["inputUsed"]
        text, partial = month_label(a, b)
        month_rows.append({"key": f"{a:%Y-%m}", "label": text, "from": a.isoformat(), "to": b.isoformat(), "partial": partial,
                           **{k: _s(v) for k, v in f.items()}, "payableAtEnd": _s(payable), "inputAtEnd": _s(available)})
    totals = {k: _s(sum((f[k] for f in figures), ZERO)) for k in GST_FIGURES}

    def kind(v: dict) -> str:
        return {"SV": "sales", "PV": "purchases", "PRV": "purchase-returns"}.get(v["vtype"], "other") if v["auto"] else "other"

    return {
        "books": books, "together": together, "from": start.isoformat(), "to": end.isoformat(),
        "accounts": {"output": _ref(outputs, together), "input": _ref(inputs, together)},
        "opening": {"payable": _s(opening_payable), "input": _s(opening_input)},
        "closing": {"payable": _s(payable), "input": _s(available)},
        "months": month_rows, "totals": totals,
        "vouchers": [{**v, "kind": kind(v), **{k: _s(v[k]) for k in ("outputDebit", "outputCredit", "inputDebit", "inputCredit")}}
                     for v in vouchers.values()],
        "byRate": await by_rate(start, end) if books == [HEAD_OFFICE_BOOK] else {
            "available": False, "rows": [], "totals": {}, "output": "0.00", "input": "0.00",
            "note": "GST by rate comes from the bills and goods received, which each branch keeps. See it on the branch's own tax report.",
        },
        "check": {"outputCredits": _s(sum((r["credit"] for r in rows if r["account_id"] in output_ids), ZERO)),
                  "outputDebits": _s(sum((r["debit"] for r in rows if r["account_id"] in output_ids), ZERO)),
                  "inputDebits": _s(sum((r["debit"] for r in rows if r["account_id"] in input_ids), ZERO)),
                  "inputCredits": _s(sum((r["credit"] for r in rows if r["account_id"] in input_ids), ZERO))},
    }


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(start, time.min, tzinfo=PKT).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=PKT).astimezone(timezone.utc))


async def by_rate(start: date, end: date) -> dict:
    """GST on the godown's goods received, by rate, worked out the way the purchase vouchers are."""
    from app.models import GRNLine

    lo, hi = _bounds(start, end)
    rates: dict[str, dict[str, Decimal]] = {}
    for l in await GRNLine.filter(grn__at__gte=lo, grn__at__lt=hi).values("qty", "unit_price", "disc_percent", "tax_rate"):
        net = _d(l["qty"]) * _d(l["unit_price"]) * (Decimal("1") - _d(l["disc_percent"]) / Decimal("100"))
        key = format(_d(l["tax_rate"]).normalize(), "f")
        row = rates.setdefault(key, {f: ZERO for f in ("salesTaxable", "salesTax", "returnsTaxable", "returnsTax", "purchasesTaxable", "purchasesTax",
                                                       "purchaseReturnsTaxable", "purchaseReturnsTax")})
        row["purchasesTaxable"] += net
        row["purchasesTax"] += net * _d(l["tax_rate"]) / Decimal("100")
    rows = [{"rate": k, **{f: _s(v) for f, v in r.items()}} for k, r in sorted(rates.items(), key=lambda kv: Decimal(kv[0]))]
    total = {f: _s(sum((r[f] for r in rates.values()), ZERO)) for f in ("salesTax", "returnsTax", "purchasesTax", "purchaseReturnsTax")}
    return {"available": True, "rows": rows, "totals": total, "output": "0.00", "input": total.get("purchasesTax", "0.00")}


# ── setting input off against output (head office's book) ───────────────────────────────────────

def _month(value) -> tuple[date, date]:
    try:
        first = date.fromisoformat(f"{str(value)[:7]}-01")
    except ValueError as exc:
        raise TaxError("Pick a month.") from exc
    return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def set_off_reference(first: date) -> str:
    return f"GST set-off {first:%Y-%m}"


async def set_off_plan(month) -> dict:
    """GST input set off against GST payable as both stand at the month's end: the smaller of the two balances."""
    first, last = _month(month)
    output = await Account.get_or_none(book=HEAD_OFFICE_BOOK, system_key=OUTPUT_KEY)
    input_ = await Account.get_or_none(book=HEAD_OFFICE_BOOK, system_key=INPUT_KEY)
    if not output or not input_:
        raise TaxError("Head office's chart has no GST payable or GST input account.")
    blockers: list[str] = []
    balances = await _balances([str(output.id), str(input_.id)], upto=last)
    payable, available = -balances.get(str(output.id), ZERO), balances.get(str(input_.id), ZERO)
    amount = max(ZERO, min(payable, available))
    settings = await vouchers_service.settings(HEAD_OFFICE_BOOK)
    if first > shop_day().replace(day=1):
        blockers.append(f"{first:%b %Y} hasn't started.")
    if settings.locked_until and last <= settings.locked_until:
        blockers.append(vouchers_service.closed_message(settings.locked_until))
    waiting = await Voucher.filter(book=HEAD_OFFICE_BOOK, reference_no=set_off_reference(first), status="draft").first()
    if waiting:
        blockers.append(f"A set-off for {first:%b %Y} is already waiting to be posted ({waiting.number}). Post it or cancel it first.")
    if amount <= 0:
        blockers.append(f"Nothing to set off at the end of {first:%b %Y}: GST payable is Rs {payable:,.2f} and GST input is Rs {available:,.2f}.")
    return {"month": f"{first:%Y-%m}", "label": f"{first:%b %Y}", "date": last.isoformat(), "payable": _s(payable), "input": _s(available),
            "amount": _s(amount), "blockers": blockers, "outputAccount": _ref([output]), "inputAccount": _ref([input_])}


@atomic()
async def prepare_set_off(user: User, month, access: accounts_areas.Access) -> Voucher:
    plan = await set_off_plan(month)
    if plan["blockers"]:
        raise TaxError(plan["blockers"][0])
    words = f"GST input set off against GST payable for {plan['label']}"
    return await vouchers_service.create_draft(user, {
        "vtype": "JV", "date": plan["date"], "referenceNo": set_off_reference(date.fromisoformat(f"{plan['month']}-01")), "description": words,
        "lines": [{"accountId": plan["outputAccount"]["id"], "debit": plan["amount"], "credit": "0", "description": words},
                  {"accountId": plan["inputAccount"]["id"], "debit": "0", "credit": plan["amount"], "description": words}],
    }, access)


# ── withholding, advance and income tax ──────────────────────────────────────────────────────────

async def _account_story(accounts: list[Account], start: date, end: date, credit_nature: bool, together: bool) -> dict | None:
    """Opening, the period's entries with a running balance, month by month, and the closing balance, on the natural side."""
    if not accounts:
        return None
    ids = [str(a.id) for a in accounts]
    sign = Decimal("-1") if credit_nature else Decimal("1")
    before = await _balances(ids, before=start)
    opening = sign * sum((before.get(i, ZERO) for i in ids), ZERO)
    running = opening
    entries = []
    rows = await _entries(ids, start, end)
    for r in rows:
        running += sign * (r["debit"] - r["credit"])
        entries.append(_entry_out(r, balance=running))
    month_rows, balance = [], opening
    for a, b in months(start, end):
        inside = [r for r in rows if a <= r["date"] <= b]
        debit = sum((r["debit"] for r in inside), ZERO)
        credit = sum((r["credit"] for r in inside), ZERO)
        balance += sign * (debit - credit)
        text, partial = month_label(a, b)
        month_rows.append({"key": f"{a:%Y-%m}", "label": text, "partial": partial, "debit": _s(debit), "credit": _s(credit), "balance": _s(balance)})
    account = _ref(accounts, together)
    if len(accounts) == 1 and together:
        account["name"] = _named(accounts[0], together)
    return {"account": account, "opening": _s(opening), "debit": _s(sum((r["debit"] for r in rows), ZERO)),
            "credit": _s(sum((r["credit"] for r in rows), ZERO)), "closing": _s(running), "entries": entries, "months": month_rows}


async def withholding(books: list[str], together: bool, start: date, end: date) -> dict:
    wht, advance = await _keyed(books, WHT_KEY), await _keyed(books, ADVANCE_KEY)
    income = await Account.filter(book__in=books, group_id__in=[gid(b, "2102") for b in books], name__iexact=INCOME_TAX_NAME).order_by("book", "code")
    by_supplier: dict[str, dict] = {}
    if advance:
        rows = await _entries([str(a.id) for a in advance], start, end)
        purchase_ids = sorted({r["voucher_id"] for r in rows if r["vtype"] == "PV" and r["auto"]})
        suppliers: dict[str, dict] = {}
        for chunk in range(0, len(purchase_ids), 400):
            part = purchase_ids[chunk:chunk + 400]
            for s in await _query(
                "SELECT l.voucher_id, a.id AS account_id, a.book, a.code, a.name FROM acc_voucher_lines l JOIN acc_accounts a ON a.id = l.account_id "
                f"WHERE a.kind = 'supplier' AND l.voucher_id IN ({','.join('?' for _ in part)})", part,
            ):
                suppliers[str(s["voucher_id"])] = {"id": str(s["account_id"]), "code": s["code"], "book": s["book"],
                                                   "name": f"{s['book']} · {s['name']}" if together else s["name"]}
        for r in rows:
            supplier = suppliers.get(r["voucher_id"]) if r["vtype"] == "PV" and r["auto"] else None
            entry = by_supplier.setdefault(supplier["id"] if supplier else "", {"account": supplier, "bills": 0, "amount": ZERO, "vouchers": []})
            entry["bills"] += 1 if r["debit"] > 0 else 0
            entry["amount"] += r["debit"] - r["credit"]
            entry["vouchers"].append(_entry_out(r))
    special = {str(a.id) for a in [*wht, *advance, *income, *await _keyed(books, OUTPUT_KEY), *await _keyed(books, INPUT_KEY)]}
    others = []
    for account in await Account.filter(book__in=books, group_id__in=[gid(b, g) for b in books for g in TAX_GROUPS]).order_by("code", "book"):
        if str(account.id) in special:
            continue
        story = await _account_story([account], start, end, credit_nature=account.group_id.endswith(":2102"), together=together)
        if story and (Decimal(story["opening"]) or story["entries"]):
            others.append({k: v for k, v in story.items() if k != "months"})
    return {
        "books": books, "together": together, "from": start.isoformat(), "to": end.isoformat(),
        "withholding": await _account_story(wht, start, end, credit_nature=True, together=together),
        "advance": await _account_story(advance, start, end, credit_nature=False, together=together),
        "advanceBySupplier": [{**v, "amount": _s(v["amount"])} for v in sorted(by_supplier.values(), key=lambda v: -v["amount"])],
        "incomeTax": await _account_story(list(income), start, end, credit_nature=True, together=together),
        "others": others,
    }
