"""Take out the demonstration data that was seeded before the real history arrived, and nothing else.

    python scripts/remove_demo_data.py                what it would remove
    python scripts/remove_demo_data.py --yes           remove it

Works against whatever `DB_URL` points at, so it can be run on the SQLite file and on SQL Server and leave the two
saying the same thing.

WHAT IT REMOVES, AND HOW IT KNOWS
---------------------------------
Everything that came from Multi-Tec carries a marker: `source = 'legacy'` on every branch document and every roster
row, `origin = 'legacy'` on suppliers. The demonstration data has no such marker and is identified by an explicit
rule per table, written out below rather than inferred, because a delete that guesses is a delete nobody can check.

  78 products         codes beginning 710 or 720. Multi-Tec's own run 100,000 to 167,803, so none of theirs
                      can be caught by this: checked against all 67,804.
                      Their aliases, supplier links, price history and stock rows go with them.
  5 suppliers         `origin = 'HO'`, the seeded list from before the real 534 arrived.
  the godown's demo   4 purchase orders, 3 GRNs and their 14 lines, 117 stock movements, 64 cycle counts.
                      These are head office's OWN warehouse, seeded to show the screens working.
  4 members           and their 12 loyalty entries.
  32 notices          seeded announcements.
  1 promotion         a seeded campaign.
  15 vouchers         head office's own seeded postings, and their 33 lines: a Rs 5,500,000 opening balance, three
                      purchase vouchers from the demo GRNs, a stock correction, and ten transfer vouchers whose
                      transfers were already removed with the demo branches. Told apart by `source`: every voucher
                      built from their data carries `legacy:`, and none of these do.
  5 accounts          the payable account each seeded supplier was given. Removed after their lines, so nothing is
                      deleted out from under a posting, and before the suppliers, which is what names them.

WHAT IT KEEPS, DELIBERATELY
---------------------------
  every user          144 of them: 6 sign-ins and 138 of their people. The owner asked for the logins to stay, and
                      their people are records that 1,036,271 bills point at.
  bins, racks         the godown's physical layout. Structure, not data: removing it would break the warehouse
  and counters        screens rather than clean them.
  8,434 list entries  departments, categories, brands, manufacturers and units. These look seeded and are not:
                      `item_lists_service.sync_list` derived them from the 67,804 real Items during the load.
  all five branches   and everything marked legacy.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from tortoise import Tortoise
from tortoise.transactions import in_transaction

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# The 78 seeded Items, by code. Multi-Tec's own Item numbers run 100,000 to 167,803, so nothing of theirs begins
# with either of these: checked against all 67,804 before this rule was written.
DEMO_SKU_PREFIXES = ("710", "720")


async def counts(db) -> list[tuple[str, str, list]]:
    """Every rule, as a label, a DELETE, and its parameters. Counted first, applied second, so the report and the
    action can never describe different things."""
    clause = " OR ".join('"sku" LIKE ?' for _ in DEMO_SKU_PREFIXES)
    demo_products = await db.execute_query_dict(
        f'SELECT "sku" FROM "products" WHERE {clause}', [p + "%" for p in DEMO_SKU_PREFIXES])
    skus = [r["sku"] for r in demo_products]
    marks = ", ".join("?" * len(skus)) if skus else "''"

    # The ledger accounts that stand for a seeded supplier, worked out before the suppliers are deleted, because
    # afterwards there is nothing left to recognise them by.
    seeded_accounts = [r["id"] for r in await db.execute_query_dict(
        """SELECT a."id" FROM "acc_accounts" a JOIN "suppliers" s ON a."system_key" = 'supplier:' || s."id"
            WHERE s."origin" = ?""", ["HO"])]
    account_marks = ", ".join("?" * len(seeded_accounts)) if seeded_accounts else "''"

    rules: list[tuple[str, str, list]] = []

    # THE BOOKS. Head office's own seeded activity posted itself into the ledger, and an earlier version of this
    # script removed the documents and left the postings, which is worse than removing neither: the trial balance
    # would still carry Rs 5,500,000 of seeded opening balances and purchase vouchers crediting suppliers that no
    # longer exist. 15 vouchers and 33 lines, against 14,545 vouchers from Multi-Tec that stay.
    #
    # `source` tells them apart with nothing to judge: every voucher built from their data carries `legacy:<shop>:
    # <number>`, and every one of the other 15 carries a head office source (`grn:`, `transfer-out:`,
    # `stock-corrections:`) or none at all, which is the seeded opening balance. Ten of them point at transfers that
    # were removed with the demo branches, so they are already orphans.
    not_theirs = '"source" IS NULL OR "source" NOT LIKE \'legacy:%\''
    rules += [
        ("lines of head office's own seeded vouchers",
         f'DELETE FROM "acc_voucher_lines" WHERE "voucher_id" IN (SELECT "id" FROM "acc_vouchers" WHERE {not_theirs})',
         []),
        ("head office's own seeded vouchers", f'DELETE FROM "acc_vouchers" WHERE {not_theirs}', []),
    ]
    if seeded_accounts:
        # After the lines above, these carry nothing. Removed before the suppliers they name, or the join that
        # found them stops working.
        rules.append(("payable accounts for the seeded suppliers",
                      f'DELETE FROM "acc_accounts" WHERE "id" IN ({account_marks})', seeded_accounts))

    if skus:
        for table, column in (("product_aliases", "product_id"), ("product_suppliers", "product_id"),
                              ("product_price_changes", "product_id"), ("product_attachments", "product_id"),
                              ("warehouse_batches", "product_id"), ("warehouse_bin_moves", "product_id"),
                              ("warehouse_stock_movements", "product_id"), ("cycle_counts", "product_id"),
                              ("warehouse_grn_lines", "product_id"),
                              ("warehouse_purchase_order_lines", "product_id")):
            rules.append((f"{table} for the demo Items", f'DELETE FROM "{table}" WHERE "{column}" IN ({marks})', skus))
        # The seeded campaign is on a demo Item, and `promotion_code` on a sale line is plain text that no line
        # uses, so nothing is left pointing at it.
        rules.append(("promotions on the demo Items",
                      f'DELETE FROM "promotions" WHERE "product_sku" IN ({marks})', skus))
        rules.append(("branch figures for those promotions",
                      f'DELETE FROM "branch_promotion_stats" WHERE "product_sku" IN ({marks})', skus))
        rules.append(("the 78 demo Items", f'DELETE FROM "products" WHERE "sku" IN ({marks})', skus))

    rules += [
        ("head office's demo GRN lines", 'DELETE FROM "warehouse_grn_lines"', []),
        ("head office's demo GRNs", 'DELETE FROM "warehouse_grns"', []),
        ("head office's demo order lines", 'DELETE FROM "warehouse_purchase_order_lines"', []),
        ("head office's demo purchase orders", 'DELETE FROM "warehouse_purchase_orders"', []),
        ("head office's demo stock movements", 'DELETE FROM "warehouse_stock_movements"', []),
        ("head office's demo bin moves", 'DELETE FROM "warehouse_bin_moves"', []),
        ("head office's demo batches", 'DELETE FROM "warehouse_batches"', []),
        ("demo cycle counts", 'DELETE FROM "cycle_counts"', []),
        ("demo loyalty entries", 'DELETE FROM "loyalty_entries"', []),
        ("demo members", 'DELETE FROM "members"', []),
        ("seeded notice reads", 'DELETE FROM "notice_reads"', []),
        ("seeded notices", 'DELETE FROM "notices"', []),
        ("the seeded supplier list", 'DELETE FROM "suppliers" WHERE "origin" = ?', ["HO"]),
    ]
    return rules


async def keeping(db) -> dict:
    """What survives, counted from the database rather than asserted. Shown before and after, so the second listing
    is the state and not a forecast."""
    out: dict[str, int] = {}
    theirs = "'legacy:%'"
    their_people = "'%@legacy.multitec.invalid'"
    for label, query in (("vouchers from Multi-Tec",
                          f'SELECT COUNT(*) AS n FROM "acc_vouchers" WHERE "source" LIKE {theirs}'),
                         ("their voucher lines",
                          'SELECT COUNT(*) AS n FROM "acc_voucher_lines" WHERE "voucher_id" IN '
                          f'(SELECT "id" FROM "acc_vouchers" WHERE "source" LIKE {theirs})'),
                         ("chart of accounts", 'SELECT COUNT(*) AS n FROM "acc_accounts"'),
                         ("users", 'SELECT COUNT(*) AS n FROM "users"'),
                         ("their people, of those",
                          f'SELECT COUNT(*) AS n FROM "users" WHERE "email" LIKE {their_people}'),
                         ("branch sign-ins", 'SELECT COUNT(*) AS n FROM "branch_staff"'),
                         ("branches", 'SELECT COUNT(*) AS n FROM "branches"'),
                         ("Items from Multi-Tec", 'SELECT COUNT(*) AS n FROM "products"'),
                         ("list entries", 'SELECT COUNT(*) AS n FROM "item_list_entries"'),
                         ("bills", 'SELECT COUNT(*) AS n FROM "branch_sales"'),
                         ("bins, racks and counters",
                          'SELECT (SELECT COUNT(*) FROM "bins") + (SELECT COUNT(*) FROM "racks") '
                          '+ (SELECT COUNT(*) FROM "counters") AS n')):
        try:
            out[label] = (await db.execute_query_dict(query))[0]["n"]
        except Exception:  # noqa: BLE001: a table this build does not have is not a finding
            pass
    return out


async def run(args) -> int:
    from app.core.config import TORTOISE_ORM

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        db = Tortoise.get_connection("default")
        engine = db.schema_generator.DIALECT
        print(f"   engine {engine}")

        rules = await counts(db)
        found: list[tuple[str, int, str, list]] = []
        for label, statement, params in rules:
            select = statement.replace("DELETE FROM", "SELECT COUNT(*) AS n FROM", 1)
            try:
                rows = await db.execute_query_dict(select, params)
                n = rows[0]["n"] if rows else 0
            except Exception:  # noqa: BLE001: a table this build does not have is not a finding
                continue
            if n:
                found.append((label, n, statement, params))

        total = sum(n for _l, n, _s, _p in found)
        print(f"   {len(found)} rule(s) match, {total:,} row(s) in all\n")
        for label, n, _s, _p in found:
            print(f"      {n:>8,}  {label}")

        print("\n   keeping, deliberately:")
        for label, n in (await keeping(db)).items():
            print(f"      {n:>8,}  {label}")

        if not args.yes:
            print("\n   Nothing removed. Add --yes.")
            return 0

        removed = 0
        async with in_transaction() as tx:
            for label, n, statement, params in found:
                await tx.execute_query(statement, params)
                removed += n
        print(f"\n   {removed:,} row(s) removed. Every user, every branch and everything marked legacy is untouched.")
        print("\n   what is there now:")
        for label, n in (await keeping(db)).items():
            print(f"      {n:>8,}  {label}")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
