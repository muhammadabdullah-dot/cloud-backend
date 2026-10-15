"""Call the read path behind every main screen, on whatever DB_URL points at, and say which ones the engine refuses.

    python scripts/check_screens.py                   every check
    python scripts/check_screens.py --only accounts    just the ones whose name contains that

The exit code is the verdict.

WHY THIS EXISTS
---------------
Moving head office onto SQL Server is mostly not a schema problem: 275 hand-written statements have to be legal on
both engines, and the ones that are not fail at the moment somebody opens a screen, with an error naming a column.
Two were found by running the dashboard and nothing else:

  GROUP BY b       an alias from the SELECT list. SQLite resolves it; SQL Server says "Invalid column name 'b'".
  GROUP BY 1       by position. SQLite allows it; SQL Server has no ordinal GROUP BY.

Neither is exotic and neither would ever be caught by a schema check, a row count or a type conversion. They are
caught by running the query. So this runs one query from behind each screen, against real data, and prints what came
back as well as whether it threw: a check that only watches for exceptions passes happily on a screen that quietly
returns nothing.

It is read-only throughout. Nothing here writes, so it is safe against a live database.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


async def checks() -> list[tuple[str, object]]:
    """Every check, as a name and a coroutine factory. Built inside the event loop because the imports need the ORM
    to be up."""
    from app.controllers import branch_page_controller as bp
    from app.controllers import executive_controller as ec
    from app.services import (accounts_registers_service as reg, accounts_reports_service as rep,
                              analytics_service as an, bill_reports_service as br,
                              branch_comparison_service as bc, branch_stock_service as bs,
                              executive_service as ex, items_service as items,
                              technical_reports_service as tech)
    from app.models import Branch

    branch = await Branch.filter(code="GT").first() or await Branch.all().first()
    code = branch.code if branch else "GT"
    end = date(2026, 9, 30)
    start = end - timedelta(days=90)
    books = ["HO"]

    async def scope(tab_period: str = "30d"):
        return await bp.scope_for(code, tab_period, None, None)

    out: list[tuple[str, object]] = [
        ("dashboard grid, 30 days", lambda: ec.list_kpis("30d")),
        ("dashboard grid, year", lambda: ec.list_kpis("ytd")),
        ("dashboard grid, one branch", lambda: ec.list_kpis("30d", branch=code)),
        ("branch page head", lambda: _head(bp, scope)),
        ("branch page overview", lambda: _tab(bp.overview, scope)),
        ("branch page people", lambda: _tab(bp.people, scope)),
        ("branch page stock", lambda: _tab(bp.stock, scope)),
        ("branch page transfers", lambda: _tab(bp.transfers, scope)),
        ("top products", lambda: ex.top_products(_period(ex, start, end), limit=10)),
        ("top categories", lambda: ex.top_by(_period(ex, start, end), "category", limit=10)),
        ("top brands", lambda: ex.top_by(_period(ex, start, end), "brand", limit=10)),
        ("top departments", lambda: ex.top_by(_period(ex, start, end), "department", limit=10)),
        ("cashier ranking", lambda: ex.cashier_ranking(_period(ex, start, end))),
        ("ABC analysis", lambda: an.abc(start, end)),
        ("XYZ analysis", lambda: an.xyz(start, end)),
        ("ABC and XYZ together", lambda: an.combined(start, end)),
        ("sold least", lambda: an.sold_least(start, end)),
        ("unsold on shelf", lambda: an.unsold_on_shelf(start, end)),
        ("dead stock tile", lambda: an.dead_stock_summary()),
        ("dead stock detail", lambda: an.dead_stock()),
        ("stock movement", lambda: an.movement(start, end)),
        ("branch options", lambda: an.branch_options()),
        ("branch stock list", lambda: bs.items(str(branch.id), q=None, state="in-stock", origin=None,
                                              sort="value", limit=25, offset=0) if branch else None),
        ("item search, company wide", lambda: items.company_search("SHAN", limit=10)),
        ("item search, branch stock", lambda: _branch_rows(items)),
        ("branch comparison", lambda: bc.compare(_period(ex, start, end))),
        ("accounts cash flow", lambda: reg.cash_flow(books, start, end)),
        ("accounts summary", lambda: reg.account_summary(books, start, end)),
        ("accounts voucher detail", lambda: reg.voucher_detail(books, start, end)),
        ("accounts party summary", lambda: reg.party_summary(books, "supplier", end)),
        ("accounts daily operation", lambda: reg.daily_operation(books, start, end)),
        ("trial balance", lambda: rep.trial_balance(books, False, start, end)),
        ("trial balance, together", lambda: rep.trial_balance(books, True, None, end)),
        ("bill reports, invoice profit", lambda: br.invoice_profit(books, start, end, limit=25)),
        ("bill reports, party category", lambda: br.party_category_sales(books, start, end)),
        ("bill reports, item by party", lambda: br.item_party_sales(books, start, end)),
        ("technical report, errors", lambda: tech.errors(limit=25)),
    ]
    return out


def _period(ex, start, end):
    """A Period the way the app builds one: it carries the equal-length window before it, because every figure on
    the dashboard is reported against that comparison."""
    span = (end - start)
    return ex.Period(id="custom", label="custom", start=start, end=end,
                     prev_start=start - span - timedelta(days=1), prev_end=start - timedelta(days=1),
                     compare_label="the period before")


async def _head(bp, scope):
    return await bp.head(await scope())


async def _tab(fn, scope):
    return await fn(await scope())


async def _branch_rows(items):
    """Search the branches' stock lists. It needs the lists themselves, and returns nothing at all when handed an
    empty list, so passing [] made this check pass without running a single query."""
    return await items.branch_rows(await items.latest_branch_lists(), terms=["SHAN"], limit=10)


def shape(value) -> str:
    """What came back, in a few words. A check that only watches for exceptions passes on a screen that silently
    returns nothing, so the size of the answer is part of the result."""
    if value is None:
        return "None"
    if isinstance(value, tuple):
        return " + ".join(shape(v) for v in value)
    if isinstance(value, (list,)):
        return f"{len(value)} row(s)"
    if isinstance(value, dict):
        rows = sum(len(v) for v in value.values() if isinstance(v, list))
        return f"{len(value)} key(s)" + (f", {rows} row(s)" if rows else "")
    for attr in ("items", "kpis", "rows"):
        got = getattr(value, attr, None)
        if isinstance(got, list):
            return f"{len(got)} {attr}"
    return type(value).__name__


async def run(args) -> int:
    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings

    engine = settings.db_url.split("://")[0]
    print(f"   {engine}: {settings.db_url.split('@')[-1] if '@' in settings.db_url else settings.db_url}")
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        every = await checks()
        if args.only:
            every = [(n, f) for n, f in every if args.only.lower() in n.lower()]
        print(f"   {len(every)} screen(s)\n")
        print(f"   {'screen':<32} {'seconds':>8}   what came back")
        failed: list[tuple[str, Exception]] = []
        empty: list[str] = []
        for name, make in every:
            at = time.monotonic()
            try:
                got = make()
                if hasattr(got, "__await__"):
                    got = await got
                took = time.monotonic() - at
                described = shape(got)
                if described in ("None", "0 row(s)", "0 key(s)"):
                    empty.append(name)
                    described += "   <- nothing"
                print(f"   {name:<32} {took:>8.2f}   {described}")
            except Exception as exc:  # noqa: BLE001: finding what this engine refuses is the whole job
                failed.append((name, exc))
                print(f"   {name:<32} {time.monotonic()-at:>8.2f}   FAILED  {str(exc).splitlines()[0][:90]}")

        print()
        if failed:
            print(f"   {len(failed)} screen(s) failed on {engine}:")
            for name, exc in failed:
                print(f"\n      {name}")
                print(f"         {type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
            return 1
        if empty:
            print(f"   every screen answered, but {len(empty)} returned nothing, which may be right or may not:")
            print(f"      {', '.join(empty)}")
        print(f"   All {len(every)} screen(s) answered on {engine}.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="just the checks whose name contains this")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
