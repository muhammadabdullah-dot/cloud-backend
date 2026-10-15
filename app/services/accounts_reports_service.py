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

from app.core import dialect, when
from app.core.pk_time import PKT, pk_day  # noqa: F401  (PKT is imported from here by other services)
from app.models import HEAD_OFFICE_BOOK, Account, AccountCategory, AccountGroup, AccountType, Voucher
from app.services.accounts_chart_service import money

ZERO = Decimal("0")
ALL = "ALL"
# Where a windowed sum stops being cheaper to seek than to hash. Measured, not guessed: see `dialect.hash_join`.
WIDE_WINDOW_DAYS = 60

# Earlier than any voucher that can exist, so that "no start date" becomes a date range the materialised view can
# answer. It is the first of a month on purpose: `sp_ledger_sums` reads the days before the first whole month from
# the voucher lines, and a floor on the 1st makes that piece empty rather than a scan from 1900.
LEDGER_FLOOR = date(1900, 1, 1)


def shop_day(at: datetime | None = None) -> date:
    """The Pakistan day an instant falls on, today when none is given (core/pk_time.py)."""
    return pk_day(at)


def _d(value) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _s(value: Decimal) -> str:
    return format(money(value), "f")


async def _query(sql: str, params: list) -> list[dict]:
    return await Tortoise.get_connection("default").execute_query_dict(sql, params)


async def all_books() -> list[str]:
    from app.models import Branch

    # Head office is a Branch row of its own as well as HEAD_OFFICE_BOOK, so it arrives twice and used to be
    # returned twice: ['HO', 'FC', 'GG', 'GT', 'HO', 'MT']. Harmless inside `book IN (...)`, and not harmless
    # anywhere that walks the list a book at a time, which would count head office's figures twice.
    codes = list(dict.fromkeys([HEAD_OFFICE_BOOK] + [b.code for b in await Branch.all().order_by("code")]))
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


def month_end(day: date) -> date:
    """The last day of that day's month."""
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def whole_months_in(start: date, end: date) -> tuple[date, date] | None:
    """The stretch of complete calendar months inside a range, or None when it holds none.

    `vw_ledger_month` is grained to a calendar month and the periods people pick are not: "last 12 months" runs
    6 October to 5 October, eleven whole months with a ragged piece at each end. Only the whole months can be
    answered from the view; the ends have to be read from the lines whatever happens.
    """
    first = start if start.day == 1 else (start.replace(day=1) + timedelta(days=32)).replace(day=1)
    last = end if end == month_end(end) else month_end(end.replace(day=1) - timedelta(days=1))
    return (first, last) if first <= last else None


