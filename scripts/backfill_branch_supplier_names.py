"""Put the supplier's name back on the branch buying documents that arrived without one.

    python scripts/backfill_branch_supplier_names.py          what it would change
    python scripts/backfill_branch_supplier_names.py --yes     change it

WHAT WENT WRONG, WHICH WAS THE FETCH AND IS ALREADY FIXED
---------------------------------------------------------
529 documents carry a supplier code and no supplier name: 304 deliveries, 186 orders and 39 returns. Every one of
them was mapped from a live VPN window rather than from the restored backup.

`map_buying.py` reads the name with `LEFT JOIN "dbo.Party" p ON p.PartySno = h.PartySno`, and for seven fetch runs
`fetch_live.py` took `dbo.Party` as `("own", "ModifiedDate")`: a window, so it copied only the parties somebody had
edited in those few days. Three of 891. The join then found nothing and the name came through as null.
`fetch_live.py` now takes it as `("whole", ...)` and its own comment records the same class of fault for Users and
Items, so nothing new will arrive this way. What is left is the documents mapped before that fix.

WHY THE NAME COMES FROM OUR SUPPLIER LIST AND NOT FROM A RE-MAP
---------------------------------------------------------------
Re-mapping the window would also re-place those documents, because `map_buying.py` has since been corrected to file
a document by the godown that received the stock instead of by who typed it. That is a change worth making for all
25,205 deliveries in one planned run, not as a side effect of fixing 304 names.

`suppliers.code` IS Multi-Tec's PartySno, checked and not assumed: all 379 suppliers the godown buys from resolve by
code with none missing. So the name is read from the company's own supplier list, which is the list these documents
are reconciled against anyway. 527 of the 529 resolve. The 2 that do not name parties 550 and 212, which the
supplier load never brought because it selects parties that appear on a delivery and these appear only on an order.
Those two are left alone and reported rather than guessed at.

Nothing else is touched: only rows whose `supplier_name` is null, and only the name.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tortoise import Tortoise, connections

from app.core.config import TORTOISE_ORM

TABLES = ("branch_grns", "branch_purchase_orders", "branch_purchase_returns")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="apply it")
    args = ap.parse_args()

    await Tortoise.init(config=TORTOISE_ORM)
    db = connections.get("default")
    total, stuck = 0, 0
    for table in TABLES:
        rows = await db.execute_query_dict(
            f"SELECT COUNT(*) AS n FROM {table} d JOIN suppliers s ON s.code = d.supplier_code "
            f"WHERE d.supplier_name IS NULL")
        fixable = int(rows[0]["n"])
        rows = await db.execute_query_dict(
            f"SELECT COUNT(*) AS n FROM {table} d LEFT JOIN suppliers s ON s.code = d.supplier_code "
            f"WHERE d.supplier_name IS NULL AND s.id IS NULL")
        unknown = int(rows[0]["n"])
        print(f"   {table:28} {fixable:>5,} to fill" + (f", {unknown:,} name a supplier we do not hold" if unknown else ""))
        total += fixable
        stuck += unknown
        if args.yes and fixable:
            # Set-based, because this is one join and 529 rows: a row-at-a-time loop would be 529 round trips to say
            # the same thing. Only null names are touched, so a second run finds nothing to do.
            await db.execute_script(
                f"UPDATE d SET d.supplier_name = s.name FROM {table} d "
                f"JOIN suppliers s ON s.code = d.supplier_code WHERE d.supplier_name IS NULL")

    if not args.yes:
        print(f"\n{total:,} row(s) would be filled. Add --yes to apply.")
        await connections.close_all()
        return 0

    print("\nVerifying ...")
    left = 0
    for table in TABLES:
        rows = await db.execute_query_dict(
            f"SELECT COUNT(*) AS n FROM {table} d JOIN suppliers s ON s.code = d.supplier_code "
            f"WHERE d.supplier_name IS NULL")
        rows2 = await db.execute_query_dict(f"SELECT COUNT(*) AS n FROM {table} WHERE supplier_name IS NULL")
        print(f"   {table:28} {int(rows[0]['n']):>5,} still fillable, {int(rows2[0]['n']):>5,} still without a name")
        left += int(rows[0]["n"])
    await connections.close_all()
    if left:
        print(f"\nFAILED: {left:,} row(s) could have been filled and were not.")
        return 1
    print(f"\nDone. {stuck:,} row(s) name a supplier that is not in our supplier list and were left as they were.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
