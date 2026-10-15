"""Prove the SQL Server copy holds the same data as the SQLite file, column by column.

    python scripts/compare_engines.py                  everything
    python scripts/compare_engines.py --only branch_sales,branch_sale_lines
    python scripts/compare_engines.py --samples 40     also compare 40 whole rows per table, field by field

The exit code is the verdict.

WHY ROW COUNTS ARE NOT A VERIFICATION
-------------------------------------
The migration already checks that each table has the same number of rows on both sides, and that check passed while
5,820,596 timestamps were being silently turned into NULL. Every row arrived; every row was wrong. A count cannot see
inside a row, so this compares what is in them:

  rows            COUNT(*) per table, which is the check the migration already makes, repeated here independently
  filled          COUNT(column) per column: how many rows have a value rather than NULL. This is the check that
                  catches a conversion returning None, which is the fault that cost a whole run.
  totals          SUM per numeric column, so a decimal that lost its scale or a boolean read the wrong way shows up
                  as a different number rather than as the same count of different values
  earliest,       MIN and MAX per date column, which catches a stamp that was truncated, shifted by an offset, or
  latest          parsed with the month and day the wrong way round
  whole rows      with --samples, real rows fetched from both sides by id and compared field by field

WHAT IS COMPARED LOOSELY, AND WHY
---------------------------------
Numeric totals are compared to a relative tolerance of 1e-9. Both engines are adding the same values, but not
necessarily in the same order, and floating point addition is not associative, so demanding the same bits would fail
on arithmetic rather than on data.

Text columns are compared by how many are filled, not by their contents. The database collation is case insensitive
where SQLite is not, so MIN and MAX of a text column legitimately disagree, and a check that fires on that would cry
wolf on every table. Text contents are covered instead by the whole-row sampling, which compares exact strings.
"""
from __future__ import annotations

import argparse
import pathlib
import random
import re
import sqlite3
import sys
import time
from datetime import datetime

SQLITE_DB = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\fresh-test\headoffice\db\cloud.db")
CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"

SKIP = {"aerich"}
TOLERANCE = 1e-9

# Derived tables that `rollup_service.refresh` rebuilds from scratch, minting a new id for every row each time. Their
# contents are compared like everything else; only the whole-row sampling skips them, because it fetches rows by id
# and an id that is reissued on every rebuild identifies nothing. Reporting 36 "on one engine only" rows for a table
# whose count and every column already matched was noise standing where a real finding would go.
REBUILT = {"branch_product_months", "branch_product_first_sale"}

# Columns a running server writes on a timer, so the SQLite file moves on after the copy was taken and the two
# disagree by however long ago the load was. `accounts_scheduler` posts the books every five minutes and stamps
# these, which is why three runs of this script an hour apart reported the same one row differing, five minutes
# further on each time.
#
# They are reported as drift and not counted as findings, in either direction, because both databases now have a
# server attached: head office runs on 4205 against the SQLite file and on 4215 against SQL Server, each posting its
# own books, so whichever posted most recently is ahead. It was written to allow only SQLite ahead, which was true for
# an hour and then stopped being true the moment the second server started.
#
# What is still a finding is a value that is missing or unreadable on one side, because that is the conversion fault
# this whole script exists to catch. Only these two columns are excused, and only for being a different time.
LIVE_COLUMNS = {("acc_settings", "last_posting_at"), ("acc_settings", "updated_at")}

NUMERIC = {"decimal", "numeric", "money", "smallmoney", "float", "real", "int", "bigint", "smallint", "tinyint", "bit"}
STAMPS = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}


def target_dsn() -> str:
    found = re.findall(r"mssql://(\S+)", CREDENTIALS.read_text(encoding="utf-8"))
    if not found:
        raise SystemExit("No mssql:// line in the credentials file.")
    url = found[-1].strip().split("?")[0]
    user, _, rest = url.partition(":")
    secret, _, hostpart = rest.rpartition("@")
    host, _, database = hostpart.partition("/")
    server, _, port = host.partition(":")
    return (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:{server},{port or 1433};"
            f"DATABASE={database};UID={user};PWD={secret};Encrypt=no;TrustServerCertificate=yes")