async def _sums(books: list[str], start: date | None = None, end: date | None = None, before: date | None = None,
                account_ids: list[str] | None = None) -> dict[str, tuple[Decimal, Decimal]]:
    # THE FAST ROAD: the months are already added up in dbo.vw_ledger_month, so ask for them rather than add
    # 5.4 million voucher lines again. A year of all five books is 0.07s this way against 3.57s, and the Net
    # Profit tile on the Company Dashboard -- which asks this twice, for the period and the one before it --
    # takes the Sales section from 7.93s to 2.60s with every tile identical.
    #
    # Three conditions, and each one is load-bearing:
    #
    #   * SQL Server only. The view and the procedure do not exist on SQLite, which is what every branch server
    #     and thirteen of the checks in ops/checks run on.
    #   * only the plain windowed question. `before=` and an account filter have their own plans and are
    #     already quick; sending them here would be slower, not faster.
    #   * only when a whole calendar month is in the range. Without a whole month the procedure reads the same
    #     lines this function would have read and charges its own overhead on top: 0.53s against 0.27s for a
    #     30 day window, 0.05s against 0.04s for one day. Speeding up the twelve month view by slowing down
    #     today and this month is the wrong trade, and those are what people look at most.
    #   * `start=None` means "everything there has ever been, up to `end`", which is the shape the Accounts Desk
    #     asks for and which used to take the slow road because the guard insisted on a start date. A floor date
    #     earlier than any voucher makes it the same question: every month before this one comes from the view and
    #     only the ragged end is read from the lines. Measured on the whole company, everything to today: 4.64s
    #     against 0.16s, identical account for account across all 2,582 of them.
    floor = start if start is not None else LEDGER_FLOOR
    if (dialect.engine() == dialect.MSSQL and end is not None
            and before is None and account_ids is None and whole_months_in(floor, end)):
        rows = await _query("EXEC dbo.sp_ledger_sums @books = ?, @from = ?, @to = ?",
                            [",".join(books), floor.isoformat(), end.isoformat()])
        return {str(r["account_id"]): (money(_d(r["dr"])), money(_d(r["cr"]))) for r in rows}

    where, params = ["v.status = 'posted'", f"v.book IN ({dialect.codes(books)})"], list(books)
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    if end:
        where.append("v.date <= ?")
        params.append(end.isoformat())
    if before:
        where.append("v.date < ?")
        params.append(before.isoformat())
    if account_ids is not None and not account_ids:
        return {}
    if account_ids:
        # SQL Server takes 2,100 parameters and no more. A party summary asks this for every customer or supplier
        # at once, which is thousands, and the whole call failed with "COUNT field incorrect" -- an error that
        # names the symptom and not the cause. Over the ceiling the list is asked for in pieces and the pieces
        # added up; each piece seeks on idx_avl_account, so the cost is the rows, not the number of queries.
        ceiling = 1800 - len(params)
        if len(account_ids) > ceiling:
            merged: dict[str, tuple[Decimal, Decimal]] = {}
            for at in range(0, len(account_ids), ceiling):
                part = await _sums(books, start, end, before, account_ids[at:at + ceiling])
                for key, (dr, cr) in part.items():
                    was_dr, was_cr = merged.get(key, (ZERO, ZERO))
                    merged[key] = (was_dr + dr, was_cr + cr)
            return merged
        where.append(f"l.account_id IN ({dialect.marks(account_ids)})")
        params.extend(account_ids)
    # Which join, by how wide the window is. With both ends pinned, the planner seeks acc_vouchers by date and
    # looks the lines up a voucher at a time: right for a month, wrong for a year, and the cost is a straight line
    # in the number of vouchers. A year of all five books took 13.91s that way and 3.22s as a hash join, which is
    # the difference between the dashboard's twelve month view answering and timing out. Below the crossover the
    # hint is the slower choice, so it is not asked for. The measurements are in `dialect.hash_join`.
    wide = start is not None and end is not None and (end - start).days > WIDE_WINDOW_DAYS
    join = dialect.hash_join() if wide else "JOIN"
    rows = await _query(
        f"SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l "
        f"{join} acc_vouchers v ON v.id = l.voucher_id WHERE {' AND '.join(where)} GROUP BY l.account_id", params,
    )
    return {str(r["account_id"]): (money(_d(r["dr"])), money(_d(r["cr"]))) for r in rows}


async def _opening_and_period(books: list[str], start: date | None, end: date,
                              ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, tuple[Decimal, Decimal]]]:
    """Both halves of a trial balance in one pass over the lines.

    `_sums(before=start)` then `_sums(start, end)` reads everything up to `end` twice: once for the opening
    balances and once for the period. The rows are the same rows, so one pass splits them on the way past, which
    halves the work for every report that needs an opening balance -- trial balance and balance sheet, the two
    that sat closest to the client's 15 second limit.

    **Which side a line falls on is a column to group by, not a CASE inside four separate sums.** Four sums each
    testing the date means four comparisons per line, 21.6 million of them over the 5.4M lines up to September,
    and three of every four then add nothing. Deciding the side once and grouping by the answer costs one
    comparison and two sums: 9.41s against 7.47s on all five books, and 14.30s against 9.05s while another
    session had the machine. It returns the same two dicts, checked against the old shape over six windows
    including one book on its own, no start date at all, and a window with nothing in it.
    """
    if start is None:
        return {}, await _sums(books, end=end)

    # On SQL Server the reasoning above is now the wrong way round. One pass was right when both halves had to be
    # read from the voucher lines, because it read them once instead of twice. They no longer are: "everything
    # before the start" and "the start to the end" are both shapes `sp_ledger_sums` answers out of the
    # materialised view, so two cheap calls beat one expensive pass. Over the fiscal year on all five books,
    # 7.84s against 0.32s -- 24 times -- with both halves identical account for account.
    #
    # The one-pass query below is kept for SQLite, which has neither the view nor the procedure, and that is where
    # it is still the better shape.
    if dialect.engine() == dialect.MSSQL:
        return (await _sums(books, end=start - timedelta(days=1)),
                await _sums(books, start=start, end=end))

    marks = dialect.codes(books)
    rows = await _query(
        "SELECT account_id, side, SUM(debit) AS dr, SUM(credit) AS cr FROM ("
        "  SELECT l.account_id AS account_id, l.debit AS debit, l.credit AS credit, "
        "         CASE WHEN v.date < ? THEN 0 ELSE 1 END AS side "
        "    FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"   WHERE v.status = 'posted' AND v.book IN ({marks}) AND v.date <= ?"
        ") z GROUP BY account_id, side",
        [start.isoformat(), *books, end.isoformat()],
    )
    opening, period = {}, {}
    for row in rows:
        dr, cr = money(_d(row["dr"])), money(_d(row["cr"]))
        # Only where there is something, so the shape matches what two separate _sums calls returned: an account
        # with no opening entries was simply absent from the opening dict, and callers rely on that.
        if not (dr or cr):
            continue
        (opening if int(row["side"]) == 0 else period)[str(row["account_id"])] = (dr, cr)
    return opening, period


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


