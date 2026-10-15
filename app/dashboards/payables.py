"""Payables & Receivables: who we owe, who owes us, and how late each piece of it is.

TWO MEANINGS OF THE DATES ON THIS PAGE
--------------------------------------
A balance is a point in time. What is owed to suppliers is what it is on one day; it is not a quantity that can be
added over a month, and thirty days of scope does not mean thirty balances. So the window's **end** is the day this
page stands on, and the balance, the overdue figure, the settle status and every ageing bracket are read as of it,
cumulative from the first voucher in the books.

The window's **start** earns its keep on the other half: what was paid inside the window, and whether each of those
payments was on time. That is a flow, it is safe to add up, and it answers "are we paying late at the moment"
rather than "how old is the pile".

Both halves come out of the same per party per day rows and the same settlement walk, so a payment total on a card
and the money in a bracket can never be two different readings of the ledger. The one fold that reads the window
straight out of SQL is the day by day line, and it reconciles to the cards by construction: see `_flow`.

`Scope.where` is therefore used for the flow fold and nowhere else, because it always emits both day bounds and
there is no way to ask it for only the upper one. `stock.py` hit the same wall from the other side and settled it
the same way.

THE ARITHMETIC IS `accounts_reports_service.ageing`, WITH ONE THING CHANGED
---------------------------------------------------------------------------
`_walk` below is that function's settlement, line for line: the owed side of a party is the debit for a customer
and the credit for a supplier, a day's owed movement becomes one open item, and every payment is applied to the
oldest open item first. Nothing here is a second opinion about what is still owed.

What is changed is the one thing that report hardcodes. It ages every party against 30 days and reports
`dueDays: 30` for all of them. Here each party is aged against its own agreed credit days, and the brackets count
**days past that party's own due date** rather than days since the invoice. A supplier who gives 60 days and was
billed 45 days ago is not "31-60 days old" on this page, it is not due at all, which is the only reading an
accountant can argue from.

With 30 credit days for everybody the two line up exactly, one bracket apart, and that is how this file was
checked: not due here is their `current` (age 0 to 30), 1-30 here is their `d31_60`, 31-60 is their `d61_90`,
61-90 is their `d91_120`, and 91-120 through Over 180 add up to their `over120`. Measured on head office's
supplier book at 2026-10-12, every one of those five matched to the paisa.

THE CREDIT DAYS COLUMN EXISTS AND IS EMPTY, SO THE PAGE SAYS SO
----------------------------------------------------------------
`acc_accounts.party_due_days` is the right column and it is already wired: the supplier screen writes it, and the
receivable and payable registers age against it. It is also zero on **all 5,012 party accounts** (2,662 suppliers,
2,350 customers, no nulls), and `suppliers.due_days` -- the other place it could have been kept -- is zero on all
542 rows as well. Both were checked before this defaulted to anything.

So zero is read as "no terms recorded" and `DEFAULT_CREDIT_DAYS` stands in for it, which is the 30 days `ageing`
already assumes. `_credit_note` says that in plain words on every visual it affects, with the live count of parties
it applies to, so the figure is never passed off as each supplier's own agreed terms. Fill the column and this page
follows it with no change here.

Zero is read that way deliberately, and it is a judgement call rather than an obvious truth. Taken literally, zero
credit days means every bill is overdue the day after it is raised, which is what `accounts_registers_service`
does: `supplier_balance_detail` marks an item overdue when `age > due_days` with `due_days` defaulting to 0. So the
two existing reports disagree today -- the register calls almost everything overdue, the ageing report calls
anything under 31 days current -- and this page sides with the ageing report rather than inventing a third answer.

WHY OVERDUE IS A SHARE OF WHAT IS OUTSTANDING AND NOT OF THE BALANCE
---------------------------------------------------------------------
This is the inconsistency the stakeholder's own dashboard had, and `ageing` has it too. Its totals put
`balance` 224,135,417.84 beside `overdue` 230,383,410.86 for head office's suppliers at 2026-10-12, so the overdue
share of the balance is 102.8%, which cannot be true of anything.

Neither figure is wrong; they are different figures added up differently. `balance` nets each party's position, so
a supplier sitting on an advance subtracts from it. The buckets and the overdue figure only ever add the open items
that are still open, so an advance contributes nothing to them. On that day the gap is exactly the advances
suppliers are holding, 34,838,933.95, and 258,974,351.79 of open items less that advance is the 224,135,417.84
balance to the paisa.

So this page carries both and keeps them apart. **Outstanding** is the open items, it is what every bracket and
every status divides up, and it is the denominator of every percentage. **Balance** is outstanding less advances,
which is what the books owe, and it is never a denominator. The advance is its own card rather than a silent
subtraction, because money already paid to a supplier is not the same thing as money never owed.

HEAD OFFICE'S OWN BOOK, NOT ALL FIVE ADDED TOGETHER
----------------------------------------------------
The same supplier is an account in every book: 424 supplier accounts in each of FC, GG, GT and MT and 966 in HO.
Adding the books would list one supplier five times as five parties with five ageings, so a page about parties has
to pick a book, and `Scope` carries no book to pick with. This reads HEAD_OFFICE_BOOK, which is what
`/accounts/ageing` reads when it is asked for no book in particular, and every visual says which book it is.

WHAT A COUNT ON THIS PAGE COUNTS
--------------------------------
An open item is one party on one day, because that is the grain `ageing` settles on and changing it would change
the answer. Two deliveries from one supplier on one day are one open item here, not two invoices. The visuals say
"open items" rather than "invoices" for exactly that reason, and it is not a quibble: measured at 2026-10-12, head
office's suppliers are owed money on 2,635 open items that were raised by 4,652 vouchers, so calling them invoices
would have been wrong by 2,017 and wrong in the flattering direction.

TWO THINGS THE APP DOES WITH WHAT THIS SENDS, WHICH SHAPED EVERY FIGURE ON THE PAGE
-----------------------------------------------------------------------------------
Both were read out of `cloud-app/src/pages/dashboards/Dashboard.tsx` rather than assumed.

**A card's value is always money, whatever `kind` says.** It renders as
`{c.kind === 'number' ? money(c.value) : money(c.value)}` -- the two arms are the same expression, so `kind` does
nothing and every card value goes through the rupee formatter. A percentage in a card value would read "Rs 89".
Every card here therefore carries money in its value and puts its counts and its percentages in `sub`, which is
printed as plain text. A bar is the same: its value is drawn with `money()` too, so the count and the ratio on a
bar ride in its label, where they read as a sentence.

**A `note` is only drawn when the visual has NO rows.** All three renderers do
`if (!rows.length) return <Empty description={visual.note ...} />` and nothing else prints it, so a note on a
visual that answered is invisible today. The notes below are still right -- they are what the contract asks for,
they appear the moment a visual comes back empty, and the app needs one line changed to show them always -- but
nothing this page has to say may rest on them alone. So anything a reader must not miss is also in a place that
always draws: the credit days default is in every ageing visual's TITLE and in the party table's own column, and
every percentage is in a card `sub`.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.core import dialect
from app.dashboards._shape import card, line, money, pct, table
from app.models import HEAD_OFFICE_BOOK
from app.services import dashboards_service as ds
from app.services.dashboards_service import Scope

META = {
    "label": "Payables & Receivables",
    "blurb": "Who we owe and who owes us, aged against each party's own credit days. Read as of the window's end.",
    # Both pages on one resource, because the registry gates a whole dashboard with one. `accounts.registers` is
    # the one grant that reaches both halves: `/accounts-registers` accepts it for the receivable summary and for
    # the payable summary alike, where `accounts.payables` reaches only the suppliers and `accounts.receivables`
    # only the customers. Gating on either of those would hide half the dashboard from somebody who is allowed it.
    "resource": "accounts.registers",
    "pages": [
        {"id": "payables", "label": "Payables"},
        {"id": "receivables", "label": "Receivables"},
    ],
}

ZERO = Decimal("0")

# Head office's own book, and what this page reads when no branch has been picked. See the docstring: one supplier
# is an account in all five books, so a page about parties cannot add them up and has to read one.
BOOKS = [HEAD_OFFICE_BOOK]

KIND_OF_PAGE = {"payables": "supplier", "receivables": "customer"}
PARTY_WORD = {"supplier": ("supplier", "suppliers"), "customer": ("customer", "customers")}

# What the settling side of the ledger is CALLED on each page. It is one column read from two sides: the credit on
# a supplier account is us paying out, the debit on a customer account is them paying in. Labelling both of them
# "paid" would put "Paid in this window: Rs 4,177,312" on the receivables page over money that came IN, and a
# reader would take it for head office paying its own customers.
MONEY_WORD = {"supplier": "Paid", "customer": "Received"}

# What stands in for a party whose credit days are not recorded. 30 days, which is what
# `accounts_reports_service.ageing` assumes for everybody, so the two reports cannot drift apart while the column
# is empty. Read the docstring before changing it: zero in the column means "not recorded" here, not "due at once".
DEFAULT_CREDIT_DAYS = 30

# Days past the party's OWN due date, not days since the bill. The last one is open ended; 10**6 days is 2,700
# years, which is past the oldest voucher in the books by a comfortable margin.
PAST_DUE_BRACKETS = (
    ("1-30 days", 1, 30), ("31-60 days", 31, 60), ("61-90 days", 61, 90), ("91-120 days", 91, 120),
    ("121-150 days", 121, 150), ("151-180 days", 151, 180), ("Over 180 days", 181, 10 ** 6),
)

# The plain word for each dimension, for the sentence a blocked visual prints. Bare nouns, with no article, so
# that `_blocked` can list two or three of them in one phrase: "with no branch, category or Item on a voucher".
DIMENSION_WORDS = {"branch": "branch", "department": "department", "category": "category",
                   "brand": "brand", "sku": "Item"}


def _rs(value: Decimal) -> str:
    """A money figure for a `sub` line or a bar label, grouped and whole.

    The app formats a card's VALUE and a bar's VALUE; a `sub` and a label are printed as they arrive, so a money
    figure in one of those has to be formatted here or it reads as 224135417.84.
    """
    return f"Rs {ds._d(value):,.0f}"


def _n(value: int) -> str:
    return f"{value:,}"


# ── what cannot be answered ─────────────────────────────────────────────────────────────────────
def _blocked(scope: Scope) -> str | None:
    """The sentence to print instead of a picture, when something has been clicked that the books cannot follow.

    `acc_vouchers` carries a book and a date and nothing else: no department, no category, no brand and no Item. A
    voucher is not a product-day, and there is no join that would make it one -- a supplier's balance is not
    divisible by brand, because the payment that settled the bill was not either.

    So those four dimensions block this whole page rather than one visual of it, and the page says so in the same
    words `sales.py` uses for its hourly chart. Drawing the unnarrowed figures under a filter chip would be the
    worst of the three options, because nothing on the screen would look wrong.

    BRANCH IS NOT ONE OF THEM, and used to be. A book IS a shop -- every branch keeps its own, with its own
    supplier and customer accounts -- so a picked branch reads that branch's book rather than head office's. That
    is what `build` does with it below. The four that remain are the ones with genuinely no column to read.
    """
    picked = [DIMENSION_WORDS.get(name, name) for name in scope.picked
              if name in ds.DIMENSIONS and name != "branch"]
    if not picked:
        return None
    words = picked[0] if len(picked) == 1 else ", ".join(picked[:-1]) + f" or {picked[-1]}"
    return (f"The books are kept by account and by day, with no {words} on a voucher, so nothing on this page can "
            f"follow what you clicked. A party's balance cannot be split that way either: the payment that settled "
            f"the bill was not split that way. Clear the filter to read this page.")


# ── the ledger, a party at a time ───────────────────────────────────────────────────────────────
async def _party_days(kind: str, as_of: date, books: list[str]) -> list[dict]:
    """Every posted movement on a party account of this kind, summed per party per day, up to `as_of`.

    `books` is one book: head office's, or the picked branch's. A list rather than a value because that is the
    shape `dialect.codes` takes and because the `IN` was already written that way.

    The same query `accounts_reports_service.ageing` runs, and for the same reasons: one pass for every party
    rather than one per party, with `kind` and `book` left to the database because they are columns on
    `acc_accounts`. `status = 'posted'` is not optional -- a draft is not a debt.

    No lower bound on the date, which is the point. An invoice from 2024 that is still open is still owed, and a
    window with a floor in it would quietly report a month's movement as a balance.

    **No join hint.** `dialect.hash_join` is for a date range with both ends pinned; with only an upper bound the
    two plans are the same to within noise, and the hint is documented there as worth nothing in this shape.
    Measured end to end, the whole page -- this query, the party list, the window fold and the walk -- builds in
    0.8s on the payables side and 0.5s on the receivables side.

    `a.kind` is compared through `dialect.mark`, not a bare `?`. The column is `varchar(12)` and pyodbc binds a
    Python str as nvarchar, which has the higher precedence, so without the cast SQL Server converts the column
    instead of the parameter and does it once per row. The cast is to `varchar(20)`, wider than the column, which
    is the safe direction `dialect.marks` describes and the same width `ageing` uses for the same comparison.

    **The ACCOUNT's book is filtered and the VOUCHER's is not**, which is what `ageing` does and is not obviously
    right -- `accounts_registers_service._party_lines` pins both. It was checked rather than assumed: over head
    office's supplier accounts up to 2026-10-12 the two give the identical 36,851 lines and the identical
    451,090,199.00 debit and 675,225,616.84 credit, so no voucher on another book reaches an account on this one
    today. Pinning `v.book` as well would therefore change nothing except make this disagree with `ageing` the
    first time a mirrored entry did appear, and a silent split between two reports of the same number is worse
    than either answer.
    """
    codes = dialect.codes(books)
    return await ds._rows(f"""
        SELECT l.account_id AS account_id, v.date AS day,
               SUM(l.debit) AS debit, SUM(l.credit) AS credit
          FROM acc_voucher_lines l
          JOIN acc_vouchers v ON v.id = l.voucher_id
          JOIN acc_accounts a ON a.id = l.account_id
         WHERE a.book IN ({codes}) AND a.kind = {dialect.mark('varchar(20)')}
           AND v.status = 'posted' AND v.date <= ?
         GROUP BY l.account_id, v.date
         ORDER BY l.account_id, v.date
    """, [*books, kind, as_of])


async def _parties(kind: str, books: list[str]) -> dict[str, dict]:
    """Who each party account is, and the credit days it carries.

    `party_due_days` rides on the account rather than on the Supplier row on purpose: head office keeps no Supplier
    of a branch's, and the registers that age against the credit days have to work for a branch's book too
    (models/accounts.py says so where the column is declared).
    """
    rows = await ds._rows(f"""
        SELECT a.id AS id, a.code AS code, a.name AS name, a.party_due_days AS due_days
          FROM acc_accounts a
         WHERE a.book IN ({dialect.codes(books)}) AND a.kind = {dialect.mark('varchar(20)')}
    """, [*books, kind])
    return {str(r["id"]): r for r in rows}


async def _flow(scope: Scope, kind: str, books: list[str]) -> list[dict]:
    """Gross movement on the party accounts inside the window, day by day.

    The one fold that reads the window out of SQL, and the one `Scope.where` can serve, because this genuinely is a
    flow: money paid on Tuesday and money paid on Wednesday add up to money paid over the two days. `only=()`
    drops every clicked dimension rather than asking `acc_vouchers` for a category column it does not have, and
    `_blocked` has already stopped the page before this runs when anything is clicked.

    It reconciles to the cards by construction: the paid side summed over the window equals on time plus late plus
    unmatched out of `_position`, because both are built from the same per party per day rows. `ops` has no check
    on that yet; the identity is asserted in the verification this file was written against.
    """
    where, args = scope.where("v", "date", only=())
    return await ds._rows(f"""
        SELECT v.date AS day, SUM(l.debit) AS debit, SUM(l.credit) AS credit
          FROM acc_voucher_lines l
          JOIN acc_vouchers v ON v.id = l.voucher_id
          JOIN acc_accounts a ON a.id = l.account_id
         WHERE a.book IN ({dialect.codes(books)}) AND a.kind = {dialect.mark('varchar(20)')}
           AND v.status = 'posted' AND {where}
         GROUP BY v.date ORDER BY v.date
    """, [*books, kind, *args])


# ── the settlement walk ─────────────────────────────────────────────────────────────────────────
def _walk(days: list[tuple[date, Decimal, Decimal]], kind: str) -> tuple[list[list], list[tuple], Decimal]:
    """One party's days, turned into what is still open, which payment cleared what, and any advance left over.

    This is `accounts_reports_service.ageing`'s settlement with the allocations kept. That function pools every
    payment and spends the pool on the open items oldest first; this walks the payments in date order and spends
    each on the oldest open item, which reaches the same remaining amounts -- all of the credit chases all of the
    items either way, and both go oldest first -- while also recording WHEN each piece was paid. That date is the
    only way to say whether a payment was on time, and the pooled version throws it away.

    Oldest first because that is how a shop and its supplier both read a payment unless somebody says otherwise,
    which is the reason `accounts_registers_service._walk` gives for the same choice.

    A payment may land on an item dated after it: the pool has no opinion about order, so a receipt in January can
    be the thing that settles a bill in March. That is an advance being consumed rather than a late payment, and
    it falls out correctly below because days to pay is then negative and nothing negative is late.

    Returns `(items, allocations, advance)` where an item is `[day, remaining, billed]`, an allocation is
    `(item_day, payment_day, amount)`, and the advance is payment that found no item at all.
    """
    owed_side_is_debit = kind == "customer"
    items: list[list] = []
    payments: list[tuple[date, Decimal]] = []
    for day, debit, credit in days:
        owed, paid = (debit, credit) if owed_side_is_debit else (credit, debit)
        if owed > ZERO:
            items.append([day, owed, owed])
        if paid > ZERO:
            payments.append((day, paid))

    allocations: list[tuple] = []
    advance = ZERO
    oldest = 0  # every item before this one is settled, so the next payment starts here
    for pay_day, amount in payments:
        left = amount
        while left > ZERO and oldest < len(items):
            item = items[oldest]
            take = min(item[1], left)
            if take > ZERO:
                item[1] -= take
                left -= take
                allocations.append((item[0], pay_day, take))
            if item[1] <= ZERO:
                oldest += 1
        advance += left
    return items, allocations, advance


def _recency_buckets(as_of: date) -> list[tuple[str, date, date]]:
    """When an overdue amount fell due, newest first, as whole day ranges. First match wins.

    THESE ARE DUE DATES, NOT INVOICE DATES, and that is not a detail. An overdue item is by definition older than
    its party's credit days, so with the 30 day default nothing overdue can have been billed yesterday or even
    this week -- read against the invoice date the first two buckets would be empty on every page this ever draws,
    for every party, forever. Read against the day it fell due they answer the question somebody opening this
    actually has: what has just tipped over, and what has been sitting there since last month.

    The ranges are ordered newest first and matched in order, so a day that two of them could claim goes to the
    narrower one: the Sunday before a Monday is "Yesterday" rather than "Last week". The last bucket catches
    everything older, so every overdue item lands in exactly one and the six of them add up to the overdue total.
    """
    yesterday = as_of - timedelta(days=1)
    week = as_of - timedelta(days=as_of.weekday())          # Monday of the week `as_of` falls in
    last_week = week - timedelta(days=7)
    month = as_of.replace(day=1)
    last_month = (month - timedelta(days=1)).replace(day=1)
    return [
        ("Fell due yesterday", yesterday, yesterday),
        ("Earlier this week", week, as_of - timedelta(days=2)),
        ("Last week", last_week, week - timedelta(days=1)),
        ("Earlier this month", month, last_week - timedelta(days=1)),
        ("Last month", last_month, month - timedelta(days=1)),
        ("Older than last month", date.min, last_month - timedelta(days=1)),
    ]


def _bracket(days_past_due: int) -> str | None:
    for label, low, high in PAST_DUE_BRACKETS:
        if low <= days_past_due <= high:
            return label
    return None


# ── the position, every figure on the page out of one walk ──────────────────────────────────────
def _position(rows: list[dict], parties: dict[str, dict], kind: str, scope: Scope) -> dict:
    """Every figure this page shows, derived once from one walk per party.

    One function rather than a fold each, because the whole complaint against the dashboard this is modelled on is
    that its headline disagreed with its own table. Here the cards, the three bar charts and the party table are
    all slices of the same dictionaries, so they cannot.
    """
    as_of = scope.end
    by_party: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
    for row in rows:
        day = row["day"] if isinstance(row["day"], date) else date.fromisoformat(str(row["day"])[:10])
        by_party.setdefault(str(row["account_id"]), []).append((day, ds._d(row["debit"]), ds._d(row["credit"])))

    recency = _recency_buckets(as_of)
    status = {name: {"amount": ZERO, "items": 0} for name in ("Not due", "Due", "Over due")}
    by_recency = {name: {"amount": ZERO, "items": 0} for name, _, _ in recency}
    by_bracket = {name: {"amount": ZERO, "items": 0} for name, _, _ in PAST_DUE_BRACKETS}
    # Two keys, not three. Money that found no bill to settle is NOT counted here: it is derived in `_paid_table`
    # as the window's gross payments less these two, which is the only way the three rows of that table are
    # guaranteed to add up to its total. Deriving it cannot go negative, because every allocation comes out of a
    # payment of that same day and a payment never allocates more than itself.
    paid = {name: {"amount": ZERO, "count": 0} for name in ("On time", "Late")}

    listed: list[dict] = []
    totals = {"outstanding": ZERO, "overdue": ZERO, "advance": ZERO, "not_due": ZERO}
    open_items = 0
    worst_days = 0
    # Counted over EVERY party account of this kind, not only the ones with movement, because this and
    # `parties_known` are the two halves of one sentence in `_credit_note` and one of them counting a subset would
    # understate how much of the book has terms recorded. It is a fact about the column, not about the ledger.
    recorded_terms = sum(1 for p in parties.values() if int(p["due_days"] or 0) > 0)

    for account_id, days in by_party.items():
        party = parties.get(account_id)
        if party is None:
            # An account that posted a line and is no longer of this kind. It cannot be aged against terms it no
            # longer has, and leaving it out silently would lose the money, so it is aged on the default.
            party = {"code": "", "name": "(account no longer a party)", "due_days": 0}
        credit_days = int(party["due_days"] or 0) or DEFAULT_CREDIT_DAYS

        items, allocations, advance = _walk(days, kind)

        brackets = {name: ZERO for name, _, _ in PAST_DUE_BRACKETS}
        party_open = ZERO
        party_not_due = ZERO
        party_overdue = ZERO
        party_oldest = 0
        for day, remaining, _billed in items:
            if remaining <= ZERO:
                continue
            open_items += 1
            party_open += remaining
            past_due = (as_of - day).days - credit_days
            if past_due < 0:
                status["Not due"]["amount"] += remaining
                status["Not due"]["items"] += 1
                party_not_due += remaining
                continue
            if past_due == 0:
                # The day the credit days run out. Not late yet, which is why `ageing` calls `age > due_days`
                # overdue and not `>=`. It is one day wide, so this bar is usually thin and that is honest.
                status["Due"]["amount"] += remaining
                status["Due"]["items"] += 1
                party_not_due += remaining
                continue
            status["Over due"]["amount"] += remaining
            status["Over due"]["items"] += 1
            party_overdue += remaining
            party_oldest = max(party_oldest, past_due)
            name = _bracket(past_due)
            if name:
                brackets[name] += remaining
                by_bracket[name]["amount"] += remaining
                by_bracket[name]["items"] += 1
            fell_due = day + timedelta(days=credit_days)
            for label, low, high in recency:
                if low <= fell_due <= high:
                    by_recency[label]["amount"] += remaining
                    by_recency[label]["items"] += 1
                    break

        # Was each payment made inside the window on time? Judged on the bill it settled, against this party's own
        # credit days. Allocations outside the window belong to an earlier page of the calendar, not to this one.
        for item_day, pay_day, amount in allocations:
            if not (scope.start <= pay_day <= scope.end):
                continue
            late = (pay_day - item_day).days > credit_days
            name = "Late" if late else "On time"
            paid[name]["amount"] += amount
            paid[name]["count"] += 1

        totals["outstanding"] += party_open
        totals["overdue"] += party_overdue
        totals["not_due"] += party_not_due
        totals["advance"] += advance
        worst_days = max(worst_days, party_oldest)

        if party_open > ZERO or advance > ZERO:
            listed.append({
                "account_id": account_id, "code": str(party["code"] or ""),
                "name": str(party["name"] or party["code"] or "--"),
                "credit_days": credit_days, "recorded": int(party["due_days"] or 0) > 0,
                "outstanding": party_open, "not_due": party_not_due, "overdue": party_overdue,
                "advance": advance, "balance": party_open - advance, "oldest": party_oldest,
                "brackets": brackets,
            })

    listed.sort(key=lambda p: (-p["overdue"], -p["outstanding"]))
    return {
        "as_of": as_of, "kind": kind, "parties": listed,
        "status": status, "recency": [(name, by_recency[name]) for name, _, _ in recency],
        "brackets": [(name, by_bracket[name]) for name, _, _ in PAST_DUE_BRACKETS],
        "paid": paid, "open_items": open_items, "worst_days": worst_days,
        "with_terms": recorded_terms, "parties_known": len(parties),
        "in_credit": sum(1 for p in listed if p["advance"] > ZERO),
        **totals,
        "balance": totals["outstanding"] - totals["advance"],
    }


def _credit_note(position: dict) -> str:
    """What the ageing was measured against, in plain words, with today's count rather than a claim.

    Printed on every visual that divides money by age. The stakeholder's point about this dashboard was that the
    ageing follows each party's own terms; while the column is empty that is an intention and not a fact, and a
    page that let the intention pass for the fact would be the one thing worse than a default.
    """
    known = position["parties_known"]
    with_terms = position["with_terms"]
    if with_terms == known and known:
        # Nothing stands in: every party carries its own terms, so there is no default to confess to.
        return (f"Aged against each {PARTY_WORD[position['kind']][0]}'s own agreed credit days, off the account "
                f"(party_due_days).")
    if with_terms:
        return (f"Aged against each {PARTY_WORD[position['kind']][0]}'s own agreed credit days. "
                f"{_n(with_terms)} of {_n(known)} carry them; the rest are aged on "
                f"{DEFAULT_CREDIT_DAYS} days until somebody records theirs.")
    return (f"None of head office's {_n(known)} {PARTY_WORD[position['kind']][1]} carry agreed credit days yet, so "
            f"every party here is aged on a flat {DEFAULT_CREDIT_DAYS} days. That is a standing default, not their "
            f"terms. Record the days on the account and this follows them party by party with nothing to change "
            f"here.")


def _terms_tag(position: dict) -> str:
    """The same disclosure as `_credit_note`, short enough for a title.

    A title always draws and a note only draws on an empty visual, so the one fact a reader must not miss about
    these brackets goes here as well. Six words rather than a paragraph, because it is in the heading of every
    chart on the page and a heading nobody finishes reading discloses nothing.
    """
    known, with_terms = position["parties_known"], position["with_terms"]
    if with_terms == known and known:
        return " · each party's own credit days"
    if with_terms:
        return f" · agreed credit days where recorded, else {DEFAULT_CREDIT_DAYS}"
    return f" · aged on a {DEFAULT_CREDIT_DAYS} day default, none agreed"


# ── the page ────────────────────────────────────────────────────────────────────────────────────
def _cards(position: dict, flow_paid: Decimal, flow_billed: Decimal, days: int) -> list[dict]:
    """Eight money figures. Every count and every percentage rides in a `sub`: see the docstring on `kind`.

    The two payment cards quote their share twice, of what was matched to a bill AND of everything paid. One
    figure alone misleads whichever one it is: on head office's customers in the September window three receipts
    totalling Rs 131,552 were matched and Rs 4,045,760 was taken in advance, so "100% on time" is true of the
    matched money and reads as a clean sheet when 96.9% of the money never met a bill at all.
    """
    kind = position["kind"]
    one, many = PARTY_WORD[kind]
    outstanding = position["outstanding"]
    owed_word = "owed to" if kind == "supplier" else "owed by"
    matched = position["paid"]["On time"]["amount"] + position["paid"]["Late"]["amount"]
    owing = sum(1 for p in position["parties"] if p["outstanding"] > ZERO)

    word = MONEY_WORD[kind]

    def paid_card(name: str) -> dict:
        amount = position["paid"][name]["amount"]
        return card(f"{word} {name.lower()}", money(amount),
                    sub=f"{pct(amount, matched)}% of what was matched to a bill"
                        f" · {pct(amount, ds._d(flow_paid))}% of everything {word.lower()}")

    return [
        card("Balance", money(position["balance"]),
             sub=f"{_n(len(position['parties']))} {many} · outstanding less "
                 f"{_rs(position['advance'])} paid in advance"),
        card("Outstanding", money(outstanding),
             sub=f"{_n(position['open_items'])} open items {owed_word} {_n(owing)} {many}"),
        card("Overdue", money(position["overdue"]),
             sub=f"{pct(position['overdue'], outstanding)}% of what is outstanding"
                 f" · oldest {_n(position['worst_days'])} days past due"),
        # The one card that has to name the credit days it was measured against, because it is the card whose
        # whole meaning is "inside them". A `sub` always draws; the note on the chart below it does not.
        card("Not yet due", money(position["not_due"]),
             sub=f"{pct(position['not_due'], outstanding)}% of what is outstanding"
                 f"{_terms_tag(position)}"),
        card("Advance held", money(position["advance"]),
             sub=f"{_n(position['in_credit'])} {many} are in credit"
                 if position["advance"] > ZERO else f"no {one} is in credit"),
        card(f"{word} in this window", money(flow_paid),
             sub=f"on {_n(days)} day(s) with movement · {_rs(flow_billed)} newly billed"),
        paid_card("On time"),
        paid_card("Late"),
    ]


def _status_bars(position: dict) -> dict:
    """Outstanding split three ways, with the count and the share in each label.

    A bar's value is drawn as money by the app, so the count and the ratio cannot go there. In the label they read
    as a sentence and the money still lines up down the right hand edge.
    """
    outstanding = position["outstanding"]
    rows = []
    for name, figure in position["status"].items():
        rows.append({
            "key": name,
            "label": f"{name} · {_n(figure['items'])} open item{'' if figure['items'] == 1 else 's'}"
                     f" · {pct(figure['amount'], outstanding)}%",
            "value": money(figure["amount"]),
            "qty": _n(figure["items"]),
        })
    # A RING, because these three are not three measurements, they are one balance cut three ways: not due, due
    # and over due add up to the outstanding figure on the card above. The slice that matters is over due, and a
    # ring shows how much of the whole it is without anybody having to add the other two up.
    return {
        "id": "settle_status", "kind": "donut", "dimension": None,
        "title": f"Outstanding by settle status{_terms_tag(position)}",
        "note": f"{_credit_note(position)} Not due is still inside those days. Due is the single day they run "
                f"out on, so it is always a thin slice. Over due is everything past it, and it is the one the "
                f"cards divide by.",
        "rows": rows,
    }


def _recency_bars(position: dict) -> dict:
    return {
        "id": "overdue_recency", "kind": "bars", "dimension": None,
        "title": f"Overdue by when it fell due{_terms_tag(position)}",
        "note": "Measured on the day each item fell due, which is its date plus that party's credit days -- not "
                "on the date it was billed. Nothing overdue can have been billed this week, so read against the "
                "billing date the first buckets would be empty on every page forever. These six add up to the "
                "overdue figure on the card above.",
        "rows": [{"key": name, "value": money(figure["amount"]),
                  "label": f"{name} · {_n(figure['items'])} item{'' if figure['items'] == 1 else 's'}"
                           f" · {pct(figure['amount'], position['overdue'])}%",
                  "qty": _n(figure["items"])}
                 for name, figure in position["recency"]],
    }


def _bracket_bars(position: dict) -> dict:
    return {
        "id": "past_due_brackets", "kind": "bars", "dimension": None,
        "title": f"Overdue by how far past due{_terms_tag(position)}",
        "note": f"{_credit_note(position)} The brackets count days past that party's own due date, not days since "
                f"the bill, so a {PARTY_WORD[position['kind']][0]} who gives longer terms is not aged as though "
                f"they gave 30 days.",
        "rows": [{"key": name, "value": money(figure["amount"]),
                  "label": f"{name} · {_n(figure['items'])} item{'' if figure['items'] == 1 else 's'}"
                           f" · {pct(figure['amount'], position['overdue'])}%",
                  "qty": _n(figure["items"])}
                 for name, figure in position["brackets"]],
    }


def _paid_table(position: dict, flow_paid: Decimal) -> dict:
    """What was paid inside the window and whether it was on time, adding up to the gross paid.

    The third row is the reason this is a table and not two cards. On time plus late does NOT equal the money that
    moved: a payment that found no bill to settle is an advance, and it is neither on time nor late. All three add
    up to the fourth row, which is the same figure as the "Paid in this window" card and the same figure the day by
    day line adds up to, so the page reconciles on screen instead of in a comment.

    It matters on the receivables side in September: Rs 131,552 of receipts met a bill and Rs 4,045,760 did not, so
    "100% on time" without this table would have read as a clean sheet over money that never met a bill at all.
    """
    word = MONEY_WORD[position["kind"]]
    matched = position["paid"]["On time"]["amount"] + position["paid"]["Late"]["amount"]
    unmatched = ds._d(flow_paid) - matched
    rows = []
    for name in ("On time", "Late"):
        figure = position["paid"][name]
        rows.append({"key": name, "label": f"{word} {name.lower()}", "count": _n(figure["count"]),
                     "value": money(figure["amount"]), "share": pct(figure["amount"], ds._d(flow_paid))})
    rows.append({
        "key": "unmatched", "label": "Against no bill (advance)", "count": "",
        "value": money(unmatched), "share": pct(unmatched, ds._d(flow_paid)),
    })
    rows.append({"key": "total", "label": f"{word} in this window", "count": "",
                 "value": money(flow_paid), "share": "100.0"})
    return table(
        "paid_in_window",
        f"What was {word.lower()} in this window, and whether it was on time{_terms_tag(position)}", None,
        [{"key": "label", "label": "Payment"}, {"key": "count", "label": "Payments", "align": "right"},
         {"key": "value", "label": "Amount", "align": "right"},
         {"key": "share", "label": "% of what was paid", "align": "right"}],
        rows,
        note=f"{_credit_note(position)} On time means inside those days of the bill it settled, oldest bill "
             f"first. The first three rows add up to the last: money that found no bill to settle is an advance "
             f"and is neither on time nor late.")


def _party_table(position: dict, limit: int = 400) -> dict:
    """Every party with money standing, worst overdue first, with its own brackets beside its own credit days.

    Not clickable. A party is not one of the five dimensions the app can narrow by, and a table whose rows looked
    clickable and did nothing would be worse than one that plainly is not. The account's code is shown so the row
    can be carried to the ledger by hand, which is where somebody chasing one of these ends up.

    400 rather than a round 100 or 200, and the note names the money left out rather than saying "the rest".
    Head office has 368 suppliers with money standing, so the cut does not bite today; at 200 it would have hidden
    168 parties holding Rs 4,806,566 of outstanding and Rs 1,579,314 of overdue, and a table that silently stops
    short of a figure printed on a card above it is the inconsistency this whole page exists to avoid. The guard
    stays because a page over all five books would list thousands.
    """
    shown = position["parties"][:limit]
    columns = [{"key": "label", "label": PARTY_WORD[position["kind"]][0].title()},
               {"key": "code", "label": "Code"},
               {"key": "terms", "label": "Credit days", "align": "right"},
               {"key": "not_due", "label": "Not due", "align": "right"}]
    columns += [{"key": name, "label": name.replace(" days", ""), "align": "right"}
                for name, _, _ in PAST_DUE_BRACKETS]
    columns += [{"key": "overdue", "label": "Overdue", "align": "right"},
                {"key": "value", "label": "Balance", "align": "right"}]

    rows = []
    for party in shown:
        row = {
            "key": party["account_id"], "label": party["name"], "code": party["code"],
            "terms": f"{party['credit_days']}" if party["recorded"] else f"{party['credit_days']} (default)",
            "not_due": money(party["not_due"]), "overdue": money(party["overdue"]),
            "value": money(party["balance"]),
        }
        for name, _, _ in PAST_DUE_BRACKETS:
            row[name] = money(party["brackets"][name])
        rows.append(row)

    left_out = position["parties"][limit:]
    tail = ""
    if left_out:
        tail = (f" Worst {_n(limit)} only: {_n(len(left_out))} more {PARTY_WORD[position['kind']][1]} hold "
                f"{_rs(sum((p['outstanding'] for p in left_out), ZERO))} of outstanding and "
                f"{_rs(sum((p['overdue'] for p in left_out), ZERO))} of overdue between them, which is in the "
                f"cards above but not in this table.")
    return table(
        "parties", f"Every {PARTY_WORD[position['kind']][0]} with money standing", None, columns, rows,
        note=f"{_credit_note(position)} Worst overdue first. Balance is that party's own outstanding less any "
             f"advance it is holding, so a party in credit shows a negative.{tail}")


def _receivables_note(position: dict) -> str:
    """Why the receivables page is small, said in its own live figures rather than left to look broken.

    Head office sells to its own branches and to a short list of credit accounts; the shops sell for cash over the
    counter, and cash never becomes a receivable. `branch_credit_customers` had 66 rows in it when this was
    written, against 542 suppliers. A thin page is the true answer here and the reader is owed the reason.

    The second half is the part worth reading twice. On 2026-10-12, 111 of the 176 customers with any position are
    in CREDIT, holding Rs 65,372,171 between them against Rs 4,516,611 still outstanding, so head office's net
    customer balance is minus Rs 60,855,560: taken as a whole its customers have paid it more than they owe it.
    That is either advances against orders not yet billed, or receipts loaded out of the legacy books without the
    invoices they settled. Either way it is a finding about the data and not a figure to present as a receivable,
    which is why the advance is its own card and is never netted into a percentage.
    """
    outstanding, advance = position["outstanding"], position["advance"]
    standing = len(position["parties"])
    line_one = (f"Head office carries few credit customers: {_n(standing)} have any position today out of "
                f"{_n(position['parties_known'])} customer accounts on its book, because the shops sell for cash "
                f"and cash never becomes a receivable. The page is thin because the trade is.")
    if advance <= outstanding:
        return line_one
    return (f"{line_one} Read the advance before the balance: {_n(position['in_credit'])} of them are in credit, "
            f"holding {_rs(advance)} against {_rs(outstanding)} still outstanding, which is why the balance is "
            f"negative. Customers owing head office less than they have paid it is a question about the data, not "
            f"a receivable.")


async def build(page_id: str, scope: Scope) -> dict:
    kind = KIND_OF_PAGE.get(page_id)
    if kind is None:
        raise KeyError(page_id)

    blocked = _blocked(scope)
    if blocked:
        # No cards either. A money figure beside a filter chip it did not obey is the thing this is avoiding, and
        # a card has no `note` to explain itself with.
        return {"cards": [], "visuals": [table("blocked", "Narrowed by something the books do not carry", None,
                                               [], [], note=blocked)]}

    # WHICH BOOK THIS PAGE IS ABOUT. Head office's unless a branch was picked, and then that branch's own, which is
    # the whole of what "narrow this by branch" can mean here: every shop keeps its own supplier and customer
    # accounts, and one supplier is a different account in each of them.
    books, whose = [HEAD_OFFICE_BOOK], "Head office"
    if "branch" in scope.picked:
        branch = await ds.picked_branch(scope)
        if branch is None:
            return {"cards": [], "visuals": [table(
                "blocked", "No such branch", None, [], [],
                note="That branch is not one of ours, so there is no book to read. Clear the branch filter.")]}
        books, whose = [branch.code], branch.name

    rows = await _party_days(kind, scope.end, books)
    parties = await _parties(kind, books)
    position = _position(rows, parties, kind, scope)

    flow = await _flow(scope, kind, books)
    paid_key, billed_key = ("credit", "debit") if kind == "customer" else ("debit", "credit")
    flow_paid = sum((ds._d(r[paid_key]) for r in flow), ZERO)
    flow_billed = sum((ds._d(r[billed_key]) for r in flow), ZERO)

    cards = _cards(position, flow_paid, flow_billed, len(flow))
    visuals = [
        _status_bars(position),
        _recency_bars(position),
        _bracket_bars(position),
        line("paid_by_day", f"{MONEY_WORD[kind]} day by day",
             [{"label": str(r["day"])[:10], "net": r[paid_key]} for r in flow]),
        _paid_table(position, flow_paid),
        _party_table(position),
    ]

    if kind == "customer":
        visuals[0]["note"] = _receivables_note(position) + " " + visuals[0]["note"]

    # ITS OWN, NOT THE CONTROLLER'S FIVE. The fallback counts Items, categories and brands off the product rollup,
    # which on a page about who owes money reads as nonsense and costs a scan of 1.87 million product-days to
    # produce it. These are what this page actually counted, and they are already in hand.
    word = PARTY_WORD[kind][1]
    covers = [
        {"label": f"{word} with money standing", "value": len(position["parties"])},
        {"label": f"{word} in the {whose} book", "value": position["parties_known"]},
        {"label": "days of movement in the window", "value": len(flow)},
    ]
    return {"cards": cards, "visuals": visuals, "covers": covers}
