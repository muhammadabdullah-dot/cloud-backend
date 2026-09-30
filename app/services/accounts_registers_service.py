"""The registers: the reports an accountant runs, as against the statements the books produce.

`accounts_reports_service` answers "what do the books say" — trial balance, income statement, balance sheet, ledger.
These answer "who owes what, who was paid when, and what is still outstanding", which is the other half of an
accountant's day and the half we did not have. Every one of them is a report the old software has under
Report > Account Report and we did not, or had only in part.

Where each came from, so the shape can be checked against theirs rather than argued about:

    Cash Flow Statement          money in and out of one cash or bank account, by who it came from or went to
    Account Summary              the trial balance pivoted to account, group, category or type, with a balance filter
    Voucher Detail               their ten voucher kinds, which are our four money types split by who the other side is
    Date Wise Payment            what went out to suppliers, day by day, cash apart from cheque
    Date Wise Receipt            what came in from customers, day by day, cash apart from cheque
    Receivable / Payable Summary what each party owes now, less the post dated cheques already in hand
    Supplier Balance Detail      the same, invoice by invoice, aged against that supplier's own credit days
    Daily Operation Report       the deliveries in a window, what was paid against each and what is left

**Everything reads the books, not the branch's own tables.** Head office holds no Party, no Supplier ledger and no
till of its own; it holds vouchers, and a voucher is what actually moved. A report built off the vouchers agrees
with the trial balance by construction, and one built off anything else has to be reconciled to it forever.

**Money is added in SQL, never in an ORM annotate.** `annotate(Sum(...)).values(...)` groups by row rather than over
the set and hands back one row's figures wearing a total's name, which is a bug that reads as correct. The house
pattern is `accounts_reports_service._sums`, and these follow it.

**Sign convention.** A balance here is positive when the party owes, for customers and suppliers alike. The old
software shows payables negative. The two say the same thing and the crosswalk records the difference, so nobody
reconciles one against the other and finds a fault that is not there.
"""
from datetime import date
from decimal import Decimal

from app.models import HEAD_OFFICE_BOOK  # noqa: F401  (imported from here by the routes)
from app.services.accounts_chart_service import money
from app.services.accounts_reports_service import _d, _query, _s, chart_map

ZERO = Decimal("0")

# The money a cash flow can follow. `wallet` is a mobile money account (Easypaisa, JazzCash); it behaves as a bank
# and their software has no separate word for it, so it rides with bank rather than being dropped.
MONEY_KINDS = {"cash": ("cash",), "bank": ("bank", "wallet"), "both": ("cash", "bank", "wallet")}

# Their Voucher Detail has ten tickboxes. Ours has four money voucher types, and the "against" half of their name is
# not a type at all: it is who the other side of the voucher was. So each of their ten is one of our types plus, for
# six of them, the kind of account facing it.
#
# `service` matches nothing today because there is no services invoice module here. The two options are kept so the
# screen offers exactly what theirs offers and so they start working the day such a module exists, rather than being
# quietly absent and noticed a year later.
VOUCHER_KINDS: dict[str, tuple[str, str | None]] = {
    "cash-received": ("CRV", None),
    "cash-payment": ("CPV", None),
    "bank-received": ("BRV", None),
    "bank-payment": ("BPV", None),
    "cash-received-against-sale": ("CRV", "customer"),
    "cash-received-against-service": ("CRV", "service"),
    "cash-payment-against-purchase": ("CPV", "supplier"),
    "bank-received-against-sale": ("BRV", "customer"),
    "bank-received-against-service": ("BRV", "service"),
    "bank-payment-against-purchase": ("BPV", "supplier"),
}
VOUCHER_KIND_LABELS = {
    "cash-received": "Cash Received", "cash-payment": "Cash Payment",
    "bank-received": "Bank Received", "bank-payment": "Bank Payment",
    "cash-received-against-sale": "Cash Received Against Sale",
    "cash-received-against-service": "Cash Received Against Service",
    "cash-payment-against-purchase": "Cash Payment Against Purchase",
    "bank-received-against-sale": "Bank Received Against Sale",
    "bank-received-against-service": "Bank Received Against Service",
    "bank-payment-against-purchase": "Bank Payment Against Purchase",
}

