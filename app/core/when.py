"""A date range that may carry a time, and which filters can honour one.

Every date range in the cloud app should let you pick a time too. Whoever is asking decides what their window is:
05:00 to 05:00 the next day is their trading day, 09:00 to 17:00 is a shift, and neither is this module's business.

It can be *asked* for everywhere and only *answered* where the rows carry a time:

    branch_sales.at      1,066,299 bills, 1,066,286 of them with a real time. The bill reports filtered on
                         `day` anyway, so the time was stored and never used. That is what changed.
    acc_vouchers.date    1,133,173 vouchers on a DateField, so there is no time on the row to compare against.
                         CORRECTED 14 October 2026: this file used to say the time does not exist "in their
                         software and in ours". The second half is true and the first half is wrong.
                         `dbo.AccountLedger.Date` is a timestamp and 5,340,870 of their 5,343,432 rows carry a
                         real clock time (100.0%), peaking between 17:00 and 22:00 like the trade does. WE threw
                         it away: `load_legacy_ledger.a_date` keeps `str(value)[:10]`, the date part, because the
                         column it lands in is a DateField. So the voucher-based filters below are day-grained by
                         our own import, not by the absence of the data, and 13 of them could answer a time if
                         the voucher carried one.
    branch_daily_stats   One row per branch per day, and branch_product_stats one per Item per day. They are
                         sums over a day, so an hour of one cannot be recovered. Every executive KPI sits here.

`GRAIN` records which is which and `/meta/date-filters` hands it to the app, so a time is offered only where it
changes the answer. A time control that silently does nothing is worse than none.

HOW A BOUND IS READ
-------------------
A parameter annotated `datetime` already accepts a bare `2026-10-01`, which pydantic reads as midnight, so a
screen sending dates today is unaffected. There are then two filters and no conversion between them:

    no time given      filter the date column, both ends inclusive, exactly as before
    a time given       filter the timestamp column, both ends inclusive

Nothing turns a date into an instant. An earlier version did, reading midnight on the upper bound as the end of
that day so a bare date still covered it, and that swept in an extra day for anyone asking to stop AT midnight,
because midnight and no-time-at-all are the same value. Hence two filters instead.

The one thing this cannot express is a range ending exactly at 00:00, which means the whole of that day. To end at
midnight, give the previous day with no time.
"""
from __future__ import annotations

from datetime import date, datetime, time

# "instant" -- the rows carry a timestamp, so a time is honoured.
# "day"     -- the rows carry a date and nothing finer. A time sent here is ignored.
# Every GET in this application that takes both `from` and `to`, and whether it can honour a time. Checked
# against the OpenAPI schema on 13 October 2026, not typed from memory: the first version of this table named
# eight paths that do not exist (`/activity`, `/items/{id}/movements`, `/warehouse/purchase-report`, `/tax/gst`
# and four more) and missed eleven that do, so `/meta/date-filters` was telling the app about endpoints it could
# not call. `ops/checks/date_filters.py` is what stops that happening again.
#
# "instant" -- the rows carry a timestamp and the parameter is declared `datetime`, so a time is honoured.
# "day"     -- the rows carry a date and nothing finer. A time sent here is accepted and ignored.
# "mixed"   -- a time is honoured, but some figures in the answer cannot follow it and are given for the whole
#              days the window touches. The answer names which, so a screen can mark them.
EXEC_WHY = ("the bills carry the moment each was rung, so the sales figures follow a time; cash in, cash out, "
            "cash collected and credit sales are kept per day and are given for the whole days instead")

