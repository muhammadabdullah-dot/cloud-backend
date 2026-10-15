"""Add the godown's attribution columns to a database that is already built and full, without rebuilding it.

    python scripts/add_godown_attribution.py          what it would change
    python scripts/add_godown_attribution.py --yes     change it

WHY THIS IS NOT `sqlserver_schema.py --build`
---------------------------------------------
That script is the right one for standing the schema up and it is the wrong one here: its own first line of help
says "drop everything and build it", and it does. Run against this database it would take 1,048,837 bills,
3,490,278 bill lines, 25,205 deliveries and 4,241,357 stock movements with it. The schema change the Multi-Tec
godown load needs is four new columns and one relaxed constraint, all additive, so it is applied in place.

`aerich` cannot migrate SQL Server in this project, so the statements are written out here rather than generated.
Migration 35 carries the same change for SQLite, which is what a fresh development database is built from.

WHAT IT CHANGES, AND WHY EACH ONE
---------------------------------
  warehouse_purchase_orders.raised_by_id   NOT NULL -> NULL
  warehouse_purchase_orders.raised_by_name new, varchar(120) null
  warehouse_purchase_orders.source         new, varchar(10) not null default 'godown'
  warehouse_grns.received_by_name          new, varchar(120) null
  warehouse_grns.source                    new, varchar(10) not null default 'godown'
  transfers.dispatched_by_name             new, varchar(120) null
  transfers.source                         new, varchar(10) not null default 'godown'

Their head office raised 811 purchase orders for its own godown and received 5,968 deliveries into it, and the
people named on those documents are eight and twenty of their logins. `users` holds six rows, the sign-ins made for
this system; their 90 people are records in `branch_users` with no sign-in, which the migration plan decided
deliberately. So `raised_by` cannot be a foreign key for an imported order, and a name column is the only honest
place for ZAIN or ASIFPOMT to go. This is the convention `app/models/branch_purchasing.py` already states for the
branch side: "Names and codes, not foreign keys ... A name is also what a person reading the document needs."

Both existing columns keep their meaning. A document raised on these screens still fills `raised_by`, and
`source` defaults to 'godown' so all existing rows read correctly without being touched.

Idempotent: each column is checked before it is added, so a second run reports nothing to do.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tortoise import Tortoise, connections

from app.core.config import TORTOISE_ORM

# (table, column, the SQL Server type, the SQLite type). Kept side by side because the same change has to land on
# both engines and a single spelling would quietly do nothing on one of them.
NEW_COLUMNS = [
    ("warehouse_purchase_orders", "raised_by_name", "varchar(120) NULL", "VARCHAR(120)"),
    ("warehouse_purchase_orders", "source", "varchar(10) NOT NULL DEFAULT 'godown'",
     "VARCHAR(10) NOT NULL DEFAULT 'godown'"),
    ("warehouse_grns", "received_by_name", "varchar(120) NULL", "VARCHAR(120)"),
    ("warehouse_grns", "source", "varchar(10) NOT NULL DEFAULT 'godown'", "VARCHAR(10) NOT NULL DEFAULT 'godown'"),
    ("transfers", "dispatched_by_name", "varchar(120) NULL", "VARCHAR(120)"),
    ("transfers", "source", "varchar(10) NOT NULL DEFAULT 'godown'", "VARCHAR(10) NOT NULL DEFAULT 'godown'"),
]


async def columns_of(db, table: str, sqlserver: bool) -> dict[str, bool]:
    """Column name -> is_nullable, for the table as it stands right now."""
    if sqlserver:
        rows = await db.execute_query_dict(
            "SELECT c.name AS col, c.is_nullable AS nullable FROM sys.columns c "
            "JOIN sys.tables t ON t.object_id = c.object_id WHERE t.name = ?", [table])
        return {r["col"]: bool(r["nullable"]) for r in rows}
    rows = await db.execute_query_dict(f'PRAGMA table_info("{table}")')
    return {r["name"]: not r["notnull"] for r in rows}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="apply the change")
    args = ap.parse_args()

    await Tortoise.init(config=TORTOISE_ORM)
    db = connections.get("default")
    sqlserver = "mssql" in db.capabilities.dialect or db.capabilities.dialect in ("mssql", "sqlserver")
    engine = "SQL Server" if sqlserver else db.capabilities.dialect
    print(f"engine: {engine}")

    planned: list[str] = []
    for table, column, mssql_type, sqlite_type in NEW_COLUMNS:
        have = await columns_of(db, table, sqlserver)
        if not have:
            print(f"  ! {table} does not exist here, skipping")
            continue
        if column in have:
            print(f"  = {table}.{column} is already there")
            continue
        kind = mssql_type if sqlserver else sqlite_type
        planned.append(f'ALTER TABLE "{table}" ADD {"" if sqlserver else "COLUMN "}"{column}" {kind}'
                       if not sqlserver else f"ALTER TABLE [{table}] ADD [{column}] {kind}")
        print(f"  + {table}.{column} {kind}")

    # The one constraint that is relaxed rather than added. SQLite cannot alter a column's nullability at all, and it
    # does not need to: a fresh SQLite database is built from the models, which now say null=True.
    relax = None
    have = await columns_of(db, "warehouse_purchase_orders", sqlserver)
    if "raised_by_id" not in have:
        print("  ! warehouse_purchase_orders.raised_by_id is missing, nothing to relax")
    elif have["raised_by_id"]:
        print("  = warehouse_purchase_orders.raised_by_id already allows null")
    elif sqlserver:
        relax = "ALTER TABLE [warehouse_purchase_orders] ALTER COLUMN [raised_by_id] char(36) NULL"
        print("  ~ warehouse_purchase_orders.raised_by_id NOT NULL -> NULL")
    else:
        print("  ! this SQLite database predates the change and has to be rebuilt from the models to get it")

    if not planned and not relax:
        print("\nNothing to do.")
        await connections.close_all()
        return 0
    if not args.yes:
        print(f"\n{len(planned) + (1 if relax else 0)} statement(s). Add --yes to apply.")
        await connections.close_all()
        return 0

    # Each statement on its own, so a refusal names the column it refused rather than failing a batch. This is the
    # same reasoning sqlserver_schema.py gives for applying its schema one statement at a time.
    for sql in planned + ([relax] if relax else []):
        print(f"  running: {sql}")
        await db.execute_script(sql)
    print("\nApplied. Verifying ...")
    for table, column, _m, _s in NEW_COLUMNS:
        have = await columns_of(db, table, sqlserver)
        if have and column not in have:
            print(f"  FAILED: {table}.{column} is still missing")
            await connections.close_all()
            return 1
    have = await columns_of(db, "warehouse_purchase_orders", sqlserver)
    if sqlserver and not have.get("raised_by_id", True):
        print("  FAILED: warehouse_purchase_orders.raised_by_id is still NOT NULL")
        await connections.close_all()
        return 1
    print("  every column is present and raised_by_id allows null.")
    await connections.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