PIVOTS = ("account", "group", "category", "type")
OPERATORS = ("=", "<>", ">", "<", ">=", "<=")
# How old an open item is, in the buckets a shop actually talks in.
BUCKETS = (("current", 0, 30), ("d31_60", 31, 60), ("d61_90", 61, 90), ("d91_180", 91, 180), ("over180", 181, 10**6))


class RegisterError(Exception):
    def __init__(self, message: str):
        self.message = message


def _passes(value: Decimal, operator: str | None, target: Decimal | None) -> bool:
    """Their balance filter: an operator and a number, on the closing balance. No filter means every row."""
    if operator is None or target is None:
        return True
    if operator not in OPERATORS:
        raise RegisterError(f"'{operator}' is not one of {', '.join(OPERATORS)}.")
    return {
        "=": value == target, "<>": value != target, ">": value > target,
        "<": value < target, ">=": value >= target, "<=": value <= target,
    }[operator]


def _chunks(values: list, size: int = 400):
    for i in range(0, len(values), size):
        yield values[i:i + size]


async def _voucher_lines(books: list[str], start: date | None, end: date | None,
                         touching: list[str] | None = None, vtypes: tuple[str, ...] | None = None) -> list[dict]:
    """Every posted line of every voucher in the window, optionally only vouchers that touch certain accounts.

    The whole voucher comes back, not only the line that matched, because most of these reports need the *other*
    side: a cash flow is meaningless without knowing who the cash came from, and that is another line of the same
    voucher.
    """
    if not books:
        return []
    where = ["v.status = 'posted'", f"v.book IN ({','.join('?' for _ in books)})"]
    params: list = list(books)
    if start:
        where.append("v.date >= ?")
        params.append(start.isoformat())
    if end:
        where.append("v.date <= ?")
        params.append(end.isoformat())
    if vtypes:
        where.append(f"v.vtype IN ({','.join('?' for _ in vtypes)})")
        params.extend(vtypes)
    sql = (
        "SELECT v.id AS voucher_id, v.book AS book, v.number AS number, v.vtype AS vtype, v.date AS day, "
        "v.reference_no AS reference_no, v.description AS description, v.cheque_no AS cheque_no, "
        "v.header_account_id AS header_id, v.created_by_name AS created_by, v.source AS source, "
        "l.account_id AS account_id, l.debit AS debit, l.credit AS credit, l.description AS line_note "
        "FROM acc_vouchers v JOIN acc_voucher_lines l ON l.voucher_id = v.id "
        f"WHERE {' AND '.join(where)}"
    )
    if touching is None:
        return await _query(sql + " ORDER BY v.date, v.number, l.line_no", params)
    rows: list[dict] = []
    seen: set[str] = set()
    for chunk in _chunks(touching):
        marks = ",".join("?" for _ in chunk)
        for row in await _query(
            f"{sql} AND v.id IN (SELECT voucher_id FROM acc_voucher_lines WHERE account_id IN ({marks})) "
            "ORDER BY v.date, v.number, l.line_no", [*params, *chunk],
        ):
            key = f"{row['voucher_id']}:{row['account_id']}:{row['debit']}:{row['credit']}:{row.get('line_note')}"
            if key in seen:
                continue  # a voucher touching two accounts of the chunked set comes back once per chunk
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda r: (str(r["day"]), str(r["number"])))
    return rows


def _by_voucher(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["voucher_id"]), []).append(row)
    return out


def _net(row: dict) -> Decimal:
    return money(_d(row["debit"]) - _d(row["credit"]))


# ── cash flow ────────────────────────────────────────────────────────────────────────────────────

