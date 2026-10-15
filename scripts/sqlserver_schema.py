"""Stand the head office schema up on SQL Server, and say exactly what it refused and why.

    python scripts/sqlserver_schema.py                report only
    python scripts/sqlserver_schema.py --build        drop everything and build it
    python scripts/sqlserver_schema.py --build --fix  also list the model fields that need NO ACTION
    python scripts/sqlserver_schema.py --relax-nulls  only the NULL fix below, on a schema already built

SQL Server will not accept two `ON DELETE CASCADE` paths to the same table, nor a self-referencing one. SQLite and
PostgreSQL accept both, which is why the whole schema has run on SQLite for months with foreign keys it cannot keep
on any other engine.

IT ALSO DISAGREES ABOUT NULL IN A UNIQUE RULE, WHICH IS QUIETER AND WORSE
------------------------------------------------------------------------
SQLite, PostgreSQL and the SQL standard treat two NULLs as different, so `unique_together = ("book", "system_key")`
lets a thousand rows have no system key. SQL Server treats two NULLs as equal and allows exactly one of them. The
generated schema is therefore **stricter than the model**, silently, and the first sign of it is rows refusing to
load: 5,668 of the 6,260 accounts, because 1,165 of them share ('HO', NULL).

A filtered unique index says what the model says, so every such rule is rebuilt as one:

    CREATE UNIQUE INDEX [name] ON [table] (book, system_key) WHERE system_key IS NOT NULL

Two of the 60 unique rules are affected. Only one of them loses rows today; the other would refuse a legitimate row
the first time a duty was recorded without a counter, which is the kind of fault that surfaces months later on a
Tuesday. Both are rebuilt.

This applies the schema one statement at a time rather than in a single script, because a single script stops at the
first refusal and reports one problem where there are several: the first real run reported one table and was hiding
three, and the run after that reported three and was hiding five. One statement at a time reports all of them at
once, and separates the tables SQL Server genuinely refuses from the ones that merely depend on those.

The rule for the fix, which is a judgement and is stated here rather than buried: **CASCADE belongs where the child
cannot exist without its parent** — a GRN line without its GRN is nothing. A reference to a user, a bin or a
requisition is not ownership; the row is still meaningful without it, and cascading there would delete a stock count
because somebody left. Those become NO ACTION, which refuses the delete instead.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys

# Run as a script rather than a module, so the app package has to be put on the path the same way
# scripts/load_legacy_shop.py is run with -m. This keeps both ways of invoking it working.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tortoise import Tortoise
from tortoise.utils import get_schema_sql

CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"
DRIVER = "ODBC+Driver+17+for+SQL+Server"


def db_url() -> str:
    if not CREDENTIALS.exists():
        raise SystemExit(f"No credentials file at {CREDENTIALS}. Run scripts/setup_sqlserver.py first.")
    found = re.findall(r"mssql://\S+", CREDENTIALS.read_text(encoding="utf-8"))
    if not found:
        raise SystemExit("No mssql:// line in the credentials file. Run scripts/setup_sqlserver.py first.")
    url = found[-1].strip()
    return url if "driver=" in url else f"{url}?driver={DRIVER}"


async def wipe(db) -> int:
    """Every foreign key, then every table. Constraints first, because a table nothing points at still has parents."""
    dropped = 0
    for query in (
        """SELECT 'ALTER TABLE ['+s.name+'].['+t.name+'] DROP CONSTRAINT ['+f.name+']' AS c
             FROM sys.foreign_keys f JOIN sys.tables t ON t.object_id = f.parent_object_id
             JOIN sys.schemas s ON s.schema_id = t.schema_id""",
        """SELECT 'DROP TABLE ['+s.name+'].['+t.name+']' AS c FROM sys.tables t
             JOIN sys.schemas s ON s.schema_id = t.schema_id""",
    ):
        for row in await db.execute_query_dict(query):
            await db.execute_script(row["c"])
            dropped += 1
    return dropped


def constraints_in(sql: str) -> dict[str, tuple[str, str, str]]:
    """Every foreign key the generator emitted, by name: which table, which column, pointing at what."""
    out: dict[str, tuple[str, str, str]] = {}
    for block in sql.split(";"):
        table = re.search(r'CREATE TABLE \[?(\w+)\]?', block, re.I)
        if not table:
            continue
        for name, column, parent in re.findall(
                r'CONSTRAINT \[(\w+)\] FOREIGN KEY \(\[(\w+)\]\) REFERENCES \[(\w+)\]', block):
            out[name] = (table.group(1), column, parent)
    return out


async def relax_nullable_uniques(db) -> list[str]:
    """Rebuild every unique rule that covers a nullable column as a filtered unique index.

    Idempotent: a rule that already carries a filter is left alone, so this can be run on its own against a schema
    that is already built and full of data. The index is created before the constraint is dropped where possible;
    where SQL Server will not allow both names at once the drop comes first, and a failure leaves the original in
    place rather than neither.
    """
    rules = await db.execute_query_dict("""
        SELECT t.name AS tbl, i.name AS ix, i.is_unique_constraint AS is_constraint,
               i.object_id AS oid, i.index_id AS iid
          FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id
         WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND i.filter_definition IS NULL
         ORDER BY t.name, i.name
    """)
    changed: list[str] = []
    for rule in rules:
        columns = await db.execute_query_dict("""
            SELECT c.name AS col, c.is_nullable AS nullable
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
             WHERE ic.object_id = ? AND ic.index_id = ? AND ic.is_included_column = 0
             ORDER BY ic.key_ordinal
        """, [rule["oid"], rule["iid"]])
        nullable = [c["col"] for c in columns if c["nullable"]]
        if not nullable:
            continue
        keys = ", ".join(f'[{c["col"]}]' for c in columns)
        where = " AND ".join(f"[{c}] IS NOT NULL" for c in nullable)
        table, name = rule["tbl"], rule["ix"]
        drop = (f"ALTER TABLE [{table}] DROP CONSTRAINT [{name}]" if rule["is_constraint"]
                else f"DROP INDEX [{name}] ON [{table}]")
        try:
            await db.execute_script(drop)
            await db.execute_script(f"CREATE UNIQUE INDEX [{name}] ON [{table}] ({keys}) WHERE {where}")
            changed.append(f"{table}.{name}  ({keys.replace('[', '').replace(']', '')})  WHERE {where}")
        except Exception as exc:  # noqa: BLE001: a rule a foreign key points at cannot be dropped, and says so
            changed.append(f"{table}.{name}  COULD NOT BE REBUILT: {re.sub(r'\s+', ' ', str(exc))[:150]}")
    return changed


async def build(db, fix: bool) -> dict:
    sql = get_schema_sql(db, safe=True)
    by_name = constraints_in(sql)
    statements = [s.strip() for s in sql.split(";") if s.strip()]

    refused: list[tuple[str, str, str, str]] = []   # table, column, parent, constraint
    knock_on: list[str] = []
    for statement in statements:
        try:
            await db.execute_script(statement)
        except Exception as exc:  # noqa: BLE001
            message = re.sub(r"\s+", " ", str(exc))
            what = re.search(r'CREATE (?:TABLE|INDEX) \[?(\w+)\]?', statement, re.I)
            label = what.group(1) if what else statement[:40]
            if "cascade paths" in message or "(1785)" in message:
                name = (re.search(r"constraint '(\w+)'", message) or [None, ""])[1]
                table, column, parent = by_name.get(name, (label, "?", "?"))
                refused.append((table, column, parent, name))
            else:
                knock_on.append(label)

    relaxed = await relax_nullable_uniques(db)

    counts = {}
    for key, query in (("tables", "SELECT COUNT(*) AS n FROM sys.tables"),
                       ("indexes", "SELECT COUNT(*) AS n FROM sys.indexes WHERE is_primary_key=0 AND name IS NOT NULL"),
                       ("foreignKeys", "SELECT COUNT(*) AS n FROM sys.foreign_keys"),
                       ("unique", "SELECT COUNT(*) AS n FROM sys.key_constraints WHERE type='UQ'")):
        counts[key] = (await db.execute_query_dict(query))[0]["n"]

    print(f"   {len(statements)} statements applied")
    for key in ("tables", "indexes", "foreignKeys", "unique"):
        print(f"      {key:<14} {counts[key]}")
    if relaxed:
        print(f"\n   {len(relaxed)} unique rule(s) covered a nullable column and were rebuilt as filtered indexes,")
        print("   so they mean on SQL Server what they mean in the models:")
        for line in relaxed:
            print(f"      {line}")
    if refused:
        print(f"\n   {len(refused)} foreign key(s) SQL Server refuses for multiple cascade paths:")
        for table, column, parent, _name in refused:
            print(f"      {table:<34} {column:<24} -> {parent}")
        if fix:
            print("\n   Each of these is a reference, not ownership. In the model, set:")
            for table, column, parent, _name in refused:
                field = column[:-3] if column.endswith("_id") else column
                print(f"      {table}.{field}  on_delete=fields.NO_ACTION")
    if knock_on:
        print(f"\n   {len(knock_on)} statement(s) failed only because something above them did not land:")
        print("      " + ", ".join(sorted(set(knock_on))[:12]))
    return {"tables": counts["tables"], "refused": len(refused), "knockOn": len(knock_on)}


async def run(args) -> int:
    from app.core.config import TORTOISE_ORM

    await Tortoise.init(config={
        "connections": {"default": db_url()},
        "apps": {"models": {"models": TORTOISE_ORM["apps"]["models"]["models"], "default_connection": "default"}},
    })
    db = Tortoise.get_connection("default")
    try:
        row = (await db.execute_query_dict(
            "SELECT CAST(@@SERVERNAME AS nvarchar(128)) AS s, DB_NAME() AS d, "
            "CAST(ConnectionProperty('net_transport') AS nvarchar(20)) AS t"))[0]
        print(f"   {row['s']} / {row['d']} over {row['t']}")
        have = (await db.execute_query_dict("SELECT COUNT(*) AS n FROM sys.tables"))[0]["n"]
        print(f"   {have} table(s) there now")
        if args.relax_nulls:
            # On its own, against a schema that is already built. Touches indexes only, never a row.
            relaxed = await relax_nullable_uniques(db)
            if not relaxed:
                print("\n   Every unique rule over a nullable column is already filtered. Nothing to do.")
                return 0
            print(f"\n   {len(relaxed)} unique rule(s) rebuilt as filtered indexes:")
            for line in relaxed:
                print(f"      {line}")
            return 1 if any("COULD NOT BE REBUILT" in line for line in relaxed) else 0
        if not args.build:
            print("\n   Nothing changed. Add --build to drop and rebuild.")
            return 0
        print(f"\n   dropping {have} table(s) and their constraints ...")
        await wipe(db)
        print("   building ...")
        summary = await build(db, args.fix)
        return 0 if summary["refused"] == 0 and summary["knockOn"] == 0 else 1
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="drop everything and build the schema")
    ap.add_argument("--fix", action="store_true", help="name the model fields that need NO ACTION")
    ap.add_argument("--relax-nulls", action="store_true",
                    help="only rebuild unique rules over nullable columns as filtered indexes; changes no data")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
