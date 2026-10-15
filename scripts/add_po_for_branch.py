r"""Add the purchase order's destination branch and the user's purchasing scope, in place.

    set DB_URL=<scripts\print_db_url.py>
    .venv\Scripts\python.exe scripts\add_po_for_branch.py          what it would change
    .venv\Scripts\python.exe scripts\add_po_for_branch.py --yes     change it

WHY THIS IS NOT `sqlserver_schema.py --build`
---------------------------------------------
Its own help says "drop everything and build it", and it does. Against this database that would take 1.07 million
bills, 3.54 million bill lines and 4.2 million stock movements with it. The change here is two nullable columns and
an index, so it is applied in place. `aerich` cannot migrate SQL Server in this project, so the statements are
written out rather than generated; migration 36 carries the same change for SQLite, which a fresh development
database is built from. The same reasoning and the same shape as `scripts/add_godown_attribution.py`.

WHAT IT CHANGES
---------------
  warehouse_purchase_orders.for_branch_id   new, char(36) null, references branches(id)
  users.po_branch_id                        new, char(36) null, references branches(id)
  an index on warehouse_purchase_orders(for_branch_id)

char(36), not uniqueidentifier. Every key in this database is a 36 character string, because that is how Tortoise
maps its UUIDField on SQL Server. Declared as uniqueidentifier the column is accepted and the foreign key is then
refused -- "Column 'branches.id' is not the same data type as referencing column" -- and the whole statement rolls
back. That is the right outcome and a confusing one to read, so it is written down here.

Null on both is the behaviour there is today: an order for no branch is a central order that fills the godown, and
a user scoped to no branch buys for the company. Nothing is backfilled and nothing is rewritten, so running it
changes no figure anybody reads.

NO ACTION on both foreign keys, not SET NULL or CASCADE. SQL Server allows one cascade path per table and
`warehouse_purchase_orders` already reaches `users` twice; and an order is a record of what was bought and for
whom, so deleting a branch should be refused rather than quietly leaving orders addressed to nobody.

It is safe to run again: each statement is guarded, so a second run reports that there is nothing to do.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

# Each step: what it is, how to tell whether it is already done, and the statement that does it.
STEPS = [
    ("warehouse_purchase_orders.for_branch_id",
     "SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('warehouse_purchase_orders') "
     "AND name = 'for_branch_id'",
     "ALTER TABLE warehouse_purchase_orders ADD for_branch_id char(36) NULL "
     "CONSTRAINT FK_wh_po_for_branch FOREIGN KEY REFERENCES branches(id)"),
    ("warehouse_purchase_order_lines.for_branch_id",
     "SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('warehouse_purchase_order_lines') "
     "AND name = 'for_branch_id'",
     "ALTER TABLE warehouse_purchase_order_lines ADD for_branch_id char(36) NULL "
     "CONSTRAINT FK_wh_pol_for_branch FOREIGN KEY REFERENCES branches(id)"),
    ("index on warehouse_purchase_order_lines(for_branch_id)",
     "SELECT COUNT(*) FROM sys.indexes WHERE object_id = OBJECT_ID('warehouse_purchase_order_lines') "
     "AND name = 'idx_wh_pol_for_branch'",
     "CREATE INDEX idx_wh_pol_for_branch ON warehouse_purchase_order_lines(for_branch_id)"),
    ("users.po_branch_id",
     "SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('users') AND name = 'po_branch_id'",
     "ALTER TABLE users ADD po_branch_id char(36) NULL "
     "CONSTRAINT FK_users_po_branch FOREIGN KEY REFERENCES branches(id)"),
    ("index on warehouse_purchase_orders(for_branch_id)",
     "SELECT COUNT(*) FROM sys.indexes WHERE object_id = OBJECT_ID('warehouse_purchase_orders') "
     "AND name = 'idx_wh_po_for_branch'",
     "CREATE INDEX idx_wh_po_for_branch ON warehouse_purchase_orders(for_branch_id)"),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="add the purchase order destination and the user's buying scope")
    parser.add_argument("--yes", action="store_true", help="actually change the schema")
    args = parser.parse_args()

    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings

    if not settings.db_url.startswith("mssql://"):
        print(f"   DB_URL is not SQL Server ({settings.db_url.split('://')[0]}). A SQLite database gets this from\n"
              f"   migration 36 instead; run `aerich upgrade`.")
        return 1

    await Tortoise.init(config=TORTOISE_ORM)
    con = Tortoise.get_connection("default")
    print(f"\n   {settings.db_url.split('@')[-1]}")

    todo = []
    for what, probe, statement in STEPS:
        there = (await con.execute_query_dict(probe))[0]
        already = int(next(iter(there.values())) or 0) > 0
        print(f"   {'already there' if already else 'to add      '}  {what}")
        if not already:
            todo.append((what, statement))

    if not todo:
        print("\n   Nothing to do.")
        return 0
    if not args.yes:
        print(f"\n   {len(todo)} change(s). Nothing done. Run it again with --yes.")
        return 0

    for what, statement in todo:
        await con.execute_script(statement)
        print(f"   added  {what}")

    # Say it out loud rather than trusting the statement: a column that is not really there would fail later, on
    # a screen, rather than here.
    for what, probe, _ in STEPS:
        there = (await con.execute_query_dict(probe))[0]
        if int(next(iter(there.values())) or 0) == 0:
            print(f"\n   {what} is still not there. Nothing else was attempted.")
            await Tortoise.close_connections()
            return 1
    print(f"\n   Done, and all {len(STEPS)} confirmed present.")
    await Tortoise.close_connections()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