async def cash_flow(books: list[str], start: date, end: date, mode: str = "both",
                    account_ids: list[str] | None = None) -> dict:
    """Money in and out of each cash or bank account, by who it came from and who it went to.

    Their report puts one money account at the top and lists the other party on each line, which is the question a
    shopkeeper actually asks of a bank account: not "what is the balance" but "who paid this in".

    A voucher with several counterparties splits across them in proportion to their own amounts, so the lines always
    add back to the account's own movement. Anything that cannot be attributed that way (a voucher with no opposite
    side, which only a broken voucher has) lands under one clearly named row rather than being dropped, because a
    cash flow that silently loses money is worse than one that admits to a remainder.
    """
    if mode not in MONEY_KINDS:
        raise RegisterError(f"A cash flow is over cash, bank or both, not '{mode}'.")
    chart = await chart_map(books)
    kinds = MONEY_KINDS[mode]
    picked = {aid for aid, m in chart.items() if m["kind"] in kinds}
    if account_ids:
        picked &= set(account_ids)
    if not picked:
        return {"start": start.isoformat(), "end": end.isoformat(), "mode": mode, "accounts": [],
                "totals": {k: _s(ZERO) for k in ("inFlow", "outFlow", "net")}}

    ids = sorted(picked)
    opening: dict[str, Decimal] = {aid: ZERO for aid in ids}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in await _query(
            "SELECT l.account_id AS account_id, SUM(l.debit) AS dr, SUM(l.credit) AS cr "
            "FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id "
            f"WHERE v.status = 'posted' AND v.book IN ({','.join('?' for _ in books)}) AND v.date < ? "
            f"AND l.account_id IN ({marks}) GROUP BY l.account_id",
            [*books, start.isoformat(), *chunk],
        ):
            opening[str(row["account_id"])] = money(_d(row["dr"]) - _d(row["cr"]))

    flows: dict[str, dict[str, list[Decimal]]] = {aid: {} for aid in ids}
    for lines in _by_voucher(await _voucher_lines(books, start, end, touching=ids)).values():
        others = [l for l in lines if str(l["account_id"]) not in picked]
        for line in lines:
            aid = str(line["account_id"])
            if aid not in picked:
                continue
            movement = _net(line)
            if movement == ZERO:
                continue
            # The counterparties are the lines facing the other way. A contra between two money accounts counts the
            # other money account as the counterparty, which is exactly how it should read.
            facing = [l for l in (others or [l for l in lines if l is not line])
                      if (_net(l) < ZERO) == (movement > ZERO) and _net(l) != ZERO]
            total = sum((abs(_net(l)) for l in facing), ZERO)
            if total == ZERO:
                bucket = flows[aid].setdefault("", [ZERO, ZERO])
                bucket[0 if movement > ZERO else 1] += abs(movement)
                continue
            left = abs(movement)
            for i, other in enumerate(facing):
                share = left if i == len(facing) - 1 else money(abs(movement) * abs(_net(other)) / total)
                left -= share
                bucket = flows[aid].setdefault(str(other["account_id"]), [ZERO, ZERO])
                bucket[0 if movement > ZERO else 1] += share

    accounts = []
    grand_in = grand_out = ZERO
    for aid in ids:
        meta = chart[aid]
        rows = []
        running = opening[aid]
        total_in = total_out = ZERO
        for other_id, (came_in, went_out) in sorted(
            flows[aid].items(), key=lambda kv: chart.get(kv[0], {}).get("name", "Unattributed")
        ):
            if came_in == ZERO and went_out == ZERO:
                continue
            running += came_in - went_out
            total_in += came_in
            total_out += went_out
            rows.append({
                "accountId": other_id or None,
                "particular": chart[other_id]["name"] if other_id in chart else "Unattributed",
                "inFlow": _s(came_in), "outFlow": _s(went_out), "balance": _s(running),
            })
        if not rows and opening[aid] == ZERO:
            continue
        accounts.append({
            "accountId": aid, "book": meta["book"], "code": meta["code"], "name": meta["name"], "kind": meta["kind"],
            "opening": _s(opening[aid]), "rows": rows,
            "totalIn": _s(total_in), "totalOut": _s(total_out), "closing": _s(opening[aid] + total_in - total_out),
        })
        grand_in += total_in
        grand_out += total_out
    return {
        "start": start.isoformat(), "end": end.isoformat(), "mode": mode, "accounts": accounts,
        "totals": {"inFlow": _s(grand_in), "outFlow": _s(grand_out), "net": _s(grand_in - grand_out)},
    }


# ── account summary ──────────────────────────────────────────────────────────────────────────────