def close_enough(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= TOLERANCE


def same_moment(a, b) -> bool:
    """Two stamps, one from each engine, as text. Compared to the second, because SQLite keeps an offset in the text
    and SQL Server keeps a naive datetime2, so the strings differ where the instants do not."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    def plain(v) -> str:
        text = str(v).replace("T", " ").strip()
        text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text).strip()
        if "." in text:
            text = text.split(".")[0]
        return text if " " in text else f"{text} 00:00:00"
    return plain(a) == plain(b)


def _both_are_times(lite, over) -> bool:
    """True when both sides hold a readable moment, so the only difference is which one is later.

    That is drift from a server writing on a timer. A value missing or unreadable on either side is not drift: it is
    the conversion failure this script was written to catch, and it stays a finding."""
    def when(value):
        if value is None:
            return None
        text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", str(value).replace("T", " ").strip()).strip()
        for shape in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, shape)
            except ValueError:
                continue
        return None
    return when(lite) is not None and when(over) is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="just these tables, comma separated")
    ap.add_argument("--samples", type=int, default=0, help="whole rows per table to compare field by field")
    ap.add_argument("--source", default=str(SQLITE_DB))
    args = ap.parse_args()

    import pyodbc

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    dst = pyodbc.connect(target_dsn(), timeout=120)
    d = dst.cursor()

    d.execute("""SELECT t.name, c.name, ty.name FROM sys.columns c
                   JOIN sys.tables t ON t.object_id = c.object_id
                   JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                  ORDER BY t.name, c.column_id""")
    target: dict[str, dict[str, str]] = {}
    for tbl, col, kind in d.fetchall():
        target.setdefault(tbl, {})[col] = kind.lower()

    here = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    tables = [t for t in here if t not in SKIP and t in target]
    if args.only:
        asked = {x.strip() for x in args.only.split(",") if x.strip()}
        tables = [t for t in tables if t in asked]

    print(f"   {args.source}")
    print(f"   against {target_dsn().split('DATABASE=')[1].split(';')[0]} on SQL Server")
    print(f"   {len(tables)} table(s)\n")

    findings: list[str] = []
    drift: list[str] = []
    notes: list[str] = []
    compared_columns = 0
    started = time.monotonic()

    for n, table in enumerate(tables, 1):
        columns = [c for c in (r[1] for r in src.execute(f'PRAGMA table_info("{table}")')) if c in target[table]]
        if not columns:
            continue
        numeric = [c for c in columns if target[table][c] in NUMERIC]
        stamps = [c for c in columns if target[table][c] in STAMPS]

        # One scan per engine per table: every measure in a single statement.
        lite_parts = ["COUNT(*)"]
        mssql_parts = ["COUNT(*)"]
        for c in columns:
            lite_parts.append(f'COUNT("{c}")')
            mssql_parts.append(f"COUNT([{c}])")
        for c in numeric:
            lite_parts.append(f'SUM(CAST("{c}" AS REAL))')
            mssql_parts.append(f"SUM(CAST([{c}] AS float))")
        for c in stamps:
            lite_parts += [f'MIN("{c}")', f'MAX("{c}")']
            mssql_parts += [f"MIN([{c}])", f"MAX([{c}])"]

        lite = src.execute(f'SELECT {", ".join(lite_parts)} FROM "{table}"').fetchone()
        d.execute(f"SELECT {', '.join(mssql_parts)} FROM [{table}]")
        over = d.fetchone()

        at = 0
        if lite[at] != over[at]:
            findings.append(f"{table}: {lite[at]:,} row(s) in SQLite, {over[at]:,} on SQL Server")
        row_count = lite[at]
        at += 1
        for c in columns:
            compared_columns += 1
            if lite[at] != over[at]:
                findings.append(
                    f"{table}.{c}: {lite[at]:,} row(s) hold a value in SQLite, {over[at]:,} on SQL Server"
                    f"   ({abs(lite[at]-over[at]):,} became NULL or stopped being NULL)")
            at += 1
        for c in numeric:
            if not close_enough(lite[at], over[at]):
                findings.append(f"{table}.{c}: total {lite[at]!r} in SQLite, {over[at]!r} on SQL Server")
            at += 1
        for c in stamps:
            for label in ("earliest", "latest"):
                if not same_moment(lite[at], over[at]):
                    line = f"{table}.{c}: {label} {lite[at]!r} in SQLite, {over[at]!r} on SQL Server"
                    if (table, c) in LIVE_COLUMNS and _both_are_times(lite[at], over[at]):
                        drift.append(line)
                    else:
                        findings.append(line)
                at += 1

        mark = "" if not findings or not findings[-1].startswith(table) else "   <- differs"
        print(f"   [{n:>2}/{len(tables)}] {table:<34} {row_count:>12,} row(s), "
              f"{len(columns):>2} column(s){mark}", flush=True)

    # Whole rows, fetched by id from both sides and compared field by field. This is what covers text contents.
    if args.samples:
        print(f"\n   comparing up to {args.samples} whole row(s) per table, field by field ...", flush=True)
        rng = random.Random(20260930)
        rows_checked = 0
        for table in tables:
            columns = [c for c in (r[1] for r in src.execute(f'PRAGMA table_info("{table}")')) if c in target[table]]
            if table in REBUILT:
                notes.append(f"{table}: rebuilt from scratch, so its ids are reissued; columns compared, rows not")
                continue
            if "id" not in columns:
                notes.append(f"{table}: no id column, not sampled")
                continue
            ids = [r[0] for r in src.execute(f'SELECT "id" FROM "{table}"')]
            if not ids:
                continue
            picked = rng.sample(ids, min(args.samples, len(ids)))
            picks_lite = ", ".join(chr(34) + c + chr(34) for c in columns)
            picks_over = ", ".join(f"[{c}]" for c in columns)
            marks = ", ".join("?" * len(picked))
            lite_rows = {r[columns.index("id")]: r for r in src.execute(
                f'SELECT {picks_lite} FROM "{table}" WHERE "id" IN ({marks})', picked)}
            d.execute(f"SELECT {picks_over} FROM [{table}] WHERE [id] IN ({marks})", *picked)
            over_rows = {r[columns.index("id")]: r for r in d.fetchall()}
            for row_id in picked:
                a, b = lite_rows.get(row_id), over_rows.get(row_id)
                if a is None or b is None:
                    findings.append(f"{table}: row {row_id} is on one engine only")
                    continue
                rows_checked += 1
                for c, x, y in zip(columns, a, b):
                    kind = target[table][c]
                    if kind in NUMERIC:
                        ok = close_enough(None if x in (None, "") else x, None if y is None else y) \
                            if not (x is None and y is None) else True
                    elif kind in STAMPS:
                        ok = same_moment(x, y)
                    else:
                        ok = (x or None) == (str(y) if y is not None else None) or \
                             (x is None and y is None) or str(x) == str(y)
                    if not ok:
                        findings.append(f"{table}.{c} on row {row_id}: {x!r} in SQLite, {y!r} on SQL Server")
        print(f"   {rows_checked:,} whole row(s) compared.")

    print(f"\n{'=' * 100}")
    print(f"   {len(tables)} table(s) and {compared_columns:,} column(s) compared in "
          f"{time.monotonic() - started:,.0f}s")
    for note in notes[:6]:
        print(f"      note: {note}")
    if drift:
        print(f"\n   {len(drift)} column(s) that a running server stamps on a timer, so the two sides hold different")
        print("   moments rather than different data. Both values are readable, which is what is being checked:")
        for line in drift:
            print(f"      {line}")
    if findings:
        # Every one of them. An earlier run printed the first 40 and said "and 14 more", which is a verification
        # tool hiding exactly the part nobody has read yet.
        print(f"\n   {len(findings)} DIFFERENCE(S):")
        for line in findings:
            print(f"      {line}")
        src.close()
        dst.close()
        return 1
    print("\n   No difference. Every table has the same rows, every column the same number of values,")
    print("   every number the same total, and every date the same first and last.")
    src.close()
    dst.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
