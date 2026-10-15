"""Building and reading the two derived tables in `models/rollups.py`.

Both are pure sums of `branch_product_stats`. They are rebuilt, never edited, so the only thing that can go wrong
with them is being stale, and `refresh()` is called wherever figures land.

    refresh()                    rebuild both, whole
    refresh(branch=b)            rebuild one branch's
    refresh(branch=b, since=d)   rebuild only the months from d onward, which is what a nightly top-up needs

    months_in(start, end)        which whole calendar months a range covers, and what is left over at each end
"""
from __future__ import annotations

import time
from datetime import date

from tortoise import Tortoise
from tortoise.transactions import in_transaction

from app.core import dialect
from app.core import logs


def _db():
    return Tortoise.get_connection("default")


def months_in(start: date, end: date) -> tuple[list[str], list[tuple[date, date]]]:
    """The whole calendar months a range covers, and the part-months left over at each end.

    A range almost never lands on month boundaries: a year view runs to today, and today is rarely the last of the
    month. So the answer is whole months from the rollup plus the ragged ends from the day-level table, which stays
    exact. A range shorter than one whole month has no whole months and two ends, which is the same code path.
    """
    whole: list[str] = []
    edges: list[tuple[date, date]] = []

    def first_of(d: date) -> date:
        return d.replace(day=1)

    def next_month(d: date) -> date:
        return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)

    cursor = first_of(start) if start.day == 1 else next_month(first_of(start))
    if cursor > start:
        edges.append((start, min(end, cursor - __import__("datetime").timedelta(days=1))))
    while True:
        after = next_month(cursor)
        if after - __import__("datetime").timedelta(days=1) > end:
            break
        if cursor > end:
            break
        whole.append(cursor.strftime("%Y-%m"))
        cursor = after
    if cursor <= end:
        edges.append((cursor, end))
    return whole, [(a, b) for a, b in edges if a <= b]


async def refresh(branch=None, since: date | None = None) -> dict:
    """Rebuild the rollups. Whole, one branch's, or only from a day onward.

    `since` is rounded back to the first of its month, because a month is rebuilt whole or not at all: rebuilding
    half of September would leave the other half of September's figures in the row.
    """
    started = time.monotonic()
    where, args = ["1 = 1"], []
    if branch is not None:
        where.append(f'ps."branch_id" = {dialect.mark()}')
        args.append(str(branch.id))
    if since is not None:
        where.append('ps."day" >= ?')
        args.append(since.replace(day=1).isoformat())
    scope = " AND ".join(where)

    async with in_transaction() as db:
        delete_where, delete_args = ["1 = 1"], []
        if branch is not None:
            delete_where.append(f'"branch_id" = {dialect.mark()}')
            delete_args.append(str(branch.id))
        if since is not None:
            delete_where.append('"ym" >= ?')
            delete_args.append(since.replace(day=1).strftime("%Y-%m"))
        await db.execute_query(
            f'DELETE FROM "branch_product_months" WHERE {" AND ".join(delete_where)}', delete_args)
        # The month expression appears in both the SELECT and the GROUP BY and has to be the same text in each.
        month_of = dialect.year_month('ps."day"')
        await db.execute_query(f'''
            INSERT INTO "branch_product_months"
                ("id", "branch_id", "ym", "product_sku", "product_name", "department", "category", "brand",
                 "qty", "net_sales", "cogs", "days_sold", "built_at")
            SELECT {dialect.new_id()}, ps."branch_id", {month_of}, ps."product_sku",
                   MAX(ps."product_name"), MAX(ps."department"), MAX(ps."category"), MAX(ps."brand"),
                   SUM(CAST(ps."qty" AS REAL)), SUM(CAST(ps."net_sales" AS REAL)), SUM(CAST(ps."cogs" AS REAL)),
                   COUNT(DISTINCT CASE WHEN CAST(ps."qty" AS REAL) > 0 THEN ps."day" END),
                   {dialect.now()}
              FROM "branch_product_stats" ps
             WHERE {scope}
             GROUP BY ps."branch_id", {month_of}, ps."product_sku"
        ''', args)

        # The first sale is always rebuilt whole for the branches in scope: "the first day ever" cannot be worked
        # out from a window, and a day earlier than the one on file can only be found by looking at all of it.
        first_where, first_args = ["1 = 1"], []
        if branch is not None:
            first_where.append(f'ps."branch_id" = {dialect.mark()}')
            first_args.append(str(branch.id))
        await db.execute_query(
            f'DELETE FROM "branch_product_first_sale" WHERE {" AND ".join(delete_where[:2])}', delete_args[:1])
        await db.execute_query(f'''
            INSERT INTO "branch_product_first_sale" ("id", "branch_id", "product_sku", "first_day", "built_at")
            SELECT {dialect.new_id()}, ps."branch_id", ps."product_sku", MIN(ps."day"), {dialect.now()}
              FROM "branch_product_stats" ps
             WHERE {" AND ".join(first_where)} AND CAST(ps."qty" AS REAL) > 0
             GROUP BY ps."branch_id", ps."product_sku"
        ''', first_args)

    months = (await _db().execute_query_dict('SELECT COUNT(*) AS n FROM "branch_product_months"'))[0]["n"]
    firsts = (await _db().execute_query_dict('SELECT COUNT(*) AS n FROM "branch_product_first_sale"'))[0]["n"]
    took = time.monotonic() - started
    logs.log.info("rollups rebuilt in %.1fs: %s month rows, %s first sales", took, months, firsts)
    return {"seconds": round(took, 2), "monthRows": months, "firstSales": firsts,
            "branch": getattr(branch, "code", None), "since": since.isoformat() if since else None}


async def is_stale() -> bool:
    """True when the rollups have never been built, so a caller can fall back rather than read an empty table."""
    rows = await _db().execute_query_dict('SELECT COUNT(*) AS n FROM "branch_product_months"')
    return not rows or not rows[0]["n"]