async def account_summary(books: list[str], start: date | None, end: date, pivot: str = "account",
                          balance_op: str | None = None, balance_value: Decimal | None = None,
                          account_ids: list[str] | None = None, include_zero: bool = False) -> dict:
    """Opening, movement and closing, rolled up to whichever level was asked for.

    Their dialog offers Account, AccCategory, AccGroup and AccType as four radio buttons over the same figures, plus
    a balance operator, which is how somebody finds "every account still carrying a balance" without reading 900
    lines. It is the trial balance pivoted, so it is computed the same way and will always agree with it.
    """
    if pivot not in PIVOTS:
        raise RegisterError(f"A summary is by {', '.join(PIVOTS)}, not '{pivot}'.")
    chart = await chart_map(books)
    wanted = set(account_ids) if account_ids else None

    def key_of(meta: dict) -> tuple[str, str]:
        return {
            "account": (meta["accountId"], f"{meta['code']} {meta['name']}"),
            "group": (meta["groupCode"], f"{meta['groupCode']} {meta['groupName']}"),
            "category": (meta["categoryCode"], f"{meta['categoryCode']} {meta['categoryName']}"),
            "type": (meta["typeCode"], f"{meta['typeCode']} {meta['typeName']}"),
        }[pivot]

    from app.services.accounts_reports_service import _sums

    ids = [aid for aid in chart if wanted is None or aid in wanted]
    before = await _sums(books, before=start, account_ids=ids) if start else {}
    during = await _sums(books, start=start, end=end, account_ids=ids)
    opening_only = await _sums(books, end=end, account_ids=ids) if not start else {}

    buckets: dict[str, dict] = {}
    for aid in ids:
        meta = chart[aid]
        key, label = key_of(meta)
        row = buckets.setdefault(key, {
            "key": key, "label": label, "nature": meta["nature"], "statement": meta["statement"],
            "opening": ZERO, "debit": ZERO, "credit": ZERO, "accounts": 0,
        })
        o_dr, o_cr = before.get(aid, (ZERO, ZERO))
        d_dr, d_cr = (during if start else opening_only).get(aid, (ZERO, ZERO))
        row["opening"] += o_dr - o_cr
        row["debit"] += d_dr
        row["credit"] += d_cr
        row["accounts"] += 1

    rows = []
    totals = {"opening": ZERO, "debit": ZERO, "credit": ZERO, "closing": ZERO}
    for row in sorted(buckets.values(), key=lambda r: r["label"]):
        closing = row["opening"] + row["debit"] - row["credit"]
        if not include_zero and closing == ZERO and row["debit"] == ZERO and row["credit"] == ZERO:
            continue
        if not _passes(closing, balance_op, balance_value):
            continue
        rows.append({
            "key": row["key"], "label": row["label"], "accounts": row["accounts"], "nature": row["nature"],
            "opening": _s(row["opening"]), "debit": _s(row["debit"]), "credit": _s(row["credit"]),
            "closing": _s(closing), "closingSide": "Dr" if closing >= ZERO else "Cr",
        })
        for field, value in (("opening", row["opening"]), ("debit", row["debit"]), ("credit", row["credit"]), ("closing", closing)):
            totals[field] += value
    return {
        "start": start.isoformat() if start else None, "end": end.isoformat(), "pivot": pivot,
        "rows": rows, "totals": {k: _s(v) for k, v in totals.items()},
    }


# ── voucher detail ───────────────────────────────────────────────────────────────────────────────

