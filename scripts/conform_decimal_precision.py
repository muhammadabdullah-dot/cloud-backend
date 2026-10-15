"""Find numbers SQLite is holding that the models say cannot exist, and record them as the field's own default.

    python scripts/conform_decimal_precision.py            what it would change
    python scripts/conform_decimal_precision.py --yes       change it

WHY THIS IS NEEDED AT ALL
-------------------------
`fields.DecimalField(max_digits=6, decimal_places=2)` is a promise that the number has at most four digits in front
of the point. SQLite does not enforce it: it stores whatever it is given and the declared type is a hint. So the
database can hold, and has held, a value the application's own schema forbids, and nothing said so for months.

SQL Server does enforce it, which is how this was found: one GRN line out of 282,522 carries a tax percentage of
12,727.27, straight from Multi-Tec's own `GstPerc` column, and DECIMAL(6,2) refuses it. The whole line was being
dropped, worth Rs 56.44 of real goods received.

WHAT IT DOES, AND WHY NOT SOMETHING CLEVERER
--------------------------------------------
The field is NOT NULL with a default of 0, so "unknown" is not available to store. The choices were to widen the
model for every tax rate everywhere because one legacy row is nonsense, to drop a real line, or to record the
impossible percentage as the default and keep the line. It records it as the default:

  - `tax_amount` is a separate DECIMAL(14,2) and keeps the money, so no figure that reconciliation checks changes.
  - A line showing tax_amount 56 against a rate of 0 is visibly odd, which is better than a line that vanished.
  - Every change is listed by table, column, row id and old value, so the decision can be reversed knowingly.

It deliberately does not derive a replacement from the other columns. That line reads qty 2 at 154.72 with a 99.86%
discount and a total of 56.44: their own numbers do not agree with each other, so anything computed from them would
be invention dressed up as arithmetic.

The precision to check against comes from the models, not from SQL Server, so this is worth running whether or not
the other engine is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tortoise import Tortoise, fields
from tortoise.transactions import in_transaction


def declared_decimals() -> list[tuple[str, str, int, int, object]]:
    """Every decimal field in every model: table, column, digits, places, and the field's default."""
    out = []
    for app_models in Tortoise.apps.values():
        for model in app_models.values():
            table = model._meta.db_table
            for name, field in model._meta.fields_map.items():
                if isinstance(field, fields.DecimalField):
                    column = getattr(field, "source_field", None) or name
                    out.append((table, column, field.max_digits, field.decimal_places, field.default))
    return sorted(set(out))


def whole_digits(text: str) -> int | None:
    try:
        parts = Decimal(str(text)).as_tuple()
    except (InvalidOperation, ValueError):
        return None
    return len(parts.digits) + parts.exponent


async def run(args) -> int:
    from app.core.config import TORTOISE_ORM

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        db = Tortoise.get_connection("default")
        engine = db.schema_generator.DIALECT
        print(f"   engine {engine}")
        if engine != "sqlite":
            print("   This is for the SQLite file, which is the one that does not enforce precision.")
            print("   Point DB_URL at cloud.db and run it there.")
            return 2

        have = {r["name"] for r in await db.execute_query_dict(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        findings: list[tuple[str, str, str, str, object]] = []
        checked = 0
        for table, column, digits, places, default in declared_decimals():
            if table not in have:
                continue
            room = digits - places
            checked += 1
            rows = await db.execute_query_dict(
                f'SELECT "id", "{column}" AS v FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND "{column}" <> \'\' '
                f'AND ABS(CAST("{column}" AS REAL)) >= ?', [10 ** room])
            for row in rows:
                if (whole_digits(row["v"]) or 0) > room:
                    findings.append((table, column, str(row["id"]), str(row["v"]),
                                     default if default is not None else 0))

        print(f"   {checked} decimal column(s) checked against what the models declare\n")
        if not findings:
            print("   Every number fits the precision its model declares. Nothing to change.")
            return 0
        print(f"   {len(findings)} value(s) the models say cannot exist:\n")
        print(f"   {'table':<26} {'column':<16} {'holds':>14}  {'would become':>13}   row")
        for table, column, row_id, value, default in findings:
            print(f"   {table:<26} {column:<16} {value:>14}  {str(default):>13}   {row_id}")

        if not args.yes:
            print("\n   Nothing changed. Add --yes.")
            return 0

        async with in_transaction() as tx:
            for table, column, row_id, _value, default in findings:
                await tx.execute_query(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE "id" = ?', [str(default), row_id])
        print(f"\n   {len(findings)} value(s) recorded as the field default. Every other column on those rows,")
        print("   including the amounts reconciliation checks, is untouched.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