async def sums_by_kind(books: list[str], kind: str, end: date) -> dict[str, tuple[Decimal, Decimal]]:
    """Every account of one kind, summed to a date, without naming them one by one.

    A party summary covers every customer or every supplier, which is thousands of ids. Passing them as
    parameters broke SQL Server's 2,100 ceiling, and chunking round it turned one scan into many: the payable
    summary took 231 seconds. `kind` is a column on acc_accounts, so the database can do the filtering itself
    and the whole thing is one pass. The same change took ageing from 282 seconds to under one.

    **The join order is pinned on purpose, and it is the only hint in this file** (`dialect.loop_join`,
    which is a plain INNER JOIN on SQLite because SQLite rejects the word). Written without one, and with
    statistics freshly rebuilt, the planner read all 5,412,269 ledger lines to find the 25,811 on head office's
    suppliers: 87,945 page reads, 687 MB. Starting from acc_accounts and seeking idx_avl_account once per party
    reads 5,442 pages, 43 MB -- sixteen times less -- for the same 388 rows and the same total to the paisa. Warm
    both are a fraction of a second, because warm pages are nearly free; the reads are what the first screen of
    the morning pays, and 687 MB of them is what made the receivable and payable summaries take 34 and 33 seconds.

    Naming the party ids instead was measured and is worse than either: 956 parameters sends SQL Server seeking
    idx_avl_account per id and then looking each line's voucher up a row at a time, and the query had not returned
    after nine minutes. That is the same trap `cash_flow` records above.

    The hint is safe in the direction this grows. The outer input is the accounts of one kind -- thousands at most,
    out of 7,301 -- and the inner side is an index seek over a range, so more parties means more seeks of the same
    cost rather than a plan that falls over.
    """
    marks = dialect.codes(books)
    rows = await _query(
        "SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr "
        "FROM acc_accounts a "
        f"{dialect.loop_join()} acc_voucher_lines l ON l.account_id = a.id "
        "INNER JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE a.book IN ({marks}) AND a.kind = {dialect.mark('varchar(20)')} AND v.status = 'posted' AND v.book IN ({marks}) "
        "AND v.date <= ? "
        "GROUP BY l.account_id", [*books, kind, *books, end.isoformat()],
    )
    return {str(r["account_id"]): (money(_d(r["dr"])), money(_d(r["cr"]))) for r in rows}


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
    opening_sums, period_sums = await _opening_and_period(books, start, end)
    opening = _merge(chart, opening_sums, together)
    period = _merge(chart, period_sums, together)
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