async def voucher_detail(books: list[str], start: date, end: date, kinds: list[str] | None = None,
                         user: str | None = None, limit: int = 500, offset: int = 0) -> dict:
    """Their ten voucher kinds, which are our four money types split by who faced them.

    One row per voucher, not per line: their report reads as a list of payments and receipts, and a voucher with
    three expense lines is still one payment. The counterparty named is the largest line on the other side, which is
    what somebody means when they say who a payment was to.
    """
    # Name the wrong one before complaining that nothing is ticked: a typo left `picked` empty, and "tick at least
    # one" would send somebody back to a screen where they had ticked one.
    unknown = sorted(set(kinds or []) - set(VOUCHER_KINDS))
    if unknown:
        raise RegisterError(f"There is no voucher kind called '{unknown[0]}'. They are: {', '.join(VOUCHER_KINDS)}.")
    picked = [k for k in (kinds or list(VOUCHER_KINDS)) if k in VOUCHER_KINDS]
    if not picked:
        raise RegisterError("Tick at least one kind of voucher.")
    vtypes = tuple({VOUCHER_KINDS[k][0] for k in picked})
    chart = await chart_map(books)
    rows_out = []
    for lines in _by_voucher(await _voucher_lines(books, start, end, vtypes=vtypes)).values():
        head = lines[0]
        if user and (head.get("created_by") or "") != user:
            continue
        header_id = str(head.get("header_id") or "")
        # The other side: every line that is not the cash or bank account the voucher is headed by.
        facing = [l for l in lines if str(l["account_id"]) != header_id] or lines
        biggest = max(facing, key=lambda l: abs(_net(l)))
        against = chart.get(str(biggest["account_id"]), {}).get("kind", "general")
        matched = [
            k for k in picked
            if VOUCHER_KINDS[k][0] == head["vtype"] and (VOUCHER_KINDS[k][1] is None or VOUCHER_KINDS[k][1] == against)
        ]
        if not matched:
            continue
        amount = sum((abs(_net(l)) for l in lines), ZERO) / 2  # a voucher balances; half its absolute movement is its value
        rows_out.append({
            "voucherId": str(head["voucher_id"]), "book": head["book"], "date": str(head["day"])[:10],
            "type": head["vtype"], "kind": matched[0], "kindLabel": VOUCHER_KIND_LABELS[matched[0]],
            "number": head["number"],
            "fromAccount": chart.get(header_id, {}).get("name") or chart.get(str(head["account_id"]), {}).get("name"),
            "chequeNo": head.get("cheque_no"),
            "partyName": chart.get(str(biggest["account_id"]), {}).get("name"),
            "partyKind": against,
            "reference": head.get("reference_no"), "description": head.get("description"),
            "user": head.get("created_by"), "amount": _s(money(amount)),
        })
    rows_out.sort(key=lambda r: (r["date"], r["number"]))
    total = sum((Decimal(r["amount"]) for r in rows_out), ZERO)
    return {
        "start": start.isoformat(), "end": end.isoformat(), "kinds": picked, "count": len(rows_out),
        "rows": rows_out[offset:offset + limit], "total": _s(total),
    }


# ── date wise payment and receipt ────────────────────────────────────────────────────────────────

async def date_wise_money(books: list[str], start: date, end: date, direction: str,
                          account_ids: list[str] | None = None, area: str | None = None,
                          sub_area: str | None = None, category: str | None = None) -> dict:
    """What went out to suppliers, or came in from customers, day by day, with cash kept apart from cheque.

    Their two reports (Date Wise Payment, DateWise Receipt) are one question asked in two directions, so they are
    one function here. A bank movement with no cheque number is its own column rather than being counted as a
    cheque: a transfer and a cheque are not the same thing to anyone chasing one.
    """
    if direction not in ("payment", "receipt"):
        raise RegisterError("A date wise register is of payments or of receipts.")
    kind = "supplier" if direction == "payment" else "customer"
    vtypes = ("CPV", "BPV") if direction == "payment" else ("CRV", "BRV")
    chart = await chart_map(books)
    parties = {
        aid: m for aid, m in chart.items()
        if m["kind"] == kind and (not account_ids or aid in set(account_ids))
    }
    if not parties:
        return {"start": start.isoformat(), "end": end.isoformat(), "direction": direction, "rows": [],
                "totals": {k: _s(ZERO) for k in ("cash", "cheque", "bank", "amount")}}
    facts = await _party_facts(list(parties))
    if area or sub_area or category:
        parties = {
            aid: m for aid, m in parties.items()
            if (not area or (facts.get(aid, {}).get("area") or "") == area)
            and (not sub_area or (facts.get(aid, {}).get("subArea") or "") == sub_area)
            and (not category or (facts.get(aid, {}).get("category") or "") == category)
        }

    rows = []
    cash_total = cheque_total = bank_total = ZERO
    for lines in _by_voucher(await _voucher_lines(books, start, end, touching=list(parties), vtypes=vtypes)).values():
        head = lines[0]
        for line in lines:
            aid = str(line["account_id"])
            if aid not in parties:
                continue
            amount = abs(_net(line))
            if amount == ZERO:
                continue
            cheque_no = head.get("cheque_no")
            column = "cash" if head["vtype"] in ("CPV", "CRV") else ("cheque" if cheque_no else "bank")
            rows.append({
                "book": head["book"], "date": str(head["day"])[:10], "number": head["number"],
                "accountId": aid, "code": chart[aid]["code"], "name": chart[aid]["name"],
                "partyCode": facts.get(aid, {}).get("code"),
                "area": facts.get(aid, {}).get("area"), "subArea": facts.get(aid, {}).get("subArea"),
                "category": facts.get(aid, {}).get("category"),
                "chequeNo": cheque_no, "reference": head.get("reference_no"), "user": head.get("created_by"),
                "cash": _s(amount if column == "cash" else ZERO),
                "cheque": _s(amount if column == "cheque" else ZERO),
                "bank": _s(amount if column == "bank" else ZERO),
                "amount": _s(amount),
            })
            cash_total += amount if column == "cash" else ZERO
            cheque_total += amount if column == "cheque" else ZERO
            bank_total += amount if column == "bank" else ZERO
    rows.sort(key=lambda r: (r["date"], r["name"], r["number"]))
    return {
        "start": start.isoformat(), "end": end.isoformat(), "direction": direction, "rows": rows,
        "totals": {"cash": _s(cash_total), "cheque": _s(cheque_total), "bank": _s(bank_total),
                   "amount": _s(cash_total + cheque_total + bank_total)},
    }


