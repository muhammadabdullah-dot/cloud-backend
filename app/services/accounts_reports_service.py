"""Reading the books at head office — head office's own, one branch's, or the whole company's.

The same reports as a branch's (ledger, trial balance, income statement, balance sheet, day book, ageing), over one
or more books. When books are added together, accounts every book shares — the standard chart, the accounts the
software posts to — become one line; a book's own accounts (its customers, its suppliers, anything it added) keep a
line each, named with the book's code. Head office's current account with a branch and that branch's account with
head office cancel out in the company's figures, as they should.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.models import HEAD_OFFICE_BOOK, Account, AccountCategory, AccountGroup, AccountType, Voucher
from app.services.accounts_chart_service import money

ZERO = Decimal("0")
PKT = timezone(timedelta(hours=5))
ALL = "ALL"


def shop_day(at: datetime | None = None) -> date:
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(PKT).date()


def _d(value) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _s(value: Decimal) -> str:
    return format(money(value), "f")


async def _query(sql: str, params: list) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, params)


async def all_books() -> list[str]:
    from app.models import Branch

    codes = [HEAD_OFFICE_BOOK] + [b.code for b in await Branch.all().order_by("code")]
    seen = set(await Account.all().distinct().values_list("book", flat=True))
    return codes + sorted(seen - set(codes))


async def resolve_books(book: str | None) -> tuple[list[str], bool]:
    """The books a request covers, and whether they're being added together."""
    wanted = (book or HEAD_OFFICE_BOOK).strip().upper()
    if wanted == ALL:
        return await all_books(), True
    return [wanted], False


async def chart_map(books: list[str]) -> dict[str, dict]:
    types = {t.code: t for t in await AccountType.all()}
    categories = {c.code: c for c in await AccountCategory.all()}
    groups = {g.id: g for g in await AccountGroup.filter(book__in=books)}
    out = {}
    for a in await Account.filter(book__in=books):
        g = groups.get(a.group_id)
        c = categories.get(g.category_id) if g else None
        t = types.get(c.type_id) if c else None
        out[str(a.id)] = {
            "accountId": str(a.id), "book": a.book, "code": a.code, "name": a.name, "kind": a.kind, "systemKey": a.system_key,
            "partyRef": a.party_ref, "active": a.active, "standard": a.standard,
            "groupCode": g.code if g else "", "groupName": g.name if g else "", "groupPriority": g.priority if g else 0,
            "categoryCode": c.code if c else "", "categoryName": c.name if c else "",
            "typeCode": t.code if t else "", "typeName": t.name if t else "", "nature": t.nature if t else "debit",
            "statement": t.statement if t else "balance",
        }
    return out


def merge_key(meta: dict, together: bool) -> str:
    if not together:
        return meta["accountId"]
    key = meta.get("systemKey") or ""
    if key and not key.startswith(("customer:", "supplier:")):
        return f"key:{key}"
    if meta.get("standard"):
        return f"code:{meta['code']}"
    return meta["accountId"]


def _label(meta: dict, together: bool, merged: bool) -> str:
    return meta["name"] if (not together or merged) else f"{meta['book']} · {meta['name']}"


async def _sums(books: list[str], start: date | None = None, end: date | None = None, before: date | None = None,
                account_ids: list[str] | None = None) -> dict[str, tuple[Decimal, Decimal]]:
    where, params = ["v.status = 'posted'", f"v.book IN ({','.join('?' for _ in books)})"], list(books)
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    if end:
        where.append("v.date <= ?")
        params.append(end.isoformat())
    if before:
        where.append("v.date < ?")
        params.append(before.isoformat())
    if account_ids:
        where.append(f"l.account_id IN ({','.join('?' for _ in account_ids)})")
        params.extend(account_ids)
    rows = await _query(
        f"SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l "
        f"JOIN acc_vouchers v ON v.id = l.voucher_id WHERE {' AND '.join(where)} GROUP BY l.account_id", params,
    )
    return {str(r["account_id"]): (money(_d(r["dr"])), money(_d(r["cr"]))) for r in rows}


def _merge(chart: dict[str, dict], sums: dict[str, tuple[Decimal, Decimal]], together: bool) -> dict[str, dict]:
    """Rows keyed by merge key: meta of the first account, and the debit/credit of all of them."""
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for account_id, meta in chart.items():
        key = merge_key(meta, together)
        counts[key] = counts.get(key, 0) + 1
    for account_id, meta in chart.items():
        key = merge_key(meta, together)
        dr, cr = sums.get(account_id, (ZERO, ZERO))
        row = out.get(key)
        if row is None:
            merged = key.startswith(("key:", "code:"))
            row = out[key] = {**meta, "name": _label(meta, together, merged), "books": [meta["book"]], "dr": ZERO, "cr": ZERO, "parts": []}
            if together and merged:
                row["accountId"] = meta["accountId"] if counts[key] == 1 else None
        elif meta["book"] not in row["books"]:
            row["books"].append(meta["book"])
        if dr or cr:
            row["parts"].append({"book": meta["book"], "accountId": account_id})
        row["dr"] += dr
        row["cr"] += cr
    return out