async def ledger(account_id: str, start: date | None, end: date, include_pdc: bool = False,
                 books: list[str] | None = None, limit: int | None = None, offset: int = 0) -> dict:
    """One account's entries. With `books`, the same account across all of them, added together.

    WHY IT PAGES
    ------------
    `limit=None` reads the whole ledger, which is what this always did and still does for every caller that does
    not ask otherwise. It is also why this screen was the one report the browser gave up on. Their busiest
    account, CASH SALE WALKING CUSTOMER, carries 1,418,437 voucher lines in head office's book alone and
    2,580,863 across all five, and reading it whole measured 207 seconds: 36 for the entries, 83 for the contra
    accounts -- 2,659 separate round trips, 400 vouchers at a time -- and the rest building two and a half
    million dictionaries that a browser was never going to be sent.

    With `limit`, every one of those costs collapses to the page: the entries are fetched by OFFSET/FETCH, the
    contra lookup has at most `limit` vouchers to ask about rather than 1,063,208, and nothing else is built.

    The three figures that are NOT the page's are read separately, because a page cannot know them: `total`, and
    `totalDebit`/`totalCredit`, come from one aggregate over the whole range, and `closing` from those. Summing
    the page would make page three of a ledger report page three's totals as if they were the account's, which is
    the kind of wrong that gets copied into a return.

    `opening` is the balance before `start`, as before. A page's own running balance starts from `opening` plus
    everything before `offset`, which is one more aggregate rather than a read of the rows being skipped.

    The chart is per book: head office and each branch hold their own row for the same account, under the same
    code. So "the whole company" on this screen means every book's copy of the account read as one ledger, which is
    what the trial balance already does for every account at once. Without it this is the only screen in the
    accounts section that makes somebody pick a branch before it will say anything.
    """
    account = await Account.get_or_none(id=account_id)
    if not account:
        raise ValueError("That account doesn't exist.")
    wanted = [str(account_id)]
    scope = [account.book]
    if books and len(books) > 1:
        # The same code in every book the person may read. One row per book at most, so naming them is a handful of
        # parameters rather than a list that could reach SQL Server's ceiling.
        siblings = await Account.filter(code=account.code, book__in=books).values_list("id", flat=True)
        if siblings:
            wanted = sorted({str(s) for s in siblings} | {str(account_id)})
            scope = list(books)
    chart = await chart_map(scope)
    meta = dict(chart[str(account_id)])
    if len(wanted) > 1:
        meta = {**meta, "book": "ALL", "bookCount": len(wanted)}
    opening = ZERO
    if start:
        sums = await _sums(scope, before=start, account_ids=wanted)
        opening = sum((dr - cr for dr, cr in (sums.get(a, (ZERO, ZERO)) for a in wanted)), ZERO)
    marks = dialect.marks(wanted)
    where = ["v.status = 'posted'", f"l.account_id IN ({marks})", "v.date <= ?"]
    params = [*wanted, end.isoformat()]
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    frm = "FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id"
    # `l.id` is a deterministic last key, and it is what makes paging possible at all. Two lines that tie on date,
    # created_at, number and line_no used to come back in whatever order the engine chose that run: harmless when
    # the whole ledger is read at once, and not harmless when it is read a page at a time, where a tie split
    # across a page boundary shows one row twice and another not at all. Added to the unpaged path too, so the two
    # reads agree with each other rather than only each with itself.
    order = "ORDER BY v.date, v.created_at, v.number, l.line_no, l.id"
    columns = ("SELECT v.id AS voucher_id, v.number, v.vtype, v.date, v.description AS vdesc, v.reference_no AS vref, "
               "v.cheque_no, v.auto, l.description, l.reference_no, l.debit, l.credit, l.line_no")
    select = f"{columns} {frm} WHERE {' AND '.join(where)}"

    skipped = ZERO
    if limit is None:
        rows = await _query(f"{select} {order}", params)
        total = len(rows)
        total_dr = sum((money(_d(r["debit"])) for r in rows), ZERO)
        total_cr = sum((money(_d(r["credit"])) for r in rows), ZERO)
    else:
        whole = await _query(
            f"SELECT COUNT(*) AS n, SUM(l.debit) AS dr, SUM(l.credit) AS cr {frm} "
            f"WHERE {' AND '.join(where)}", params)
        total = int(whole[0]["n"] or 0)
        # Safe as one SUM rather than a sum of rounded rows only because debit and credit are decimal(16, 2): at
        # two places `money()` is the identity, so rounding each row and rounding the total give the same figure.
        # It would not be safe on a column with more places, and the ledger would stop balancing by a paisa.
        total_dr, total_cr = money(_d(whole[0]["dr"])), money(_d(whole[0]["cr"]))
        if offset > 0:
            head, head_params = dialect.page(offset, 0)
            prior = await _query(
                f"SELECT SUM(x.dr) AS dr, SUM(x.cr) AS cr FROM "
                f"(SELECT l.debit AS dr, l.credit AS cr {frm} WHERE {' AND '.join(where)} {order} {head}) x",
                [*params, *head_params])
            skipped = money(_d(prior[0]["dr"])) - money(_d(prior[0]["cr"]))
        # Two queries, not one, and this is the whole difference between a page that answers and a page that
        # does not. Asking for the entry columns and the page in one statement made SQL Server fetch every one of
        # the 2,580,863 joined rows -- `v.description`, `l.description`, both reference numbers and the cheque
        # number are in no index, so each row costs a lookup into the table -- sort all of them, and then throw
        # away all but a hundred. Measured at 53.44 seconds for 100 rows.
        #
        # So the order is settled first over narrow columns the indexes already carry, which yields a hundred
        # line ids, and only those hundred are read in full. The second read is by primary key and costs nothing.
        clause, page_params = dialect.page(limit, offset)
        ids = [str(r["id"]) for r in await _query(
            f"SELECT l.id {frm} WHERE {' AND '.join(where)} {order} {clause}", [*params, *page_params])]
        if ids:
            rows = await _query(f"{columns} {frm} WHERE l.id IN ({dialect.marks(ids)}) {order}", ids)
        else:
            rows = []
    voucher_ids = sorted({str(r["voucher_id"]) for r in rows})
    others: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for chunk_start in range(0, len(voucher_ids), 400):
        chunk = voucher_ids[chunk_start:chunk_start + 400]
        for line in await _query(f"SELECT voucher_id, account_id, debit, credit FROM acc_voucher_lines WHERE voucher_id IN ({dialect.marks(chunk)})", chunk):
            others.setdefault(str(line["voucher_id"]), []).append((str(line["account_id"]), _d(line["debit"]), _d(line["credit"])))
    # Where this page starts: the balance before the period, plus every entry the page skipped over.
    running = opening + skipped
    entries = []
    for r in rows:
        debit, credit = money(_d(r["debit"])), money(_d(r["credit"]))
        running += debit - credit
        # Every copy of the account, not just the one that was clicked: with the books combined, the other books'
        # rows for the same account are this account too, not the other side of the entry.
        contra_ids = list(dict.fromkeys(a for a, d, c in others.get(str(r["voucher_id"]), [])
                                        if a not in wanted and ((c > 0) if debit > 0 else (d > 0))))
        contra = [chart.get(a, {}).get("name", "?") for a in contra_ids]
        description = r["description"] if r["description"] and r["description"] != "__header__" else r["vdesc"]
        entries.append({
            "voucherId": str(r["voucher_id"]), "number": r["number"], "vtype": r["vtype"], "date": str(r["date"])[:10],
            "description": description, "referenceNo": r["reference_no"] or r["vref"], "chequeNo": r["cheque_no"], "auto": bool(r["auto"]),
            "debit": _s(debit), "credit": _s(credit), "balance": _s(running), "against": ", ".join(contra)[:200] if contra else None,
            "againstAccounts": [{"accountId": a, "name": chart.get(a, {}).get("name", "?")} for a in contra_ids[:8]],
        })
    # The account's closing balance, not the page's. Unpaged these are the same number, because `running` ends at
    # exactly opening plus every debit less every credit; paged they are not, and it is the account's that belongs
    # on the screen.
    closing = opening + total_dr - total_cr
    return {"account": meta, "from": start.isoformat() if start else None, "to": end.isoformat(), "opening": _s(opening),
            "entries": entries, "totalDebit": _s(total_dr), "totalCredit": _s(total_cr), "closing": _s(closing),
            "total": total, "limit": limit, "offset": offset, "pdc": []}