# ── who owes what ────────────────────────────────────────────────────────────────────────────────

async def _party_facts(account_ids: list[str]) -> dict[str, dict]:
    """The customer's or supplier's own details, off the account they ride on (models/accounts.py)."""
    from app.models import Account

    out: dict[str, dict] = {}
    for chunk in _chunks(account_ids):
        for a in await Account.filter(id__in=chunk).values(
            "id", "party_code", "party_phone", "party_address", "party_contact", "party_city",
            "party_area", "party_sub_area", "party_category", "party_due_days", "party_credit_limit",
        ):
            out[str(a["id"])] = {
                "code": a["party_code"], "phone": a["party_phone"], "address": a["party_address"],
                "contact": a["party_contact"], "city": a["party_city"], "area": a["party_area"],
                "subArea": a["party_sub_area"], "category": a["party_category"],
                "dueDays": a["party_due_days"] or 0,
                "creditLimit": a["party_credit_limit"],
            }
    return out


async def _post_dated(books: list[str], account_ids: list[str], as_of: date) -> dict[str, Decimal]:
    """Post dated cheques in hand against each party: written, not yet cleared, and dated after the day asked about.

    This is the column their receivable and payable summaries carry beside the balance, and the reason they carry it
    is that a balance covered by a cheque already in the drawer is not the same debt as one that is not.
    """
    from app.models import Cheque

    out: dict[str, Decimal] = {}
    for chunk in _chunks(account_ids):
        for row in await Cheque.filter(
            book__in=books, party_account_id__in=chunk, status="pending", cheque_date__gt=as_of,
        ).values("party_account_id", "amount"):
            key = str(row["party_account_id"])
            out[key] = out.get(key, ZERO) + money(_d(row["amount"]))
    return out