GRAIN: dict[str, tuple[str, str]] = {
    # Bills carry the moment they were rung, and these three filter on it.
    "/accounts/registers/invoice-profit": ("instant", "each bill carries the moment it was rung"),
    "/accounts/registers/item-party-sales": ("instant", "each bill carries the moment it was rung"),
    "/accounts/registers/party-category-sales": ("instant", "each bill carries the moment it was rung"),
    "/branches/{branch_id}/activity": ("instant", "every entry is stamped with the moment it happened"),
    "/warehouse/items/price-history": ("instant", "a price change is stamped with when it was made"),
    "/warehouse/purchase-summary": ("instant", "a receipt and an order both carry their moment"),

    # Built from voucher dates. `acc_vouchers.date` is a date column, so there is no time to compare against.
    "/accounts/ledger": ("day", "a voucher is dated, not timed, in their software and in ours"),
    "/accounts/trial-balance": ("day", "built from voucher dates"),
    "/accounts/income-statement": ("day", "built from voucher dates"),
    "/accounts/month-by-month": ("day", "built from voucher dates"),
    "/accounts/day-book": ("instant", "each entry carries the moment their ledger recorded it; entries whose moment is unknown are counted and left out"),
    "/accounts/statement": ("day", "built from voucher dates"),
    "/accounts/vouchers": ("instant", "each entry carries the moment their ledger recorded it; entries whose moment is unknown are counted and left out"),
    "/accounts/cheques": ("day", "a cheque carries the date it falls due, not a time"),
    "/accounts/bank-reconciliations/statement": ("day", "a bank statement is by date"),
    "/accounts/registers/cash-flow": ("day", "built from voucher dates"),
    "/accounts/registers/account-summary": ("day", "built from voucher dates"),
    "/accounts/registers/voucher-detail": ("instant", "each entry carries the moment their ledger recorded it; entries whose moment is unknown are counted and left out"),
    "/accounts/registers/date-wise-payment": ("instant", "each entry carries the moment their ledger recorded it; entries whose moment is unknown are counted and left out"),
    "/accounts/registers/date-wise-receipt": ("instant", "each entry carries the moment their ledger recorded it; entries whose moment is unknown are counted and left out"),
    "/accounts/registers/daily-operation": ("day", "a day's operation is the unit it reports"),
    "/accounts/registers/excess-short-purchase": ("day", "built from purchase dates"),
    "/accounts/tax/gst": ("day", "a tax return is filed by date"),
    "/accounts/tax/withholding": ("day", "a tax return is filed by date"),

    # The analytical dashboards. Day, and honestly so: almost every visual on them is folded from
    # `branch_product_stats`, which is one row per Item per branch per DAY and has no moment in it at all. A time
    # control here would narrow the handful of bill-level figures and silently do nothing to the twenty charts
    # beside them, which is worse than not offering one. The dashboards say "whole days only" and mean it.
    "/dashboards/{dashboard}/{page}": ("day", "these are folded from the daily Item rollup, which holds a day "
                                              "and nothing finer"),
    # The card on the board. It builds the dashboard's own first page and cuts it down, so it is day grained for
    # exactly the reason that page is, and saying anything else here would have the board offer a time control
    # the dashboard behind it then ignores.
    "/dashboards/{dashboard}/preview": ("day", "a card shows the dashboard's own first page, which is folded "
                                               "from the daily Item rollup"),

    # CORRECTED 14 October 2026. These said "day", because `executive_service` folds every figure from
    # BranchDailyStat and its own docstring says so. That is a fact about the service, not about the data: head
    # office also holds 1,067,857 bills, each stamped `branch_sales.at` with the branch's own local clock, with
    # their lines, their returns and their till closes. Given a time, `totals_for` folds those instead.
    #
    # "mixed" because four figures still cannot follow -- cash in, cash out, cash collected and credit sales. The
    # first two are summed per day by the branch before head office sees them; the other two are in the documents
    # but their tender rows are 29% complete and 176 times too large, a barcode having been written into `amount`.
    # `executive_service.WHOLE_DAY_WHEN_TIMED` is the list, and the answer carries it so a screen can mark those
    # tiles rather than show a day's cash beside an hour's sales.
    #
    # `ops/checks/executive_grain.py` folds the same window both ways and every other figure matches exactly, bar
    # the rounding named in `_totals_from_bills`.
    "/executive/kpis": ("mixed", EXEC_WHY),
    "/executive/kpis/{kpi_id}": ("mixed", EXEC_WHY),
    "/executive/sections/{name}": ("mixed", EXEC_WHY),
    "/executive/branches/{code}": ("mixed", EXEC_WHY),
    "/executive/branches/{code}/{tab}": ("mixed", EXEC_WHY),
    "/executive/branch-comparison": ("mixed", EXEC_WHY),
    "/executive/branch-comparison/pair": ("mixed", EXEC_WHY),
}


