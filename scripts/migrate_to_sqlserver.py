"""Move head office from SQLite to SQL Server, table by table, and prove every row arrived.

    python scripts/migrate_to_sqlserver.py                     what it would move
    python scripts/migrate_to_sqlserver.py --yes                move it
    python scripts/migrate_to_sqlserver.py --yes --only products,branch_sales

Reads the SQLite file directly rather than through the ORM: 16 million rows do not need to become 16 million Python
objects on the way past, and the schema on the other side was built from the same models so the columns already
match.

HOW THE TYPES ARE HANDLED, WHICH IS THE WHOLE JOB
-------------------------------------------------
Tortoise on SQLite keeps a Decimal as VARCHAR, a date as text, and a boolean as 0/1 in an INTEGER. SQL Server wants
DECIMAL, DATE, DATETIME2 and BIT. So every value is converted according to **the type of the column it is going
into**, read from SQL Server's own catalogue rather than guessed from the value: a column holding "0" is a bit if
the target says bit and a string if it says varchar, and only the target knows which.

A VALUE THIS CANNOT READ STOPS ITS ROW. IT IS NEVER QUIETLY NULLED
------------------------------------------------------------------
An earlier version of this script turned anything it could not parse into NULL, and that cost a whole run. Tortoise
writes a timestamp as `2026-09-21 09:38:44.094590+00:00` when it has microseconds and `2024-11-29 00:00:00+00:00`
when it does not. The parser truncated to 26 characters, which happens to remove the offset from the first shape and
not from the second, so every second-precision timestamp in the database became NULL and said nothing. 831 of 3,782
sampled values were that shape.

So the rule now is: an empty string is NULL, because empty means absent. Anything else that cannot be read **raises**,
which drops that one row, names the column in the report, and makes its table arrive short. A short table is a
finding somebody reads. A nulled column is not.

Offsets are resolved rather than removed: a stamp is shifted to UTC and stored naive, because the target columns are
`datetime2` and have nowhere to keep an offset. Everything in this database is written `+00:00`, so in practice
nothing shifts, but a non-zero offset would be handled instead of silently misread by six hours.

WHY THE PLAIN INDEXES COME OFF FIRST
------------------------------------
93 tables carry 275 indexes. Maintaining the non-key ones through 16 million inserts is most of the cost, so they are
scripted out, dropped, and rebuilt at the end. Primary keys and everything enforcing uniqueness stay: they are what
the foreign keys point at, and dropping them would need the foreign keys dropped too, which trades a slow load for a
fragile one. The generated DDL is written to reports/mssql-indexes.sql before anything is dropped, so a crash mid-load
leaves the statements on disk rather than only in this process.

CONSTRAINTS ARE OFF WHILE IT LOADS, AND CHECKED AFTERWARDS
----------------------------------------------------------
Loading 93 tables in dependency order is possible but brittle: one missed edge and a table fails for a reason that
looks like corruption. Instead every foreign key is switched off, the data goes in, and then they are switched back
on **WITH CHECK**, which makes SQL Server verify every existing row. If anything is wrong it says so then, against
the whole loaded set, rather than one row at a time.

WHAT IT PROVES
--------------
Row counts per table on both sides, and the exit code is the verdict. A table that arrives short is a finding, not
a warning.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"
SQLITE_DB = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\fresh-test\headoffice\db\cloud.db")
REPORTS = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\ops\legacy-import\reports")

# Tables that belong to the tool, not the application. `aerich` is the migration ledger of the SQLite database and
# means nothing on the other side.
SKIP = {"aerich"}

TRUE = {"1", "true", "t", "yes", "y"}

# A date, an optional time with optional fractional seconds, and an optional offset. Written out rather than handed
# to `fromisoformat` because 3.10's version rejects both `Z` and a six-digit fraction, and this has to read whatever
# is in the file.
STAMP = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"
    r"(?:[ T](?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?(?:\.(?P<frac>\d{1,9}))?)?"
    r"\s*(?P<zone>Z|z|[+-]\d{2}:?\d{2})?$")


class Unreadable(ValueError):
    """A value this script does not understand. Raised rather than returned as NULL, so it reaches the report."""


def read_stamp(text: str) -> datetime:
    """Text to a naive UTC datetime, or raise.

    The offset is applied and then dropped, because the target columns are `datetime2`. `Z` and `+00:00` mean the
    same thing and both appear in exports.
    """
    found = STAMP.match(text)
    if not found:
        raise Unreadable(f"not a date or time: {text!r}")
    part = found.groupdict()
    micro = int((part["frac"] or "0").ljust(6, "0")[:6])
    moment = datetime(int(part["y"]), int(part["m"]), int(part["d"]),
                      int(part["H"] or 0), int(part["M"] or 0), int(part["S"] or 0), micro)
    zone = part["zone"]
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        hours, _, minutes = zone[1:].partition(":")
        if not minutes:  # +0500 as well as +05:00
            hours, minutes = zone[1:3], zone[3:5]
        moment -= sign * timedelta(hours=int(hours), minutes=int(minutes or 0))
    return moment


def converter_for(kind: str):
    """The conversion for one column, chosen once from the target's type rather than per value.

    Returning a function per column instead of comparing type names 330 million times is most of the difference
    between a load that finishes overnight and one that does not.
    """
    kind = kind.lower()

    if kind == "bit":
        def as_bit(value):
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return 1 if value else 0
            return 1 if str(value).strip().lower() in TRUE else 0
        return as_bit

    if kind in ("decimal", "numeric", "money", "smallmoney", "float", "real"):
        def as_number(value):
            if value is None:
                return None
            if isinstance(value, (int, float, Decimal)):
                return value
            text = str(value).strip()
            if not text:
                return None
            try:
                return Decimal(text)
            except (InvalidOperation, ValueError) as exc:
                raise Unreadable(f"not a number: {text!r}") from exc
        return as_number

    if kind in ("int", "bigint", "smallint", "tinyint"):
        def as_whole(value):
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return int(value)
            text = str(value).strip()
            if not text:
                return None
            try:
                return int(float(text))
            except ValueError as exc:
                raise Unreadable(f"not a whole number: {text!r}") from exc
        return as_whole

    if kind == "date":
        def as_date(value):
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            text = str(value).strip()
            return read_stamp(text).date() if text else None
        return as_date

    if kind in ("datetime", "datetime2", "smalldatetime", "datetimeoffset"):
        def as_stamp(value):
            if value is None:
                return None
            if isinstance(value, datetime):
                return value
            if isinstance(value, date):
                return datetime(value.year, value.month, value.day)
            text = str(value).strip()
            return read_stamp(text) if text else None
        return as_stamp

    if kind == "time":
        def as_time(value):
            if value is None:
                return None
            text = str(value).strip()
            if not text:
                return None
            head = text if ":" in text.split(" ")[-1] else text
            try:
                return read_stamp(f"1900-01-01 {head}").time()
            except Unreadable:
                return read_stamp(head).time()
        return as_time

    def as_text(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        return value if isinstance(value, str) else str(value)
    return as_text


def convert(value, kind: str):
    """One value, for the type of the column it is going into. Kept for callers that want a single conversion; the
    load itself uses `converter_for` so the type name is read once per column and not once per value."""
    return converter_for(kind)(value)


def target_dsn() -> str:
    found = re.findall(r"mssql://(\S+)", CREDENTIALS.read_text(encoding="utf-8"))
    if not found:
        raise SystemExit("No mssql:// line in the credentials file. Run scripts/setup_sqlserver.py first.")
    url = found[-1].strip().split("?")[0]
    user, _, rest = url.partition(":")
    secret, _, hostpart = rest.rpartition("@")
    host, _, database = hostpart.partition("/")
    server, _, port = host.partition(":")
    return (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:{server},{port or 1433};"
            f"DATABASE={database};UID={user};PWD={secret};Encrypt=no;TrustServerCertificate=yes")


def plain_indexes(cur) -> list[tuple[str, str, str]]:
    """Every nonclustered index that is not a primary key and enforces nothing: table, name, and the DDL to put it
    back. Unique indexes and unique constraints are left alone because the foreign keys reference them."""
    cur.execute("""
        SELECT t.name AS tbl, i.name AS ix, i.object_id, i.index_id, i.filter_definition
          FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id
         WHERE i.type_desc = 'NONCLUSTERED' AND i.is_primary_key = 0
           AND i.is_unique_constraint = 0 AND i.is_unique = 0 AND i.name IS NOT NULL
         ORDER BY t.name, i.name
    """)
    found = cur.fetchall()
    out = []
    for tbl, ix, object_id, index_id, where in found:
        cur.execute("""
            SELECT c.name, ic.is_descending_key, ic.is_included_column
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
             WHERE ic.object_id = ? AND ic.index_id = ?
             ORDER BY ic.is_included_column, ic.key_ordinal
        """, object_id, index_id)
        keys, included = [], []
        for name, descending, is_included in cur.fetchall():
            piece = f"[{name}]" + (" DESC" if descending else "")
            (included if is_included else keys).append(f"[{name}]" if is_included else piece)
        if not keys:
            continue
        ddl = f"CREATE NONCLUSTERED INDEX [{ix}] ON [{tbl}] ({', '.join(keys)})"
        if included:
            ddl += f" INCLUDE ({', '.join(included)})"
        if where:
            ddl += f" WHERE {where}"
        out.append((tbl, ix, ddl))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--only", help="just these tables, comma separated")
    ap.add_argument("--batch", type=int, default=5000)
    # One batch, one transaction. Grouping batches is faster until a batch fails, and then the rollback that clears
    # the failure also discards the batches before it, which have already been counted: the run would report a
    # complete table that is missing rows. Correct beats slightly faster, and a commit per 5,000 rows is cheap.
    ap.add_argument("--commit-every", type=int, default=1, help="batches per commit")
    ap.add_argument("--keep-indexes", action="store_true", help="do not drop and rebuild the plain indexes")
    ap.add_argument("--source", default=str(SQLITE_DB))
    args = ap.parse_args()

    try:
        import pyodbc
    except ImportError:
        print("This needs pyodbc:  .venv\\Scripts\\python.exe -m pip install pyodbc")
        return 2

    source_path = pathlib.Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"No SQLite database at {source_path}")
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)

    dst = pyodbc.connect(target_dsn(), timeout=60, autocommit=False)
    dcur = dst.cursor()

    # What the target actually has, and the type of every column in it.
    dcur.execute("""
        SELECT t.name AS tbl, c.name AS col, ty.name AS kind
          FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
          JOIN sys.types ty ON ty.user_type_id = c.user_type_id
         ORDER BY t.name, c.column_id
    """)
    target: dict[str, dict[str, str]] = {}
    for tbl, col, kind in dcur.fetchall():
        target.setdefault(tbl, {})[col] = kind
    if not target:
        raise SystemExit("The target has no tables. Run scripts/sqlserver_schema.py --build first.")

    # Tables whose key SQL Server numbers itself. Their rows already have ids that the rest of the database points
    # at, so the ids have to be carried over rather than reissued, and SQL Server only allows that while
    # IDENTITY_INSERT is on for that one table. Without it every row is refused: loyalty_settings lost its single
    # row that way, and branch_promotion_stats would have lost all of them the first time it held any.
    dcur.execute("""
        SELECT t.name, c.name FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
         WHERE c.is_identity = 1
    """)
    identity: dict[str, str] = {tbl: col for tbl, col in dcur.fetchall()}

    here = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    wanted = [t for t in here if t not in SKIP and t in target]
    missing = [t for t in here if t not in SKIP and t not in target]
    if args.only:
        asked = {x.strip() for x in args.only.split(",") if x.strip()}
        wanted = [t for t in wanted if t in asked]

    plan = []
    total = 0
    for table in wanted:
        n = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if n:
            plan.append((table, n))
            total += n
    # Biggest last is the wrong order to watch: put the small tables first so a type problem shows in the first
    # minute rather than the fourth hour. Alphabetical does that by accident here and is also reproducible.
    plan.sort(key=lambda p: p[1])

    print(f"   from  {source_path}  ({source_path.stat().st_size/1e9:.2f} GB)")
    print(f"   to    {target_dsn().split('DATABASE=')[1].split(';')[0]} on SQL Server")
    print(f"   {len(plan)} table(s) with rows, {total:,} rows in all", flush=True)
    if missing:
        print(f"   {len(missing)} table(s) in SQLite with no counterpart, skipped: {', '.join(missing[:6])}")
    print()
    for table, n in sorted(plan, key=lambda p: -p[1])[:12]:
        print(f"      {table:<34} {n:>12,}")
    if len(plan) > 12:
        print(f"      ... and {len(plan) - 12} more")

    if not args.yes:
        print("\n   Nothing moved. Add --yes.")
        return 0

    database = target_dsn().split("DATABASE=")[1].split(";")[0]

    # The plain indexes, scripted to disk before they are dropped.
    rebuild: list[tuple[str, str, str]] = []
    script = REPORTS / "mssql-indexes.sql"
    if not args.keep_indexes:
        rebuild = plain_indexes(dcur)
        if rebuild:
            REPORTS.mkdir(parents=True, exist_ok=True)
            script.write_text(
                "-- Written by scripts/migrate_to_sqlserver.py before dropping these for the load.\n"
                "-- If the load died before rebuilding them, run this file, or run the migration again: it reads\n"
                "-- this file back when the database has none of these indexes left to script.\n\n"
                + ";\n".join(ddl for _t, _i, ddl in rebuild) + ";\n", encoding="utf-8")
            print(f"\n   {len(rebuild)} plain index(es) scripted to {script.name}, dropping them for the load ...",
                  flush=True)
            for tbl, ix, _ddl in rebuild:
                dcur.execute(f"DROP INDEX [{ix}] ON [{tbl}]")
            dst.commit()
        elif script.exists():
            # A previous run dropped them and did not get to the rebuild. Take the list from what it wrote, so the
            # indexes come back at the end of this run instead of staying missing without anybody noticing.
            #
            # The comment lines are removed line by line, not by skipping a chunk that starts with one. The file's
            # header sits immediately above the first CREATE with no semicolon between them, so treating the chunk
            # as a comment silently dropped the first index: 20 of 21 came back and the count was the only sign.
            statements = []
            for chunk in script.read_text(encoding="utf-8").split(";"):
                body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--")).strip()
                if body:
                    statements.append(body)
            for ddl in statements:
                found = re.search(r"INDEX \[(\w+)\] ON \[(\w+)\]", ddl)
                if found:
                    rebuild.append((found.group(2), found.group(1), ddl))
            print(f"\n   the database has no plain indexes to drop; {len(rebuild)} from {script.name} will be"
                  f" rebuilt at the end (a previous run dropped them and stopped).", flush=True)

    print("   switching every foreign key off for the load ...", flush=True)
    dcur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT ALL'")
    dst.commit()

    # Simple recovery while loading: a bulk load into a fully logged database spends most of its time growing the
    # transaction log. Put back at the end, because a head office database in production is not run this way.
    recovery_changed = False
    try:
        dcur.execute(f"ALTER DATABASE [{database}] SET RECOVERY SIMPLE")
        dst.commit()
        recovery_changed = True
        print("   recovery model set to SIMPLE for the load.", flush=True)
    except Exception as exc:  # noqa: BLE001: it only makes it slower, so not a reason to stop
        print(f"   could not set SIMPLE recovery, carrying on: {str(exc)[:90]}", flush=True)

    started = time.monotonic()
    moved: dict[str, int] = {}
    problems: list[tuple[str, str]] = []
    done_rows = 0
    for n, (table, expected) in enumerate(plan, 1):
        columns = [c for c in (r[1] for r in src.execute(f'PRAGMA table_info("{table}")')) if c in target[table]]
        funcs = [converter_for(target[table][c]) for c in columns]
        marks = ", ".join("?" * len(columns))
        names = ", ".join(f"[{c}]" for c in columns)
        insert = f"INSERT INTO [{table}] ({names}) VALUES ({marks})"
        dcur.execute(f"DELETE FROM [{table}]")
        dst.commit()
        carries_own_id = table in identity and identity[table] in columns
        if carries_own_id:
            dcur.execute(f"SET IDENTITY_INSERT [{table}] ON")
        dcur.fast_executemany = True
        read = src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in columns)} FROM "{table}"')
        count = 0
        since_commit = 0
        table_started = time.monotonic()
        while True:
            batch = read.fetchmany(args.batch)
            if not batch:
                break
            rows = []
            for row in batch:
                try:
                    rows.append([f(v) for f, v in zip(funcs, row)])
                except Unreadable as exc:
                    problems.append((table, f"a row refused: {exc}"[:170]))
            try:
                dcur.executemany(insert, rows)
                count += len(rows)
            except Exception as outer:  # noqa: BLE001: one bad row must cost one row, not the batch
                # A failed executemany leaves the transaction and the cursor in a state where every following
                # statement fails too, whatever it contains. An earlier run lost 5,000 good rows of
                # item_list_entries that way: one refusal, then 4,999 refusals that were only its echo. So the
                # transaction is rolled back and the cursor thrown away before the batch is retried, and each row
                # is committed on its own so nothing downstream can inherit a failure.
                dst.rollback()
                try:
                    dcur.close()
                except Exception:  # noqa: BLE001
                    pass
                dcur = dst.cursor()
                first = re.sub(r"\s+", " ", str(outer))[:170]
                kept = 0
                for row in rows:
                    try:
                        dcur.execute(insert, row)
                        dst.commit()
                        kept += 1
                    except Exception as inner:  # noqa: BLE001
                        dst.rollback()
                        if len(problems) < 400:
                            problems.append((table, re.sub(r"\s+", " ", str(inner))[:170]))
                count += kept
                if kept == len(rows):
                    # Every row went in on its own, so the batch failure was about the batch and not the data.
                    problems.append((table, f"a batch was refused but all {kept} rows loaded singly: {first}"))
                dcur.fast_executemany = True
                since_commit = 0
                continue
            since_commit += 1
            if since_commit >= args.commit_every:
                dst.commit()
                since_commit = 0
        dst.commit()
        if carries_own_id:
            # Only one table at a time may have it on, so it goes off before the next table needs it.
            dcur.execute(f"SET IDENTITY_INSERT [{table}] OFF")
            dst.commit()
        moved[table] = count
        done_rows += count
        took = max(time.monotonic() - table_started, 0.001)
        elapsed = time.monotonic() - started
        rate = done_rows / max(elapsed, 0.001)
        left = (total - done_rows) / rate if rate else 0
        mark = "" if count == expected else f"   SHORT by {expected - count:,}"
        print(f"   [{n:>2}/{len(plan)}] {table:<34} {count:>12,}  {count/took:>7,.0f}/s"
              f"   {done_rows*100/total:>5.1f}%  about {left/60:>5.0f} min left{mark}", flush=True)

    if rebuild:
        print(f"\n   rebuilding {len(rebuild)} index(es) ...", flush=True)
        for at, (tbl, ix, ddl) in enumerate(rebuild, 1):
            try:
                dcur.execute(ddl)
                dst.commit()
            except Exception as exc:  # noqa: BLE001
                problems.append((f"(index {ix})", re.sub(r"\s+", " ", str(exc))[:170]))
            if at % 25 == 0 or at == len(rebuild):
                print(f"      {at}/{len(rebuild)}", flush=True)

    print("\n   switching the foreign keys back on, WITH CHECK, so every row is verified ...", flush=True)
    try:
        dcur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? WITH CHECK CHECK CONSTRAINT ALL'")
        dst.commit()
        print("   every foreign key holds.")
    except Exception as exc:  # noqa: BLE001
        problems.append(("(foreign keys)", re.sub(r"\s+", " ", str(exc))[:220]))
        print(f"   A FOREIGN KEY DOES NOT HOLD: {str(exc)[:200]}")

    if recovery_changed:
        try:
            dcur.execute(f"ALTER DATABASE [{database}] SET RECOVERY FULL")
            dst.commit()
            print("   recovery model back to FULL.")
        except Exception as exc:  # noqa: BLE001
            problems.append(("(recovery model)", str(exc)[:150]))

    # The covering indexes the models cannot express, and statistics. Both belong to the load rather than after it:
    # a freshly loaded database whose statistics predate its data plans every query on guesses, and the dashboard's
    # year view took 41 seconds without the covering index and 11 with it. Leaving these to be remembered separately
    # means the first person to open the new database decides it is slower than the old one, and they are right.
    try:
        dst.autocommit = True
        from sqlserver_tune import apply as tune

        print("\n   covering indexes and statistics ...", flush=True)
        summary = tune(dcur, say=lambda line: print(line, flush=True))
        for line in summary["skipped"]:
            print(f"      {line}")
        dst.autocommit = False
    except Exception as exc:  # noqa: BLE001: the data is loaded and verified; this only makes it quick
        print(f"   could not apply the covering indexes or statistics: {str(exc)[:160]}")
        problems.append(("(tuning)", re.sub(r"\s+", " ", str(exc))[:170]))

    print(f"\n{'=' * 96}")
    short = [(t, e, moved.get(t, 0)) for t, e in plan if moved.get(t, 0) != e]
    print(f"   {sum(moved.values()):,} rows moved in {time.monotonic() - started:,.0f}s")
    if short:
        print(f"   {len(short)} table(s) arrived short:")
        for table, expected, got in short[:12]:
            print(f"      {table:<34} expected {expected:,}, got {got:,}")
    seen = set()
    for table, message in problems:
        if (table, message[:60]) in seen:
            continue
        seen.add((table, message[:60]))
        print(f"      {table}: {message}")
        if len(seen) >= 12:
            break
    src.close()
    dst.close()
    if short or problems:
        print("\n   NOT CLEAN. Nothing on the SQLite side was touched, so this can be run again.")
        return 1
    print("\n   Every table matched, and every foreign key was verified against the loaded rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