async def party_summary(books: list[str], kind: str, as_of: date, group_by: str = "none",
                        balance_op: str | None = None, balance_value: Decimal | None = None,
                        area: str | None = None, sub_area: str | None = None,
                        account_ids: list[str] | None = None) -> dict:
    """What each customer owes, or what is owed to each supplier, less the post dated cheques already in hand.

    Their Customer Receivable Summary and Supplier Payable Summary are the same report pointed two ways, with the
    same three money columns: Balance, PD Cheques, Net Balance. Grouping is theirs too: by party category, by area,
    or by area and sub-area, which is how a recovery round is actually organised.

    A balance here is positive when the party owes, for both kinds. Theirs shows payables negative. Same fact,
    opposite sign, and the crosswalk says so.
    """
    if kind not in ("customer", "supplier"):
        raise RegisterError("A party summary is of customers or of suppliers.")
    if group_by not in ("none", "category", "area", "area-sub-area"):
        raise RegisterError("Group by nothing, category, area, or area and sub-area.")
    from app.services.accounts_reports_service import _sums

    chart = await chart_map(books)
    picked = [aid for aid, m in chart.items() if m["kind"] == kind and (not account_ids or aid in set(account_ids))]
    if not picked:
        return {"asOf": as_of.isoformat(), "kind": kind, "groupBy": group_by, "groups": [], "count": 0,
                "totals": {k: _s(ZERO) for k in ("balance", "pdCheques", "netBalance")}}
    facts = await _party_facts(picked)
    if area or sub_area:
        picked = [
            aid for aid in picked
            if (not area or (facts.get(aid, {}).get("area") or "") == area)
            and (not sub_area or (facts.get(aid, {}).get("subArea") or "") == sub_area)
        ]
    sums = await _sums(books, end=as_of, account_ids=picked)
    pdc = await _post_dated(books, picked, as_of)

    grouped: dict[str, list[dict]] = {}
    totals = {"balance": ZERO, "pdCheques": ZERO, "netBalance": ZERO}
    for aid in picked:
        meta, fact = chart[aid], facts.get(aid, {})
        dr, cr = sums.get(aid, (ZERO, ZERO))
        # A customer owes when their account is in debit; a supplier is owed when theirs is in credit. Both come out
        # positive, so the two halves of this report read the same way round.
        balance = money(dr - cr) if kind == "customer" else money(cr - dr)
        cheques = pdc.get(aid, ZERO)
        net = balance - cheques
        if balance == ZERO and cheques == ZERO:
            continue
        if not _passes(balance, balance_op, balance_value):
            continue
        label = {
            "none": "All",
            "category": fact.get("category") or "No category",
            "area": fact.get("area") or "No area",
            "area-sub-area": f"{fact.get('area') or 'No area'} · {fact.get('subArea') or 'No sub-area'}",
        }[group_by]
        grouped.setdefault(label, []).append({
            "accountId": aid, "book": meta["book"], "code": meta["code"], "name": meta["name"],
            "partyCode": fact.get("code"), "phone": fact.get("phone"), "address": fact.get("address"),
            "contact": fact.get("contact"), "city": fact.get("city"),
            "area": fact.get("area"), "subArea": fact.get("subArea"), "category": fact.get("category"),
            "dueDays": fact.get("dueDays") or 0,
            "creditLimit": _s(_d(fact.get("creditLimit"))) if fact.get("creditLimit") is not None else None,
            "balance": _s(balance), "pdCheques": _s(cheques), "netBalance": _s(net),
        })
        totals["balance"] += balance
        totals["pdCheques"] += cheques
        totals["netBalance"] += net

    groups = []
    for label in sorted(grouped):
        rows = sorted(grouped[label], key=lambda r: r["name"])
        groups.append({
            "label": label, "rows": rows,
            "totals": {field: _s(sum((Decimal(r[field]) for r in rows), ZERO))
                       for field in ("balance", "pdCheques", "netBalance")},
        })
    return {
        "asOf": as_of.isoformat(), "kind": kind, "groupBy": group_by, "groups": groups,
        "count": sum(len(g["rows"]) for g in groups), "totals": {k: _s(v) for k, v in totals.items()},
    }


# ── invoice by invoice ───────────────────────────────────────────────────────────────────────────

def _walk(entries: list[dict], kind: str) -> list[dict]:
    """One party's vouchers in date order, turned into open items with payments applied oldest first.

    Oldest first because that is how a shop and its supplier both read a payment unless somebody says otherwise,
    and because any other rule has to be explained to the person holding the invoice.
    """
    owed_sign = 1 if kind == "customer" else -1
    items: list[dict] = []
    credit_left = ZERO
    for entry in entries:
        movement = entry["movement"] * owed_sign
        if movement > ZERO:
            items.append({**entry, "amount": movement, "paid": ZERO})
        else:
            credit_left += -movement
        while credit_left > ZERO:
            open_item = next((i for i in items if i["amount"] - i["paid"] > ZERO), None)
            if open_item is None:
                break
            take = min(open_item["amount"] - open_item["paid"], credit_left)
            open_item["paid"] += take
            credit_left -= take
    for item in items:
        item["outstanding"] = item["amount"] - item["paid"]
    if credit_left > ZERO and items:
        items[-1]["advance"] = credit_left
    return items


async def _party_entries(books: list[str], account_ids: list[str], as_of: date) -> dict[str, list[dict]]:
    entries: dict[str, list[dict]] = {}
    for line in await _voucher_lines(books, None, as_of, touching=account_ids):
        aid = str(line["account_id"])
        if aid not in set(account_ids):
            continue
        movement = _net(line)
        if movement == ZERO:
            continue
        entries.setdefault(aid, []).append({
            "voucherId": str(line["voucher_id"]), "book": line["book"], "number": line["number"],
            "vtype": line["vtype"], "date": str(line["day"])[:10], "movement": movement,
            "reference": line.get("reference_no"), "description": line.get("description"),
            "source": line.get("source"),
        })
    for rows in entries.values():
        rows.sort(key=lambda r: (r["date"], r["number"]))
    return entries


