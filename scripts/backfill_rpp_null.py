"""Retail price: store nothing where Multi-Tec recorded nothing, instead of zero.

WHAT THIS IS FOR
----------------
The Items screen shows "Rs 0" as the retail price on nine Items in ten, which reads as "we sell it for nothing"
when it means "nobody ever wrote one down". Both the Items table and the Item drawer already print a dash for a
null (`p.rpp == null ? dash() : formatPkr(p.rpp)`), and `Product.rpp` is nullable, so the honest value is NULL and
no screen needs changing.

It is their gap, not ours. `dbo.Item.RetailPrice` is non-zero on 5,928 Items of 67,804 on their own server --
8.7% -- and we hold exactly 5,928. Checked before concluding it: `MaxSalePrice` is populated on 98.4% but equals
`SalePrice` on 99.6% of the rows where RetailPrice is zero, so it is a ceiling on the selling price and not an
MRP; `PackRetailPrice` carries the same number as `RetailPrice`, row for row, so it is a duplicate rather than an
alternative; `PrintPrice`, `SalePrice2` through `SalePrice5` and `MinSalePrice` are empty on every row. There is
no live twin to read instead.

The import turned their nothing into a zero because `map_common.num` answers "0" for a NULL, which is right for
money that is genuinely zero -- a bill with no discount discounted nothing -- and wrong for a field they do not
keep. `map_common.num_or_none` now exists for the second case and `map_masters` uses it for rpp, so a future
master import stores NULL. That fix cannot reach the Items already loaded: `sync_nightly.LOAD_STAGES` is
`figures,stock,documents,buying,roster,movements` and masters are not among them, so the master is not reloaded
nightly. Hence this, once.

WHY IT IS SAFE
--------------
Reversible exactly, because no row is NULL today: every NULL after this ran came from a zero, so

    UPDATE products SET rpp = 0 WHERE rpp IS NULL

puts it back precisely. The script refuses to run if that precondition does not hold, since a pre-existing NULL
would make the undo wrong, and it takes a full backup first regardless.

It touches one nullable column on one table. Nothing computes from `rpp`: it is displayed, exported, and carried
in `ProductPriceChange.old_rpp`/`new_rpp` when someone edits a price. No figure moves.

    cd backend\\cloud-server
    .venv\\Scripts\\python.exe -m scripts.backfill_rpp_null              # say what it would do, change nothing
    .venv\\Scripts\\python.exe -m scripts.backfill_rpp_null --yes        # do it
"""
import argparse
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

from tortoise import Tortoise  # noqa: E402

from app.core.config import TORTOISE_ORM  # noqa: E402
from scripts.reconcile_legacy_receipts import back_up  # noqa: E402


async def counts(conn) -> dict[str, int]:
    _, r = await conn.execute_query(
        "SELECT COUNT(*) AS all_, "
        "       SUM(CASE WHEN rpp IS NULL THEN 1 ELSE 0 END) AS nulls, "
        "       SUM(CASE WHEN rpp IS NOT NULL AND CAST(rpp AS REAL) = 0 THEN 1 ELSE 0 END) AS zeros, "
        "       SUM(CASE WHEN CAST(rpp AS REAL) > 0 THEN 1 ELSE 0 END) AS priced "
        "FROM products")
    return {k: int(r[0][k] or 0) for k in ("all_", "nulls", "zeros", "priced")}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually write; without it nothing changes")
    ap.add_argument("--no-backup", action="store_true", help="skip the backup. Do not use this.")
    args = ap.parse_args()

    await Tortoise.init(config=TORTOISE_ORM)
    conn = Tortoise.get_connection("default")
    try:
        before = await counts(conn)
        print("\nRETAIL PRICE AS IT STANDS")
        print(f"   {before['all_']:>7,} products")
        print(f"   {before['priced']:>7,} with a retail price, which is what Multi-Tec actually records")
        print(f"   {before['zeros']:>7,} stored as zero, which is what this turns into nothing")
        print(f"   {before['nulls']:>7,} already null")

        if before["nulls"]:
            print(f"\n   STOPPING: {before['nulls']:,} row(s) are already null, so after this ran there would be no "
                  f"way to tell\n   them from the ones it changed, and the undo would set them to zero wrongly. "
                  f"Decide what those\n   rows should be first.")
            return 1
        if not before["zeros"]:
            print("\n   Nothing to do: no product has a zero retail price.")
            return 0
        if not args.yes:
            print(f"\n   Would set rpp to NULL on {before['zeros']:,} product(s). Nothing was changed. Add --yes.")
            return 0

        if args.no_backup:
            print("\n   NO BACKUP TAKEN, because --no-backup was passed.")
        else:
            print("\n   backing up first")
            where = back_up(os.environ["DB_URL"], "rpp-null")
            print(f"      {where}")

        _, _rows = await conn.execute_query(
            "UPDATE products SET rpp = NULL WHERE rpp IS NOT NULL AND CAST(rpp AS REAL) = 0")
        after = await counts(conn)
        print("\nAFTER")
        print(f"   {after['priced']:>7,} with a retail price   (was {before['priced']:,})")
        print(f"   {after['zeros']:>7,} stored as zero        (was {before['zeros']:,})")
        print(f"   {after['nulls']:>7,} null                  (was {before['nulls']:,})")

        problems = []
        if after["priced"] != before["priced"]:
            problems.append(f"the count of priced Items moved, {before['priced']:,} to {after['priced']:,}: this "
                            f"was only meant to touch zeros")
        if after["nulls"] != before["zeros"]:
            problems.append(f"{before['zeros']:,} zeros went in but {after['nulls']:,} nulls came out")
        if after["zeros"]:
            problems.append(f"{after['zeros']:,} zero(s) are still there")
        if after["all_"] != before["all_"]:
            problems.append(f"the number of products changed, {before['all_']:,} to {after['all_']:,}")
        if problems:
            print("\nPROBLEM(S)")
            for p in problems:
                print("  -", p)
            return 1

        print(f"\n{after['nulls']:,} RETAIL PRICES NOW READ AS NOT RECORDED RATHER THAN AS ZERO. "
              f"{after['priced']:,} REAL ONES UNTOUCHED.")
        print("\nTo put it back exactly, every null here came from a zero:")
        print("   UPDATE products SET rpp = 0 WHERE rpp IS NULL")
        return 0
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