async def balance_of(account_id, upto: date | None = None, natural: bool = True) -> Decimal:
    account = await Account.get(id=account_id).prefetch_related("group__category__type")
    sums = await _sums([account.book], end=upto, account_ids=[str(account_id)])
    dr, cr = sums.get(str(account_id), (ZERO, ZERO))
    if not natural:
        return dr - cr
    return dr - cr if account.group.category.type.nature == "debit" else cr - dr


def _split(value: Decimal) -> tuple[str, str]:
    return (_s(value), "0.00") if value >= 0 else ("0.00", _s(-value))


def codes(value: str | None) -> set[str]:
    """A filter that names one group or category, or several separated by commas ("1102,1103")."""
    return {c.strip() for c in (value or "").split(",") if c.strip()}


async def trial_balance(books: list[str], together: bool, start: date | None, end: date, group_code: str | None = None, include_zero: bool = False,
                        category_code: str | None = None) -> dict:
    chart = await chart_map(books)
    opening = _merge(chart, await _sums(books, before=start) if start else {}, together)
    period = _merge(chart, await _sums(books, start=start, end=end), together)
    groups_wanted, categories_wanted = codes(group_code), codes(category_code)
    rows = []
    for key, p in period.items():
        if groups_wanted and p["groupCode"] not in groups_wanted:
            continue
        if categories_wanted and p["categoryCode"] not in categories_wanted:
            continue
        o = opening.get(key, {"dr": ZERO, "cr": ZERO})
        o_dr, o_cr, p_dr, p_cr = o["dr"], o["cr"], p["dr"], p["cr"]
        if not include_zero and not (o_dr or o_cr or p_dr or p_cr):
            continue
        open_bal = o_dr - o_cr
        close_bal = open_bal + p_dr - p_cr
        open_dr, open_cr = _split(open_bal)
        close_dr, close_cr = _split(close_bal)
        meta = {k: v for k, v in p.items() if k not in ("dr", "cr")}
        # A book whose account has only an opening balance is still a book the line opens.
        meta["parts"] = list({part["accountId"]: part for part in [*opening.get(key, {}).get("parts", []), *p["parts"]]}.values())
        rows.append({**meta, "opening": _s(open_bal), "debit": _s(p_dr), "credit": _s(p_cr), "closing": _s(close_bal),
                     "openingDr": open_dr, "openingCr": open_cr, "closingDr": close_dr, "closingCr": close_cr,
                     "totalDebit": _s(o_dr + p_dr), "totalCredit": _s(o_cr + p_cr)})
    rows.sort(key=lambda r: (r["code"], r["name"]))
    groups: dict[str, dict] = {}
    for r in rows:
        gkey = f"{r['groupCode']}|{r['groupName']}"
        g = groups.setdefault(gkey, {
            "groupCode": r["groupCode"], "groupName": r["groupName"], "categoryCode": r["categoryCode"], "categoryName": r["categoryName"],
            "typeCode": r["typeCode"], "typeName": r["typeName"], "nature": r["nature"],
            "opening": ZERO, "debit": ZERO, "credit": ZERO, "closing": ZERO, "totalDebit": ZERO, "totalCredit": ZERO, "accounts": 0,
        })
        for k in ("opening", "debit", "credit", "closing", "totalDebit", "totalCredit"):
            g[k] += Decimal(r[k])
        g["accounts"] += 1
    group_rows = []
    for g in sorted(groups.values(), key=lambda x: (x["groupCode"], x["groupName"])):
        open_dr, open_cr = _split(g["opening"])
        close_dr, close_cr = _split(g["closing"])
        group_rows.append({**g, **{k: _s(g[k]) for k in ("opening", "debit", "credit", "closing", "totalDebit", "totalCredit")},
                           "openingDr": open_dr, "openingCr": open_cr, "closingDr": close_dr, "closingCr": close_cr})
    totals = {k: ZERO for k in ("openingDr", "openingCr", "debit", "credit", "closingDr", "closingCr", "totalDebit", "totalCredit")}
    for r in rows:
        for k in totals:
            totals[k] += Decimal(r[k])
    return {"books": books, "together": together, "from": start.isoformat() if start else None, "to": end.isoformat(),
            "rows": rows, "groups": group_rows, "totals": {k: _s(v) for k, v in totals.items()},
            "difference": _s(totals["closingDr"] - totals["closingCr"])}