SECTION_OF_CATEGORY = {"41": "sales", "51": "cost", "42": "other", "52": "operating", "53": "financial"}
SECTION_TITLES = {"sales": "Revenue", "cost": "Cost of sales", "other": "Other income", "operating": "Operating expenses", "financial": "Financial expenses"}


async def income_statement(books: list[str], together: bool, start: date, end: date) -> dict:
    chart = await chart_map(books)
    return _build_income_statement(chart, await _sums(books, start=start, end=end), books, together, start, end)




async def income_statement_pair(books: list[str], together: bool, start: date, end: date,
                                prev_start: date, prev_end: date) -> tuple[dict, dict]:
    """This period's income statement and the period before it, for a tile that shows the change.

    What this saves is the chart: `chart_map` reads the same 7,301 accounts and their groups, categories and types,
    and calling `income_statement` twice read it twice for nothing.

    WHAT IT DELIBERATELY DOES NOT DO, AND WHY
    -----------------------------------------
    The obvious next step is `_opening_and_period`'s trick: one pass over the lines with a CASE per period instead of
    two passes. It was written, it gave identical figures, and it was **slower**, so it was taken out. Measured over
    six interleaved rounds on their ledger, two adjacent 30 day periods:

        two passes   3.11s        one pass, plain join   6.16s        one pass, derived table   7.69s

    The reason the opening-balance case wins and this one loses is the shape of the ranges. An opening balance and
    its period are nested, so one pass reads exactly what two passes read. Two adjacent periods are not: one pass has
    to widen the date filter to cover both, which doubles the vouchers it seeks and the lines it aggregates, and the
    four CASEs add per-row work on top. Narrow and twice beats wide and once here.

    A first, careless measurement said the opposite (11.89s against 5.80s). It was one run of each on a machine whose
    answers for the same query range over 3x, and the ranking inverted as soon as it was run round robin. Hence the
    numbers above, and hence this note.
    """
    chart = await chart_map(books)
    return (_build_income_statement(chart, await _sums(books, start=start, end=end), books, together, start, end),
            _build_income_statement(chart, await _sums(books, start=prev_start, end=prev_end), books, together,
                                    prev_start, prev_end))


def _build_income_statement(chart: dict[str, dict], sums: dict[str, tuple[Decimal, Decimal]], books: list[str],
                            together: bool, start: date, end: date) -> dict:
    merged = _merge(chart, sums, together)
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


def _income_total(chart: dict[str, dict], sums: dict[str, tuple[Decimal, Decimal]]) -> Decimal:
    """The profit in a set of sums: what the income statement accounts in it came to, credits less debits.

    Takes sums rather than fetching them, because the balance sheet needs this twice over two halves of one pass.
    """
    total = ZERO
    for account_id, (dr, cr) in sums.items():
        meta = chart.get(account_id)
        if meta and meta["statement"] == "income":
            total += cr - dr
    return total