async def supplier_balance_detail(books: list[str], as_of: date, account_ids: list[str] | None = None,
                                  kind: str = "supplier", only_open: bool = False) -> dict:
    """Every invoice, what has been paid against it, and how many days it has been standing.

    Their Supplier Balance Detail is the report an accountant argues from: not "this supplier is owed 47,017" but
    "these four bills make up 47,017 and this one is 761 days old". The ageing is against that supplier's own credit
    days, which ride on the account (models/accounts.py), not against one number for everybody.
    """
    if kind not in ("customer", "supplier"):
        raise RegisterError("Invoice detail is of customers or of suppliers.")
    chart = await chart_map(books)
    picked = [aid for aid, m in chart.items() if m["kind"] == kind and (not account_ids or aid in set(account_ids))]
    if not picked:
        return {"asOf": as_of.isoformat(), "kind": kind, "parties": [], "count": 0,
                "totals": {k: _s(ZERO) for k in ("invoiced", "paid", "outstanding")}}
    facts = await _party_facts(picked)
    entries = await _party_entries(books, picked, as_of)

    parties = []
    grand = {"invoiced": ZERO, "paid": ZERO, "outstanding": ZERO}
    for aid in picked:
        rows = entries.get(aid, [])
        if not rows:
            continue
        items = _walk(rows, kind)
        fact = facts.get(aid, {})
        due_days = fact.get("dueDays") or 0
        running = ZERO
        listed = []
        for item in items:
            previous = running
            running += item["outstanding"]
            if only_open and item["outstanding"] == ZERO:
                continue
            age = (as_of - date.fromisoformat(item["date"])).days
            listed.append({
                "voucherId": item["voucherId"], "book": item["book"], "number": item["number"],
                "date": item["date"], "type": item["vtype"],
                "invoiceNo": item.get("reference") or item["number"],
                "description": item.get("description"),
                "ageDays": age, "overdue": age > due_days,
                "previousBalance": _s(previous), "invoiced": _s(item["amount"]),
                "paid": _s(item["paid"]), "outstanding": _s(item["outstanding"]), "balance": _s(running),
            })
        if not listed:
            continue
        invoiced = sum((i["amount"] for i in items), ZERO)
        paid = sum((i["paid"] for i in items), ZERO)
        parties.append({
            "accountId": aid, "book": chart[aid]["book"], "code": chart[aid]["code"], "name": chart[aid]["name"],
            "partyCode": fact.get("code"), "creditDays": due_days,
            "area": fact.get("area"), "subArea": fact.get("subArea"), "category": fact.get("category"),
            "rows": listed,
            "totals": {"invoiced": _s(invoiced), "paid": _s(paid), "outstanding": _s(invoiced - paid)},
        })
        grand["invoiced"] += invoiced
        grand["paid"] += paid
        grand["outstanding"] += invoiced - paid
    parties.sort(key=lambda p: p["name"])
    return {
        "asOf": as_of.isoformat(), "kind": kind, "parties": parties,
        "count": sum(len(p["rows"]) for p in parties), "totals": {k: _s(v) for k, v in grand.items()},
    }


async def daily_operation(books: list[str], start: date, end: date,
                          account_ids: list[str] | None = None) -> dict:
    """The deliveries taken in a window, what has been paid against each, and what is still owed.

    Their Daily Operation Report, which despite the name is a purchase ledger: one line per delivery, grouped by
    supplier, with a total under each. It is the invoice detail above, cut to a window, and it is computed from the
    same walk so the two can never disagree.
    """
    whole = await supplier_balance_detail(books, end, account_ids=account_ids, kind="supplier")
    lo, hi = start.isoformat(), end.isoformat()
    suppliers = []
    grand = {"invoiced": ZERO, "paid": ZERO, "outstanding": ZERO}
    for party in whole["parties"]:
        rows = [r for r in party["rows"] if lo <= r["date"] <= hi]
        if not rows:
            continue
        totals = {field: sum((Decimal(r[field]) for r in rows), ZERO) for field in ("invoiced", "paid", "outstanding")}
        suppliers.append({**party, "rows": rows, "totals": {k: _s(v) for k, v in totals.items()}})
        for field, value in totals.items():
            grand[field] += value
    return {
        "start": start.isoformat(), "end": end.isoformat(), "suppliers": suppliers,
        "count": sum(len(s["rows"]) for s in suppliers), "totals": {k: _s(v) for k, v in grand.items()},
    }