async def ledger(account_id: str, start: date | None, end: date, include_pdc: bool = False) -> dict:
    account = await Account.get_or_none(id=account_id)
    if not account:
        raise ValueError("That account doesn't exist.")
    chart = await chart_map([account.book])
    meta = chart[str(account_id)]
    opening = ZERO
    if start:
        dr, cr = (await _sums([account.book], before=start, account_ids=[str(account_id)])).get(str(account_id), (ZERO, ZERO))
        opening = dr - cr
    where, params = ["v.status = 'posted'", "l.account_id = ?", "v.date <= ?"], [str(account_id), end.isoformat()]
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    rows = await _query(
        "SELECT v.id AS voucher_id, v.number, v.vtype, v.date, v.description AS vdesc, v.reference_no AS vref, v.cheque_no, v.auto, "
        "l.description, l.reference_no, l.debit, l.credit, l.line_no FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE {' AND '.join(where)} ORDER BY v.date, v.created_at, v.number, l.line_no", params,
    )
    voucher_ids = sorted({str(r["voucher_id"]) for r in rows})
    others: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for chunk_start in range(0, len(voucher_ids), 400):
        chunk = voucher_ids[chunk_start:chunk_start + 400]
        for line in await _query(f"SELECT voucher_id, account_id, debit, credit FROM acc_voucher_lines WHERE voucher_id IN ({','.join('?' for _ in chunk)})", chunk):
            others.setdefault(str(line["voucher_id"]), []).append((str(line["account_id"]), _d(line["debit"]), _d(line["credit"])))
    running = opening
    entries = []
    for r in rows:
        debit, credit = money(_d(r["debit"])), money(_d(r["credit"]))
        running += debit - credit
        contra_ids = list(dict.fromkeys(a for a, d, c in others.get(str(r["voucher_id"]), [])
                                        if a != str(account_id) and ((c > 0) if debit > 0 else (d > 0))))
        contra = [chart.get(a, {}).get("name", "?") for a in contra_ids]
        description = r["description"] if r["description"] and r["description"] != "__header__" else r["vdesc"]
        entries.append({
            "voucherId": str(r["voucher_id"]), "number": r["number"], "vtype": r["vtype"], "date": str(r["date"])[:10],
            "description": description, "referenceNo": r["reference_no"] or r["vref"], "chequeNo": r["cheque_no"], "auto": bool(r["auto"]),
            "debit": _s(debit), "credit": _s(credit), "balance": _s(running), "against": ", ".join(contra)[:200] if contra else None,
            "againstAccounts": [{"accountId": a, "name": chart.get(a, {}).get("name", "?")} for a in contra_ids[:8]],
        })
    total_dr = sum((Decimal(e["debit"]) for e in entries), ZERO)
    total_cr = sum((Decimal(e["credit"]) for e in entries), ZERO)
    return {"account": meta, "from": start.isoformat() if start else None, "to": end.isoformat(), "opening": _s(opening),
            "entries": entries, "totalDebit": _s(total_dr), "totalCredit": _s(total_cr), "closing": _s(running), "pdc": []}


SECTION_OF_CATEGORY = {"41": "sales", "51": "cost", "42": "other", "52": "operating", "53": "financial"}
SECTION_TITLES = {"sales": "Revenue", "cost": "Cost of sales", "other": "Other income", "operating": "Operating expenses", "financial": "Financial expenses"}


async def income_statement(books: list[str], together: bool, start: date, end: date) -> dict:
    chart = await chart_map(books)
    merged = _merge(chart, await _sums(books, start=start, end=end), together)
    sections = {key: {"key": key, "title": title, "groups": {}, "amount": ZERO} for key, title in SECTION_TITLES.items()}
    departments: dict[str, dict] = {}
    for meta in merged.values():
        if meta["statement"] != "income" or not (meta["dr"] or meta["cr"]):
            continue
        key = SECTION_OF_CATEGORY.get(meta["categoryCode"]) or ("other" if meta["typeCode"] == "4" else "operating")
        amount = (meta["cr"] - meta["dr"]) if key in ("sales", "other") else (meta["dr"] - meta["cr"])
        section = sections[key]
        group = section["groups"].setdefault(f"{meta['groupCode']}|{meta['groupName']}", {"code": meta["groupCode"], "name": meta["groupName"], "accounts": [], "amount": ZERO})
        group["accounts"].append({"accountId": meta["accountId"], "code": meta["code"], "name": meta["name"], "amount": _s(amount), "parts": meta["parts"]})
        group["amount"] += amount
        section["amount"] += amount
        system_key = meta.get("systemKey") or ""
        for prefix, field in (("sales.dept.", "sales"), ("cogs.dept.", "cost")):
            if system_key.startswith(prefix):
                dept = departments.setdefault(system_key[len(prefix):], _department(meta["name"].split(" - ", 1)[-1]))
                dept[field] += amount
                dept[f"{field}AccountId"], dept[f"{field}Parts"] = meta["accountId"], meta["parts"]
        if system_key in ("sales.general", "cogs.general"):
            dept = departments.setdefault("", _department("GENERAL"))
            field = "sales" if system_key == "sales.general" else "cost"
            dept[field] += amount
            dept[f"{field}AccountId"], dept[f"{field}Parts"] = meta["accountId"], meta["parts"]
    net_sales, cost = sections["sales"]["amount"], sections["cost"]["amount"]
    gross = net_sales - cost
    net = gross + sections["other"]["amount"] - sections["operating"]["amount"] - sections["financial"]["amount"]
    return {
        "books": books, "together": together, "from": start.isoformat(), "to": end.isoformat(),
        "sections": [{"key": s["key"], "title": s["title"], "amount": _s(s["amount"]),
                      "groups": [{**g, "amount": _s(g["amount"]), "accounts": sorted(g["accounts"], key=lambda a: (a["code"], a["name"]))}
                                 for g in sorted(s["groups"].values(), key=lambda g: g["code"])]} for s in sections.values()],
        "netSales": _s(net_sales), "costOfSales": _s(cost), "grossProfit": _s(gross),
        "grossMargin": _s(gross / net_sales * 100) if net_sales else "0.00", "otherIncome": _s(sections["other"]["amount"]),
        "operatingExpenses": _s(sections["operating"]["amount"]), "financialExpenses": _s(sections["financial"]["amount"]),
        "netProfit": _s(net), "netMargin": _s(net / net_sales * 100) if net_sales else "0.00",
        "departments": [{"name": d["name"], "sales": _s(d["sales"]), "cost": _s(d["cost"]), "gross": _s(d["sales"] - d["cost"]),
                         "salesAccountId": d["salesAccountId"], "costAccountId": d["costAccountId"], "salesParts": d["salesParts"], "costParts": d["costParts"]}
                        for d in sorted(departments.values(), key=lambda d: -d["sales"])],
    }