async def balance_sheet(books: list[str], together: bool, as_of: date, fiscal_start_month: int) -> dict:
    chart = await chart_map(books)
    fy_start = fiscal_year_start(as_of, fiscal_start_month)
    # One pass over the lines, not three. This needs the cumulative total per account, the profit before the year
    # started and the profit since, and those were three separate reads of the same rows: everything up to `as_of`,
    # everything up to the day before `fy_start`, and `fy_start` onwards. The last two add up to the first, so
    # splitting one pass at `fy_start` gives all three. 19.5s to 7.0s on all five books, and it is the same helper
    # the trial balance already uses for the same reason.
    before_year, since_year = await _opening_and_period(books, fy_start, as_of)
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for half in (before_year, since_year):
        for account_id, (dr, cr) in half.items():
            was_dr, was_cr = totals.get(account_id, (ZERO, ZERO))
            totals[account_id] = (was_dr + dr, was_cr + cr)
    merged = _merge(chart, totals, together)
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
    brought_forward = _income_total(chart, before_year)
    current = _income_total(chart, since_year)
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
    from app.services.vouchers_service import vouchers_out

    # The rows and their totals are two separate reads, so they narrow through the same pair in core/when and must
    # be changed together: a page whose figures do not add up to its own rows is worse than one without a time.
    qs = when.narrow_vouchers(Voucher.filter(book__in=books, status="posted"), start, end)
    types = [t.strip().upper() for t in (vtype or "").split(",") if t.strip()]
    if types:
        qs = qs.filter(vtype__in=types)
    total = await qs.count()
    vouchers = await qs.order_by("date", "book", "created_at", "number").offset(offset).limit(limit)
    lo, hi = when.voucher_bound(">=", start), when.voucher_bound("<=", end)
    params = [*books, *(b[1] for b in (lo, hi) if b), *types]
    totals = await _query(
        f"SELECT SUM(l.debit) AS dr, SUM(l.credit) AS cr FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
        f"WHERE v.status = 'posted' AND v.book IN ({dialect.codes(books)})"
        + "".join(f" AND {b[0]}" for b in (lo, hi) if b)
        + (f" AND v.vtype IN ({dialect.codes(types)})" if types else ""), params,
    )
    return {"books": books, "from": start.isoformat(), "to": end.isoformat(), "items": await vouchers_out(vouchers), "total": total,
            "totalDebit": _s(_d(totals[0]["dr"]) if totals else ZERO), "totalCredit": _s(_d(totals[0]["cr"]) if totals else ZERO)}


BUCKETS = (("current", 0, 30), ("d31_60", 31, 60), ("d61_90", 61, 90), ("d91_120", 91, 120), ("over120", 121, 10 ** 6))


