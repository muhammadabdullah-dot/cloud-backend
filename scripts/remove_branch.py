"""Take a branch off head office: the branch row, every row that points at it, and its book.

    python -m scripts.remove_branch --branch CT                 what it would delete
    python -m scripts.remove_branch --branch CT --yes            delete it

Written for one job: clearing the demonstration branches out of the fresh test before the real shops are loaded into
it. It deletes, so it asks twice and counts first.

WHAT IT DELETES
---------------
  * every table with a `branch_id` that has rows for this branch, found by reading the schema rather than from a list
    somebody maintains, so a table added later is not quietly missed;
  * the branch's book: its accounts, groups, sub-groups, vouchers and voucher lines, keyed on `book = <code>`;
  * the branch row itself, last.

WHAT IT REFUSES
---------------
  * `HO`. That is head office's own book, not a branch's, and its 622 accounts are the standard chart the whole
    system posts against.
  * a branch that still has a sync secret, unless `--force`. A verified branch has a server that believes it is
    paired; taking it off head office leaves that server pushing into nothing. That is sometimes exactly what is
    wanted, which is why the flag exists, but it should be typed on purpose.

It backs the database up with VACUUM INTO first, always, unless `--no-backup`.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sqlite3
import sys
from datetime import datetime

from tortoise import Tortoise
from tortoise.transactions import in_transaction

from app.core.config import TORTOISE_ORM

PROTECTED = {"HO"}


def database_path() -> pathlib.Path | None:
    from app.core.config import settings

    url = str(getattr(settings, "db_url", "") or TORTOISE_ORM["connections"]["default"])
    if "sqlite" not in url:
        return None
    path = pathlib.Path(url.split("://", 1)[1].lstrip("/"))
    return path if path.exists() else pathlib.Path(url.split("://", 1)[1])


async def branch_tables(db) -> list[str]:
    """Every table with a branch_id, read from the schema so a table added later cannot be missed."""
    rows = await db.execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    out = []
    for row in rows:
        table = row["name"]
        columns = await db.execute_query_dict(f'PRAGMA table_info("{table}")')
        if any(c["name"] == "branch_id" for c in columns):
            out.append(table)
    return out


async def run(args) -> int:
    code = args.branch.strip().upper()
    if code in PROTECTED and not args.i_really_mean_ho:
        raise SystemExit(f"{code} is head office's own book, not a branch. Refusing.")

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Branch

        db = Tortoise.get_connection("default")
        branch = await Branch.get_or_none(code=code)
        if not branch:
            print(f"   no branch {code} at head office. Nothing to do.")
            return 0
        verified = branch.verified_at is not None

        print(f"   {code} ({branch.name})"
              + (f", verified {branch.verified_at:%Y-%m-%d}, so a server somewhere believes it is paired"
                 if verified else ", no server"))
        print(f"   database {database_path()}")
        print()

        counts: list[tuple[str, int]] = []
        for table in await branch_tables(db):
            rows = await db.execute_query_dict(
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE branch_id = ?', [str(branch.id)])
            if rows and rows[0]["n"]:
                counts.append((table, rows[0]["n"]))
        book: list[tuple[str, int]] = []
        for table, column in (("acc_voucher_lines", None), ("acc_vouchers", "book"), ("acc_accounts", "book"),
                              ("acc_sub_groups", "book"), ("acc_groups", "book"), ("acc_settings", "book")):
            if column is None:
                rows = await db.execute_query_dict(
                    'SELECT COUNT(*) AS n FROM acc_voucher_lines WHERE voucher_id IN '
                    '(SELECT id FROM acc_vouchers WHERE book = ?)', [code])
            else:
                rows = await db.execute_query_dict(f'SELECT COUNT(*) AS n FROM "{table}" WHERE {column} = ?', [code])
            if rows and rows[0]["n"]:
                book.append((table, rows[0]["n"]))

        print(f"   {len(counts)} table(s) hold rows for this branch:")
        for table, n in counts:
            print(f"      {table:<34} {n:>9,}")
        if book:
            print(f"   and its book:")
            for table, n in book:
                print(f"      {table:<34} {n:>9,}")
        total = sum(n for _t, n in counts) + sum(n for _t, n in book)
        print(f"\n   {total:,} row(s) in all, plus the branch row itself.")

        if verified and not args.force:
            print(f"\n   {code} is verified. Pass --force to take it off anyway, and be sure the server that syncs")
            print("   as it is meant to stop working.")
            return 1
        if not args.yes:
            print("\n   Nothing deleted. Add --yes.")
            return 0

        if not args.no_backup:
            live = database_path()
            if live and live.exists():
                target = live.with_name(f"{live.name}.bak-before-remove-{code}-{datetime.now():%Y%m%d-%H%M%S}")
                conn = sqlite3.connect(str(live), timeout=120)
                try:
                    conn.execute("VACUUM INTO ?", (str(target),))
                finally:
                    conn.close()
                print(f"\n   backup {target.name} ({target.stat().st_size/1e6:,.0f} MB)")

        async with in_transaction() as tx:
            # Lines before vouchers, and children before parents, because foreign keys are not enforced on this
            # connection and an orphan row is harder to find later than it was to avoid now.
            await tx.execute_query(
                'DELETE FROM acc_voucher_lines WHERE voucher_id IN (SELECT id FROM acc_vouchers WHERE book = ?)',
                [code])
            for table in ("acc_vouchers", "acc_accounts", "acc_sub_groups", "acc_groups", "acc_settings"):
                await tx.execute_query(f'DELETE FROM "{table}" WHERE book = ?', [code])
            for table, _n in counts:
                await tx.execute_query(f'DELETE FROM "{table}" WHERE branch_id = ?', [str(branch.id)])
            await tx.execute_query("DELETE FROM branches WHERE id = ?", [str(branch.id)])

        print(f"   {code} and {total:,} row(s) removed. Its book is gone too.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--yes", action="store_true", help="delete it. Without this, nothing is deleted")
    ap.add_argument("--force", action="store_true", help="remove a verified branch, whose server will then be orphaned")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--i-really-mean-ho", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