def _department(name: str) -> dict:
    return {"name": name, "sales": ZERO, "cost": ZERO, "salesAccountId": None, "costAccountId": None, "salesParts": [], "costParts": []}


async def first_posted_day(books: list[str]) -> date | None:
    row = await Voucher.filter(book__in=books, status="posted").order_by("date").first()
    return row.date if row else None


def fiscal_year_start(day: date, start_month: int) -> date:
    return date(day.year if day.month >= start_month else day.year - 1, start_month, 1)


async def _net_profit(books: list[str], start: date | None, end: date) -> Decimal:
    chart = await chart_map(books)
    total = ZERO
    for account_id, (dr, cr) in (await _sums(books, start=start, end=end)).items():
        meta = chart.get(account_id)
        if meta and meta["statement"] == "income":
            total += cr - dr
    return total


async def balance_sheet(books: list[str], together: bool, as_of: date, fiscal_start_month: int) -> dict:
    chart = await chart_map(books)
    merged = _merge(chart, await _sums(books, end=as_of), together)
    fy_start = fiscal_year_start(as_of, fiscal_start_month)
    sides = {"1": {"title": "Assets", "categories": {}, "amount": ZERO}, "2": {"title": "Liabilities", "categories": {}, "amount": ZERO},
             "3": {"title": "Equity", "categories": {}, "amount": ZERO}}
    for meta in merged.values():
        if meta["statement"] != "balance" or meta["typeCode"] not in sides:
            continue
        amount = (meta["dr"] - meta["cr"]) if meta["typeCode"] == "1" else (meta["cr"] - meta["dr"])
        if amount == 0:
            continue
        side = sides[meta["typeCode"]]
        if not together and meta["kind"] == "interoffice" and meta["typeCode"] == "1" and amount < 0:
            # owed to the other office, not owned: it belongs with what the book owes (added together, the offices cancel out instead)
            side, amount = sides["2"], -amount
        category = side["categories"].setdefault(meta["categoryCode"], {"code": meta["categoryCode"], "name": meta["categoryName"], "groups": {}, "amount": ZERO})
        group = category["groups"].setdefault(f"{meta['groupCode']}|{meta['groupName']}", {"code": meta["groupCode"], "name": meta["groupName"], "accounts": [], "amount": ZERO})
        group["accounts"].append({"accountId": meta["accountId"], "code": meta["code"], "name": meta["name"], "amount": _s(amount), "parts": meta["parts"]})
        group["amount"] += amount
        category["amount"] += amount
        side["amount"] += amount
    brought_forward = await _net_profit(books, None, fy_start - timedelta(days=1))
    current = await _net_profit(books, fy_start, as_of)
    equity = sides["3"]
    profit_cat = {"code": "PL", "name": "PROFIT AND LOSS", "groups": {}, "amount": ZERO}
    for code, name, amount in (("PL-BF", "PROFIT BROUGHT FORWARD", brought_forward), ("PL-CY", f"PROFIT FOR THE YEAR FROM {fy_start:%d %b %Y}", current)):
        if amount:
            profit_cat["groups"][code] = {"code": code, "name": name, "accounts": [], "amount": amount}
            profit_cat["amount"] += amount
            equity["amount"] += amount
    if profit_cat["groups"]:
        equity["categories"]["PL"] = profit_cat

    def shape(side):
        return {"title": side["title"], "amount": _s(side["amount"]),
                "categories": [{"code": c["code"], "name": c["name"], "amount": _s(c["amount"]),
                                "groups": [{**g, "amount": _s(g["amount"]), "accounts": sorted(g["accounts"], key=lambda a: (a["code"], a["name"]))}
                                           for g in sorted(c["groups"].values(), key=lambda g: g["code"])]}
                               for c in sorted(side["categories"].values(), key=lambda c: c["code"])]}
    assets, liabilities = sides["1"]["amount"], sides["2"]["amount"]
    return {"books": books, "together": together, "asOf": as_of.isoformat(), "fiscalYearStart": fy_start.isoformat(),
            "assets": shape(sides["1"]), "liabilities": shape(sides["2"]), "equity": shape(sides["3"]),
            "totalAssets": _s(assets), "totalLiabilitiesAndEquity": _s(liabilities + equity["amount"]),
            "difference": _s(assets - liabilities - equity["amount"]), "currentYearProfit": _s(current), "profitBroughtForward": _s(brought_forward),
            # What the two profit lines add up, so each opens the income statement for its own days.
            "broughtForwardFrom": (first.isoformat() if (first := await first_posted_day(books)) and first < fy_start else None),
            "broughtForwardTo": (fy_start - timedelta(days=1)).isoformat()}