def has_time(value: datetime | date | None) -> bool:
    """Did the caller give a time? Midnight counts as no time; see the note above."""
    return isinstance(value, datetime) and value.timetz() != time(0, 0)


def instant(value: datetime | date | None) -> datetime | None:
    """A bound as a moment. Only called when a time was given, so it never has to guess."""
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.combine(value, time(0, 0))


def day_of(value: datetime | date | None) -> date | None:
    """The day a bound falls on, for a filter with no time to compare against."""
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def voucher_bound(op: str, value: datetime | date | None) -> tuple[str, str] | None:
    """One end of a window over the books, as SQL and the value to bind: `("v.date >= ?", "2026-10-12")` for a
    whole day, `("v.[at] >= ?", "2026-10-12T17:00:00")` when a time was picked.

    `acc_vouchers.at` carries the moment their ledger recorded, filled for 1,100,573 of 1,134,868 vouchers. The
    rest are entries their own software has since deleted, and the 934 they wrote at midnight.

    A VOUCHER WITHOUT A MOMENT DROPS OUT OF A TIMED WINDOW, and that is why this is only ever used by the reports
    that LIST entries, never by the ones that state a balance. A list missing an entry is visible -- the caller
    counts them and says so. A trial balance missing 34,295 vouchers would simply stop balancing, and somebody
    would spend a day finding out why. `GRAIN` is what holds that line: the balance reports stay "day".
    """
    if value is None:
        return None
    if isinstance(value, datetime) and value.timetz() != time(0, 0):
        return f"v.{'[at]' if _mssql() else 'at'} {op} ?", value.isoformat(sep=" ", timespec="seconds")
    return f"v.date {op} ?", day_of(value).isoformat()


def narrow_vouchers(qs, from_: datetime | date | None, to: datetime | date | None):
    """The same choice as `voucher_bound`, for a Tortoise queryset over `Voucher`.

    The two exist side by side because the day book asks twice -- once through the ORM for the vouchers and once in
    SQL for their totals -- and the two must narrow identically or the page's figures would not add up to its own
    rows. Changing one without the other is the fault this pair is shaped to prevent.

    THE NAIVE DATETIME IS DELIBERATE, and Tortoise warns about it on every call. `acc_vouchers.at` holds Pakistan
    wall-clock time, because that is what their ledger recorded and what the person picking 17:00 means. Attaching
    a timezone here would have Tortoise convert to UTC and move every bound five hours, which is the one error
    that would be invisible: the figures would still look like figures.

    Checked rather than assumed, 14 October 2026, on GT for 12 October 17:00 to 21:00: this path returns 266
    vouchers and Rs 2,428,529.00 of debits; the SQL path beside it returns the same; and a hand-written query that
    nothing could have converted returns the same again. All three agree, and the day book balances in both the
    timed window and the whole day.
    """
    if from_ is not None:
        qs = qs.filter(at__gte=from_) if has_time(from_) else qs.filter(date__gte=day_of(from_))
    if to is not None:
        qs = qs.filter(at__lte=to) if has_time(to) else qs.filter(date__lte=day_of(to))
    return qs


def without_moment(op_from: datetime | date | None, op_to: datetime | date | None) -> bool:
    """Would this window be read off `at`? Then some vouchers cannot be placed in it and the caller has to say so."""
    return has_time(op_from) or has_time(op_to)


def _mssql() -> bool:
    """`AT` is a keyword in SQL Server and an ordinary name in SQLite. One import, kept local so `when` stays a
    module about meaning rather than about engines."""
    from app.core import dialect

    return dialect.engine() == dialect.MSSQL


def ignored_note(path: str, from_: datetime | date | None, to: datetime | date | None) -> str | None:
    """What to tell the caller when it sent a time to a filter with no time in its data.

    Returned on the response rather than raised: one date-and-time control serves every screen, and refusing
    would break the screens that are day-grained through nobody's fault.
    """
    if not (has_time(from_) or has_time(to)):
        return None
    grain, why = GRAIN.get(path, ("day", ""))
    if grain == "instant":
        return None
    if grain == "mixed":
        # Not "the time was not used": most of this answer did use it. The caveat is which figures did not, and
        # the answer carries those by name, so the note points at them rather than contradicting the screen.
        return f"Some figures here are by day: {why}."
    return f"This report is by day: {why}. The time was not used."