async def ageing(books: list[str], kind: str, as_of: date, account_ids: list[str] | None = None) -> dict:
    if kind not in ("customer", "supplier"):
        raise ValueError("Ageing is for customers or suppliers.")
    accounts = await (Account.filter(book__in=books, kind=kind, id__in=account_ids) if account_ids is not None else Account.filter(book__in=books, kind=kind))
    ids = [str(a.id) for a in accounts]
    lines: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
    # One query for every party, not one per four hundred of them. The chunked version re-scanned the lines for
    # each chunk, which is why this took 282 seconds even after the daily grouping: the work was the scanning,
    # not the rows. `kind` and `book` are conditions the database can apply itself, so it does.
    marks = dialect.codes(books)
    where = [f"a.book IN ({marks})", f"a.kind = {dialect.mark('varchar(20)')}", "v.status = 'posted'", "v.date <= ?"]
    params: list = [*books, kind, as_of.isoformat()]
    if account_ids is not None:
        if not account_ids:
            return {"rows": [], "totals": {k: _s(ZERO) for k in ("balance", "overdue", *[b[0] for b in BUCKETS])},
                    "buckets": [b[0] for b in BUCKETS]}
        where.append(f"l.account_id IN ({dialect.marks(account_ids)})")
        params.extend(account_ids)
    for r in await _query(
        "SELECT l.account_id AS account_id, v.date AS date, SUM(l.debit) AS debit, SUM(l.credit) AS credit "
        "FROM acc_voucher_lines l "
        "JOIN acc_vouchers v ON v.id = l.voucher_id "
        "JOIN acc_accounts a ON a.id = l.account_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY l.account_id, v.date ORDER BY v.date", params,
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


async def sums_by_book(books: list[str], end: date) -> dict[str, dict[str, tuple[Decimal, Decimal]]]:
    """Every book's position at `end`, in one pass instead of one scan each.

    `/accounts/books` called `book_summary` per book, and each one summed that book's whole history. Five books
    took 23 seconds, which is past the client's 15 second limit -- so the request failed, and `BookSwitcher` fell
    back to its single hardcoded "Head Office" option. The branch filter looked unbuilt when it was only timing
    out. One query grouped by book answers all of them.
    """
    # The same road `_sums` takes, for the same reason: everything up to a date is whole calendar months plus a
    # ragged tail, so the materialised view answers all but the tail. `sp_ledger_sums` returns the book because an
    # account belongs to exactly one, which makes this the identical question with one more column. 4.74s to 0.2s,
    # and it is most of what `/accounts/books` spends.
    if dialect.engine() == dialect.MSSQL and whole_months_in(LEDGER_FLOOR, end):
        rows = await _query("EXEC dbo.sp_ledger_sums @books = ?, @from = ?, @to = ?",
                            [",".join(books), LEDGER_FLOOR.isoformat(), end.isoformat()])
    else:
        marks = dialect.codes(books)
        rows = await _query(
            "SELECT v.book AS book, l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr "
            "FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
            f"WHERE v.status = 'posted' AND v.book IN ({marks}) AND v.date <= ? "
            "GROUP BY v.book, l.account_id", [*books, end.isoformat()],
        )
    out: dict[str, dict[str, tuple[Decimal, Decimal]]] = {book: {} for book in books}
    for row in rows:
        out.setdefault(str(row["book"]).strip(), {})[str(row["account_id"])] = (money(_d(row["dr"])), money(_d(row["cr"])))
    return out


async def book_summary(book: str, today: date, sums: dict[str, tuple[Decimal, Decimal]] | None = None) -> dict:
    """One book at a glance, for head office's list of books.

    `sums` lets a caller that is doing every book hand in that book's figures from one batched query. Left out,
    it falls back to asking for this book alone, so a single-book caller still works.
    """
    from app.services.vouchers_service import settings as book_settings

    books = [book]
    chart = await chart_map(books)
    if sums is None:
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
    raw_sums = await _sums(books, end=today)
    bal = {k: dr - cr for k, (dr, cr) in raw_sums.items()}
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
        # Valued at what each movement itself says a unit cost, which is what `StockMovement.unit_cost` is for, and
        # only falling back to the product when a movement has none. It used to value everything at
        # `products.avg_cost`, which `items_service` deliberately leaves alone on an update so that an item import
        # cannot overwrite a cost that receiving maintains -- so avg_cost is null on 67,803 of 67,804 products and
        # this check reported the godown as holding Rs 0.00 whatever was in it. Head office holds Rs 9.9 million.
        rows = await _query(
            "SELECT COALESCE(SUM(b.value), 0) AS v FROM ("
            "  SELECT m.product_id,"
            "         SUM(m.qty * COALESCE(m.unit_cost, COALESCE(p.avg_cost, 0))) AS value"
            "    FROM warehouse_stock_movements m"
            "    LEFT JOIN products p ON p.id = m.product_id"
            "   GROUP BY m.product_id) b", [])
        godown_value = _d(rows[0]["v"]) if rows else ZERO
    # Hand each book the figures already in `raw_sums`, which is what `book_summary`'s `sums` argument is for and
    # what its docstring describes. Without it this read the ledger once for all five books and then once more
    # inside each of the five -- the same 5,439,192 lines, six times, for one screen. Measured on the whole
    # company view: 6.66 seconds for the five summaries against 1.98 with the figures handed in.
    #
    # Filtered per book, not handed the whole dict, because `book_summary` ends with `sum(bal.values())` for that
    # book's trial-balance difference. Give it every book's accounts and that line quietly reports the company's
    # difference on each of the five rows.
    summaries = [await book_summary(b, today, sums={k: v for k, v in raw_sums.items()
                                                    if chart.get(k, {}).get("book") == b})
                 for b in books] if together else []
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
    marks = dialect.codes(books)
    window = [*books, start.isoformat(), end.isoformat()]
    month = dialect.year_month("v.date")
    # Both ends of the window are pinned, which is the shape the planner gets wrong past about two months: it seeks
    # the vouchers by date and looks their lines up one voucher at a time. Jan to Mar took 20.2s and a year 80.0s,
    # against a browser that gives up at 15. Same rule and the same measurements as `_sums`; `dialect.hash_join`.
    join = dialect.hash_join() if (end - start).days > WIDE_WINDOW_DAYS else "JOIN"

    # This used to run three queries per month: the flows, that month's sums, and the closing balance. The closing
    # one was `_sums(books, end=period_end)` with no start, so every month re-summed the books from the beginning
    # of time -- twelve months of a year view meant twelve ever-longer scans of 5.4 million lines, and 63 seconds
    # against a client that gives up at 15.
    #
    # The same numbers come out of three queries in total. A closing balance is just the opening balance plus every
    # month since, so it is carried forward in `running` rather than asked for again. `month_periods` cuts calendar
    # months to the range, so each period is one 'YYYY-MM' and the grouping lines up exactly.
    # The most expensive query in the accounts section, and the one the materialised view was made for: it groups
    # 5,439,192 voucher lines by month and account. Over the two years this screen opens on, 17.13 seconds against
    # 0.17 from the view, for the identical 16,191 rows.
    #
    # `sp_ledger_by_month` does the same three-way cut as `sp_ledger_sums`: whole calendar months out of the view,
    # the ragged piece at each end out of the lines. It is NOT a plain read of the view, because the view holds
    # whole months and this range usually ends mid-month -- reading October's row for a range that stops on the
    # 5th would quietly add the rest of October, and it would only start being wrong the day somebody picked a
    # past date. Checked against the lines in seven window shapes, ragged ends included: identical every time.
    #
    # Guarded on a whole month being present for the same reason `_sums` is: without one the procedure reads the
    # very lines this query reads and charges its own overhead, 0.37s against 0.23s.
    opening = await _sums(books, before=start)
    by_month: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    flows_by_month: dict[str, list[dict]] = {}

    if dialect.engine() == dialect.MSSQL and whole_months_in(start, end):
        # One call for both. `sp_ledger_by_month` returns a row per month, voucher type and account, which is the
        # grain the party flows need; the account movements are the same rows summed over the voucher type. That
        # is why `vtype` was put in the view's key -- the flows were the one query on this screen still reading
        # the voucher lines, 5.40s of the 7.15 it took.
        #
        # The account kinds come from `chart`, already loaded above, rather than from a third join to
        # acc_accounts. Anything not in the chart is not in these books, so it cannot be one of their parties.
        rows = await _query("EXEC dbo.sp_ledger_by_month @books = ?, @from = ?, @to = ?",
                            [",".join(books), start.isoformat(), end.isoformat()])
        for row in rows:
            # `ym` is char(7). Trimmed because it is matched against `f"{period_start:%Y-%m}"` and a trailing
            # space would not raise -- every month would simply come out empty.
            ym, account_id = str(row["ym"]).strip(), str(row["account_id"])
            dr, cr = money(_d(row["dr"])), money(_d(row["cr"]))
            slot = by_month.setdefault(ym, {})
            # Added, not assigned: there is now a row per voucher type, so an account appears several times in
            # one month and assigning would keep whichever came last.
            was_dr, was_cr = slot.get(account_id, (ZERO, ZERO))
            slot[account_id] = (was_dr + dr, was_cr + cr)
            kind = chart.get(account_id, {}).get("kind")
            if kind in ("supplier", "customer"):
                flows_by_month.setdefault(ym, []).append(
                    {"ym": ym, "vtype": row["vtype"], "kind": kind, "dr": dr, "cr": cr})
    else:
        per_month = await _query(
            f"SELECT {month} AS ym, l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr "
            f"FROM acc_voucher_lines l {join} acc_vouchers v ON v.id = l.voucher_id "
            f"WHERE v.status = 'posted' AND v.book IN ({marks}) AND v.date >= ? AND v.date <= ? "
            f"GROUP BY {month}, l.account_id", window,
        )
        flow_rows = await _query(
            f"SELECT {month} AS ym, v.vtype AS vtype, a.kind AS kind, SUM(l.debit) AS dr, SUM(l.credit) AS cr "
            f"FROM acc_voucher_lines l {join} acc_vouchers v ON v.id = l.voucher_id JOIN acc_accounts a ON a.id = l.account_id "
            f"WHERE v.status = 'posted' AND v.book IN ({marks}) AND v.date >= ? AND v.date <= ? "
            f"AND a.kind IN ('supplier', 'customer') GROUP BY {month}, v.vtype, a.kind", window,
        )
        for row in per_month:
            by_month.setdefault(row["ym"], {})[str(row["account_id"])] = (money(_d(row["dr"])), money(_d(row["cr"])))
        for row in flow_rows:
            flows_by_month.setdefault(row["ym"], []).append(row)

    figures, running = [], dict(opening)
    for period_start, _period_end in periods:
        key = f"{period_start:%Y-%m}"
        sums = by_month.get(key, {})
        for account_id, (dr, cr) in sums.items():
            was_dr, was_cr = running.get(account_id, (ZERO, ZERO))
            running[account_id] = (was_dr + dr, was_cr + cr)
        # A copy, because `running` keeps moving and every month keeps its own closing position.
        figures.append({"sums": sums, "flows": money_flows(flows_by_month.get(key, [])), "closing": dict(running)})
    return build_month_by_month(chart, periods, figures)