async def day_book(books: list[str], start: date, end: date, vtype: str | None, limit: int, offset: int) -> dict:
    from app.services.vouchers_service import voucher_out

    qs = Voucher.filter(book__in=books, status="posted", date__gte=start, date__lte=end)
    types = [t.strip().upper() for t in (vtype or "").split(",") if t.strip()]
    if types:
        qs = qs.filter(vtype__in=types)
    total = await qs.count()
    vouchers = await qs.order_by("date", "book", "created_at", "number").offset(offset).limit(limit)
    params = [*books, start.isoformat(), end.isoformat(), *types]
    totals = await _query(
        f"SELECT SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE v.status = 'posted' AND v.book IN ({','.join('?' for _ in books)}) AND v.date >= ? AND v.date <= ?"
        + (f" AND v.vtype IN ({','.join('?' for _ in types)})" if types else ""), params,
    )
    return {"books": books, "from": start.isoformat(), "to": end.isoformat(), "items": [await voucher_out(v) for v in vouchers], "total": total,
            "totalDebit": _s(_d(totals[0]["dr"]) if totals else ZERO), "totalCredit": _s(_d(totals[0]["cr"]) if totals else ZERO)}


BUCKETS = (("current", 0, 30), ("d31_60", 31, 60), ("d61_90", 61, 90), ("d91_120", 91, 120), ("over120", 121, 10 ** 6))


async def ageing(books: list[str], kind: str, as_of: date, account_ids: list[str] | None = None) -> dict:
    if kind not in ("customer", "supplier"):
        raise ValueError("Ageing is for customers or suppliers.")
    accounts = await (Account.filter(book__in=books, kind=kind, id__in=account_ids) if account_ids is not None else Account.filter(book__in=books, kind=kind))
    ids = [str(a.id) for a in accounts]
    lines: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start:chunk_start + 400]
        for r in await _query(
            f"SELECT l.account_id, v.date, l.debit, l.credit FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
            f"WHERE v.status = 'posted' AND v.date <= ? AND l.account_id IN ({','.join('?' for _ in chunk)}) ORDER BY v.date, v.created_at",
            [as_of.isoformat(), *chunk],
        ):
            day = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"])[:10])
            lines.setdefault(str(r["account_id"]), []).append((day, _d(r["debit"]), _d(r["credit"])))
    rows = []
    totals = {k: ZERO for k in ("balance", "overdue", *[b[0] for b in BUCKETS])}
    for account in accounts:
        entries = lines.get(str(account.id), [])
        if not entries:
            continue
        open_items, settled, last_payment = [], ZERO, None
        for day, debit, credit in entries:
            owed, paid = (debit, credit) if kind == "customer" else (credit, debit)
            if owed > 0:
                open_items.append([day, owed])
            if paid > 0:
                settled += paid
                last_payment = day
        for item in open_items:
            take = min(item[1], settled)
            item[1] -= take
            settled -= take
        balance = sum((i[1] for i in open_items), ZERO) - settled
        buckets = {b[0]: ZERO for b in BUCKETS}
        overdue = ZERO
        for day, amount in open_items:
            if amount <= 0:
                continue
            age = (as_of - day).days
            for name, low, high in BUCKETS:
                if low <= age <= high:
                    buckets[name] += amount
                    break
            if age > 30:
                overdue += amount
        rows.append({"accountId": str(account.id), "book": account.book, "code": account.code, "name": account.name, "partyRef": account.party_ref,
                     "dueDays": 30, "balance": _s(balance), "overdue": _s(overdue), "advance": _s(-balance if balance < 0 else ZERO),
                     "lastPayment": last_payment.isoformat() if last_payment else None, **{k: _s(v) for k, v in buckets.items()}})
        totals["balance"] += balance
        totals["overdue"] += overdue
        for k, v in buckets.items():
            totals[k] += v
    rows.sort(key=lambda r: -Decimal(r["balance"]))
    return {"asOf": as_of.isoformat(), "kind": kind, "books": books, "rows": rows, "totals": {k: _s(v) for k, v in totals.items()}}


async def statement(account_id: str, start: date | None, end: date) -> dict:
    """What a customer or supplier is sent: who they are, what they owed at the start, every entry with the balance after
    it, what is owed at the end and how old it is. The same figures as the ledger, read from their side."""
    from app.models import Supplier

    account = await Account.get_or_none(id=account_id)
    if not account:
        raise ValueError("That account doesn't exist.")
    if account.kind not in ("customer", "supplier"):
        raise ValueError("A statement of account is for a customer or a supplier.")
    book = await ledger(account_id, start, end)
    party = await Supplier.get_or_none(id=account.party_ref) if account.kind == "supplier" and account.party_ref else None
    address = ", ".join(p for p in (getattr(party, "address", None), getattr(party, "city", None)) if p)
    ages = await ageing([account.book], account.kind, end, [str(account.id)])
    return {
        **book,
        "kind": account.kind,
        "party": {"name": account.name, "code": getattr(party, "code", None) or account.code, "phone": getattr(party, "phone", None),
                  "address": address or None, "ntn": getattr(party, "ntn", None), "dueDays": getattr(party, "due_days", None)},
        "owedSide": "debit" if account.kind == "customer" else "credit",
        "ageing": ages["rows"][0] if ages["rows"] else None,
    }


async def book_summary(book: str, today: date) -> dict:
    """One book at a glance, for head office's list of books."""
    from app.services.vouchers_service import settings as book_settings

    books = [book]
    chart = await chart_map(books)
    sums = await _sums(books, end=today)
    bal = {k: dr - cr for k, (dr, cr) in sums.items()}

    def total(kinds, sign=1):
        return sign * sum((bal.get(k, ZERO) for k, m in chart.items() if m["kind"] in kinds), ZERO)

    month = await income_statement(books, False, today.replace(day=1), today)
    row = await book_settings(book)
    last_voucher = await Voucher.filter(book=book).order_by("-updated_at").first()
    return {
        "book": book, "cash": _s(total(("cash",))), "bank": _s(total(("bank", "wallet"))), "receivable": _s(total(("customer",))),
        "payable": _s(total(("supplier",), -1)), "monthNetSales": month["netSales"], "monthGrossProfit": month["grossProfit"],
        "monthNetProfit": month["netProfit"], "difference": _s(sum(bal.values(), ZERO)), "vouchers": await Voucher.filter(book=book).count(),
        "booksStart": row.books_start.isoformat() if row.books_start else None, "lockedUntil": row.locked_until.isoformat() if row.locked_until else None,
        "lastReceivedAt": row.last_received_at.isoformat() if row.last_received_at else None,
        "lastChangeAt": last_voucher.updated_at.isoformat() if last_voucher else None,
    }


async def dashboard(book: str | None) -> dict:
    from app.services import vouchers_service

    books, together = await resolve_books(book)
    today = shop_day()
    chart = await chart_map(books)
    bal = {k: dr - cr for k, (dr, cr) in (await _sums(books, end=today)).items()}
    head = await vouchers_service.settings(HEAD_OFFICE_BOOK)

    def natural(account_id: str) -> Decimal:
        value = bal.get(account_id, ZERO)
        return value if chart[account_id]["nature"] == "debit" else -value

    money_rows = [{"accountId": k, "book": m["book"], "code": m["code"], "name": (f"{m['book']} · " if together else "") + m["name"], "kind": m["kind"],
                   "balance": _s(natural(k))} for k, m in chart.items() if m["kind"] in ("cash", "bank", "wallet") and bal.get(k)]
    interoffice = sum((bal.get(k, ZERO) for k, m in chart.items() if m["kind"] == "interoffice"), ZERO)
    fy_start = fiscal_year_start(today, head.fiscal_start_month)
    month = await income_statement(books, together, today.replace(day=1), today)
    day = await income_statement(books, together, today, today)
    year = await income_statement(books, together, fy_start, today)
    stock = sum((bal.get(k, ZERO) for k, m in chart.items() if m["systemKey"] == "stock.main"), ZERO)
    transit = sum((bal.get(k, ZERO) for k, m in chart.items() if m["systemKey"] == "stock.transit"), ZERO)
    godown_value = ZERO
    if HEAD_OFFICE_BOOK in books:
        rows = await _query("SELECT COALESCE(SUM(b.qty * COALESCE(p.avg_cost, 0)), 0) AS v FROM (SELECT product_id, SUM(qty) AS qty FROM warehouse_stock_movements GROUP BY product_id) b JOIN products p ON p.id = b.product_id", [])
        godown_value = _d(rows[0]["v"]) if rows else ZERO
    summaries = [await book_summary(b, today) for b in books] if together else []
    return {
        "today": today.isoformat(), "books": books, "together": together, "money": sorted(money_rows, key=lambda r: (r["book"], r["code"])),
        "cashTotal": _s(sum((natural(k) for k, m in chart.items() if m["kind"] == "cash"), ZERO)),
        "bankTotal": _s(sum((natural(k) for k, m in chart.items() if m["kind"] in ("bank", "wallet")), ZERO)),
        "receivable": _s(sum((bal.get(k, ZERO) for k, m in chart.items() if m["kind"] == "customer"), ZERO)),
        "payable": _s(-sum((bal.get(k, ZERO) for k, m in chart.items() if m["kind"] == "supplier"), ZERO)),
        "day": {k: day[k] for k in ("netSales", "grossProfit", "netProfit")},
        "month": {k: month[k] for k in ("netSales", "costOfSales", "grossProfit", "grossMargin", "operatingExpenses", "netProfit")},
        "year": {"from": fy_start.isoformat(), **{k: year[k] for k in ("netSales", "grossProfit", "netProfit")}},
        "checks": {
            "trialBalanceDifference": _s(sum(bal.values(), ZERO)), "stockInBooks": _s(stock), "stockInTransit": _s(transit),
            "godownStockAtCost": _s(godown_value), "interofficeDifference": _s(interoffice) if together else None,
            "stockAccountId": None if together else next((k for k, m in chart.items() if m["systemKey"] == "stock.main"), None),
            "transitAccountId": None if together else next((k for k, m in chart.items() if m["systemKey"] == "stock.transit"), None),
            "interofficeAccountIds": [k for k, m in chart.items() if m["kind"] == "interoffice"],
        },
        "problemSources": [await vouchers_service.problem_source(p) for p in (head.posting_problems or [])[:20]],
        "drafts": await Voucher.filter(book__in=books, status="draft").count(),
        "bookSummaries": summaries,
        "settings": {"booksStart": head.books_start.isoformat() if head.books_start else None, "lockedUntil": head.locked_until.isoformat() if head.locked_until else None,
                     "fiscalStartMonth": head.fiscal_start_month, "lastPostingAt": head.last_posting_at.isoformat() if head.last_posting_at else None,
                     "lastPostingNote": head.last_posting_note, "postingProblems": head.posting_problems or []},
    }

# ── month by month ─────────────────────────────────────────────────────────────────────────────

def month_periods(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar months from start to end, the first and last cut to the range."""
    out, first = [], start.replace(day=1)
    while first <= end:
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(first, start), min(following - timedelta(days=1), end)))
        first = following
    return out


def month_label(start: date, end: date) -> tuple[str, bool]:
    last_day = ((start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    whole = start.day == 1 and end.day == last_day
    return (f"{start:%b %Y}" if whole else f"{start.day} to {end.day} {end:%b %Y}"), not whole


def money_flows(rows: list[dict]) -> dict[str, Decimal]:
    """Supplier and customer money in one period, from sums of debit and credit by voucher type and account kind."""
    out = {"purchases": ZERO, "returns": ZERO, "paid": ZERO, "creditSales": ZERO, "collected": ZERO}
    for r in rows:
        dr, cr, vtype = _d(r["dr"]), _d(r["cr"]), r["vtype"]
        if r["kind"] == "supplier":
            if vtype == "PV":
                out["purchases"] += cr
            elif vtype == "PRV":
                out["returns"] += dr
            elif vtype in ("CPV", "BPV"):
                out["paid"] += dr
        elif r["kind"] == "customer":
            if vtype == "SV":
                out["creditSales"] += dr - cr
            elif vtype != "OB":
                out["collected"] += cr - dr
    return out


def build_month_by_month(chart: dict[str, dict], periods: list[tuple[date, date]], figures: list[dict]) -> dict:
    """One column a month: sales, what they cost, expenses by group, the profit, and where money and stock stood at
    each month's end. `figures` holds, per month, the period's sums, its supplier and customer money flows, and the
    closing balances."""
    expense_groups: dict[str, str] = {}
    for meta in sorted(chart.values(), key=lambda m: m["groupCode"]):
        if meta["categoryCode"] in ("52", "53"):
            expense_groups.setdefault(meta["groupCode"], meta["groupName"])

    columns: list[dict] = []
    values: dict[str, list[Decimal]] = {}

    def put(key: str, value: Decimal) -> None:
        values.setdefault(key, []).append(value)

    for (start, end), f in zip(periods, figures):
        sums, flows, closing = f["sums"], f["flows"], f["closing"]

        def total(pred, credit: bool = False, source=sums) -> Decimal:
            out = ZERO
            for account_id, (dr, cr) in source.items():
                meta = chart.get(account_id)
                if meta and pred(meta):
                    out += (cr - dr) if credit else (dr - cr)
            return out

        label, partial = month_label(start, end)
        days = (end - start).days + 1
        columns.append({"key": f"{start:%Y-%m}", "label": label, "from": start.isoformat(), "to": end.isoformat(), "partial": partial, "days": days})
        gross = total(lambda m: m["categoryCode"] == "41" and m["groupCode"] != "4102", credit=True)
        returns = total(lambda m: m["systemKey"] == "sales.returns")
        discounts = total(lambda m: m["groupCode"] == "4102" and m["systemKey"] != "sales.returns")
        net_sales = gross - returns - discounts
        cogs = total(lambda m: m["categoryCode"] == "51" and m["groupCode"] == "5101")
        losses = total(lambda m: m["categoryCode"] == "51" and m["groupCode"] != "5101")
        other = total(lambda m: m["categoryCode"] == "42", credit=True)
        put("grossSales", gross)
        put("returns", -returns)
        put("discounts", -discounts)
        put("netSales", net_sales)
        put("salesPerDay", net_sales / days)
        put("cogs", -cogs)
        put("stockLosses", -losses)
        put("grossProfit", net_sales - cogs - losses)
        put("otherIncome", other)
        expenses = ZERO
        for code in expense_groups:
            amount = total(lambda m, code=code: m["groupCode"] == code)
            put(f"expense:{code}", -amount)
            expenses += amount
        put("expenses", -expenses)
        put("netProfit", net_sales - cogs - losses + other - expenses)
        put("purchases", flows["purchases"])
        put("supplierReturns", -flows["returns"])
        put("paidSuppliers", -flows["paid"])
        put("creditSales", flows["creditSales"])
        put("collected", flows["collected"])
        put("cash", total(lambda m: m["kind"] == "cash", source=closing))
        put("bank", total(lambda m: m["kind"] in ("bank", "wallet"), source=closing))
        put("stock", total(lambda m: m["systemKey"] in ("stock.main", "stock.transit"), source=closing))
        put("receivable", total(lambda m: m["kind"] == "customer", source=closing))
        put("payable", total(lambda m: m["kind"] == "supplier", credit=True, source=closing))

    def pct(numerator: str, index: int | None) -> str | None:
        top = sum(values[numerator], ZERO) if index is None else values[numerator][index]
        bottom = sum(values["netSales"], ZERO) if index is None else values["netSales"][index]
        return format((top / bottom * 100).quantize(Decimal("0.1")), "f") if bottom else None

    rows: list[dict] = []

    def row(section: str, key: str, label: str, kind: str = "money", strong: bool = False, hide_zero: bool = False) -> None:
        series = values.get(key, [])
        if hide_zero and not any(series):
            return
        if kind == "balance":
            whole = series[-1] if series else ZERO
        elif kind == "average":
            days = sum(c["days"] for c in columns)
            whole = sum(values.get("netSales", []), ZERO) / days if days else ZERO
        else:
            whole = sum(series, ZERO)
        rows.append({"section": section, "key": key, "label": label, "kind": kind, "strong": strong, "values": [_s(v) for v in series], "total": _s(whole)})

    def percent_row(section: str, key: str, label: str, numerator: str) -> None:
        rows.append({"section": section, "key": key, "label": label, "kind": "percent", "strong": False,
                     "values": [pct(numerator, i) for i in range(len(columns))], "total": pct(numerator, None) if columns else None})

    row("Sales", "grossSales", "Gross sales")
    row("Sales", "returns", "Returns", hide_zero=True)
    row("Sales", "discounts", "Discounts given", hide_zero=True)
    row("Sales", "netSales", "Net sales", strong=True)
    row("Sales", "salesPerDay", "Net sales a day", kind="average")
    row("Profit", "cogs", "Cost of goods sold")
    row("Profit", "stockLosses", "Stock losses and adjustments", hide_zero=True)
    row("Profit", "grossProfit", "Gross profit", strong=True)
    percent_row("Profit", "grossMargin", "Gross margin", "grossProfit")
    row("Profit", "otherIncome", "Other income", hide_zero=True)
    for code, name in expense_groups.items():
        row("Profit", f"expense:{code}", name.capitalize(), hide_zero=True)
    row("Profit", "expenses", "Total expenses", strong=True)
    row("Profit", "netProfit", "Net profit", strong=True)
    percent_row("Profit", "netMargin", "Net margin", "netProfit")
    row("Buying and money", "purchases", "Goods received from suppliers")
    row("Buying and money", "supplierReturns", "Sent back to suppliers", hide_zero=True)
    row("Buying and money", "paidSuppliers", "Paid to suppliers")
    row("Buying and money", "creditSales", "Sold on credit", hide_zero=True)
    row("Buying and money", "collected", "Collected from credit customers", hide_zero=True)
    row("At the month's end", "cash", "Cash in hand", kind="balance")
    row("At the month's end", "bank", "Bank and wallets", kind="balance")
    row("At the month's end", "stock", "Stock at cost", kind="balance")
    row("At the month's end", "receivable", "Customers owe", kind="balance", hide_zero=True)
    row("At the month's end", "payable", "Owed to suppliers", kind="balance")
    return {"from": periods[0][0].isoformat() if periods else None, "to": periods[-1][1].isoformat() if periods else None, "months": columns, "rows": rows}


async def month_by_month(books: list[str], start: date, end: date) -> dict:
    """The same report over one book or several added together. Head office's account with a branch and the branch's
    account with head office aren't money, stock or parties, so they never show here."""
    chart = await chart_map(books)
    periods = month_periods(start, end)
    marks = ",".join("?" for _ in books)
    figures = []
    for period_start, period_end in periods:
        flows = await _query(
            "SELECT v.vtype AS vtype, a.kind AS kind, SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l "
            "JOIN acc_vouchers v ON v.id = l.voucher_id JOIN acc_accounts a ON a.id = l.account_id "
            f"WHERE v.status = 'posted' AND v.book IN ({marks}) AND v.date >= ? AND v.date <= ? AND a.kind IN ('supplier', 'customer') GROUP BY v.vtype, a.kind",
            [*books, period_start.isoformat(), period_end.isoformat()],
        )
        figures.append({"sums": await _sums(books, start=period_start, end=period_end), "flows": money_flows(flows), "closing": await _sums(books, end=period_end)})
    return build_month_by_month(chart, periods, figures)
