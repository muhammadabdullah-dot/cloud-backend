r"""Put back the time we threw away: `acc_vouchers.at`, from their ledger's own timestamps.

    set DB_URL=<scripts\print_db_url.py>
    python scripts/backfill_voucher_moment.py                what it would do
    python scripts/backfill_voucher_moment.py --yes            do it

WHAT THIS IS FOR
----------------
Every date range in the books answered "whole days only", and the reason written down was that a voucher is dated
and not timed "in their software and in ours". The second half was true. The first half was not:
`dbo.AccountLedger.Date` is a timestamp, and 5,340,870 of their 5,343,432 rows carry a real clock time, peaking
between 17:00 and 22:00 exactly as the trade does.

We dropped it at the column boundary. `load_legacy_ledger.a_date` keeps `str(value)[:10]`, the date part, because
`acc_vouchers.date` is a DateField. So this adds a nullable `at` beside it and fills it from the same rows the
vouchers were built from, without reloading 5.3 million lines to do it.

`date` is untouched and stays the field every report keys on. A voucher belongs to a business day; the day is what
a trial balance balances over. `at` is extra information, never a replacement, and it is null wherever the moment
was never recorded -- our own automatic postings, and anything typed here by hand.

HOW A VOUCHER IS MATCHED BACK TO ITS ROWS
-----------------------------------------
`load_legacy_ledger` groups the ledger by `(Type, VCode)` and writes `source = legacy:{book}:{TYPE}-{VCode}`, with
the book from `UseBranchSno`. That key is rebuilt here from the same staged table, so the match is the loader's own
arithmetic rather than a guess at it. Measured on the staged ledger, 14 October 2026:

    1,101,527 voucher groups, which is exactly the number of vouchers marked legacy
    2 groups carry more than one distinct timestamp (0.00%) -- those take the earliest
    0 groups span more than one branch, so the book is never ambiguous

WHY IT GOES THROUGH A TEMPORARY TABLE
--------------------------------------
A million single-row UPDATEs over ODBC is an afternoon. The pairs are bulk-inserted into `#voucher_moment` and
applied with one UPDATE ... FROM, which is one statement and one pass.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

HERE = pathlib.Path(__file__).resolve().parent
STAGING = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging"
# The restored backup first, then the live server's copy over the top of it: live wins, as everywhere else.
SOURCES = [STAGING / "source.db", STAGING / "live.db"]
CHUNK = 5_000


def say(line: str) -> None:
    print(line, flush=True)


def pairs_from_staging(sources: list[pathlib.Path]) -> list[tuple[str, str]]:
    """(source key, moment) for every voucher group in their ledger, built the way the loader builds it.

    Read in order, each overwriting the one before, so the LIVE server's copy wins over the restored backup. That
    is `fetch_live.py`'s rule and it is not a preference: they delete and re-date entries, so where the two
    disagree the live server is the one that is true now.

    It also matters here for plain coverage. The backup stops partway through September 2026, so filling from it
    alone left October entirely without moments -- 20,226 vouchers -- and September 13,115 short, while every
    month before August was complete to within a handful.
    """
    sys.path.insert(0, str(HERE.parent))
    from scripts.load_legacy_ledger import BOOK_OF_BRANCH

    found: dict[str, str] = {}
    for source in sources:
        if not source.exists():
            say(f"   no {source.name}, skipping it")
            continue
        db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dbo.AccountLedger'")}
        if not tables:
            say(f"   {source.name} holds no ledger, skipping it")
            db.close()
            continue
        rows = db.execute("""
            SELECT UPPER(TRIM(Type)) AS t, TRIM(VCode) AS v,
                   MIN(Date) AS moment, MIN(TRIM(COALESCE(UseBranchSno, ''))) AS branch
              FROM "dbo.AccountLedger"
             WHERE Date IS NOT NULL AND TRIM(Date) <> ''
             GROUP BY UPPER(TRIM(Type)), TRIM(VCode)
        """)
        taken = no_book = no_time = 0
        for r in rows:
            book = BOOK_OF_BRANCH.get(r["branch"])
            if not book:
                no_book += 1
                continue
            moment = str(r["moment"]).strip()
            if len(moment) < 19 or moment[11:] in ("", "00:00:00"):
                # Midnight is how their software writes "no time", the same rule core/when.py applies everywhere,
                # so it is left null rather than recorded as a minute nobody chose.
                no_time += 1
                continue
            found[f"legacy:{book}:{r['t']}-{r['v']}"[:160]] = moment
            taken += 1
        db.close()
        say(f"   {source.name}: {taken:,} voucher(s) carry a moment"
            + (f", {no_book:,} on a branch that is not ours" if no_book else "")
            + (f", {no_time:,} written at midnight" if no_time else ""))
    say(f"   {len(found):,} distinct voucher(s) to fill, the live server's copy winning where both have one")
    return list(found.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="actually add the column and fill it")
    parser.add_argument("--source", action="append", default=None,
                        help="a staged ledger to read; repeatable, later ones win. Defaults to the backup "
                             "then the live copy.")
    args = parser.parse_args()

    sources = [pathlib.Path(s) for s in args.source] if args.source else SOURCES
    if not any(s.exists() for s in sources):
        say(f"   no staged ledger at any of: {', '.join(str(s) for s in sources)}")
        return 1

    url = os.environ.get("DB_URL", "")
    if "mssql" not in url:
        say("   This alters SQL Server in place, because aerich cannot migrate it in this project.")
        say("   Point DB_URL at head office and run it again.")
        return 1

    import pyodbc

    # Taken apart the same way `load_legacy_ledger.Out.open` does it, so there is one shape of connection string in
    # this folder rather than two that can drift.
    user, _, rest = url[len("mssql://"):].partition(":")
    secret, _, hostpart = rest.rpartition("@")
    host, _, database = hostpart.partition("/")
    database = database.split("?")[0]
    server, _, port = host.partition(":")

    pairs = pairs_from_staging(sources)
    conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:{server},{port or 1433};"
        f"DATABASE={database};UID={user};PWD={secret};Encrypt=no;TrustServerCertificate=yes",
        timeout=180, autocommit=False)
    cur = conn.cursor()

    has = cur.execute("""SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                          WHERE TABLE_NAME = 'acc_vouchers' AND COLUMN_NAME = 'at'""").fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM acc_vouchers").fetchone()[0]
    filled = cur.execute("SELECT COUNT(*) FROM acc_vouchers WHERE [at] IS NOT NULL").fetchone()[0] if has else 0
    say(f"   acc_vouchers holds {total:,} voucher(s); the column is "
        f"{'there' if has else 'MISSING'}, {filled:,} filled")

    if not args.yes:
        say("")
        say(f"   It would {'' if has else 'add the nullable `at` column and '}fill it on up to "
            f"{len(pairs):,} voucher(s). `date` is not touched. Nothing done. Add --yes.")
        conn.close()
        return 0

    if not has:
        cur.execute("ALTER TABLE acc_vouchers ADD [at] datetime2 NULL")
        conn.commit()
        say("   added acc_vouchers.[at] as datetime2 NULL")

    # The temp table's key has to carry `acc_vouchers.source`'s own collation, not the server's default. This table
    # is UTF-8 (Latin1_General_100_CI_AS_SC_UTF8) while tempdb follows the instance, and SQL Server refuses to
    # compare two collations rather than silently picking one -- correctly, but it means the collation has to be
    # read and repeated here rather than assumed.
    collation = cur.execute("""SELECT COLLATION_NAME FROM INFORMATION_SCHEMA.COLUMNS
                                WHERE TABLE_NAME = 'acc_vouchers' AND COLUMN_NAME = 'source'""").fetchone()[0]
    say(f"   matching the collation of acc_vouchers.source: {collation}")
    cur.execute(f"""IF OBJECT_ID('tempdb..#voucher_moment') IS NOT NULL DROP TABLE #voucher_moment;
                    CREATE TABLE #voucher_moment (
                        src varchar(160) COLLATE {collation} NOT NULL PRIMARY KEY,
                        moment datetime2 NOT NULL)""")
    cur.fast_executemany = True
    at = time.monotonic()
    for i in range(0, len(pairs), CHUNK):
        cur.executemany("INSERT INTO #voucher_moment (src, moment) VALUES (?, ?)", pairs[i:i + CHUNK])
    say(f"   staged {len(pairs):,} pair(s) in {time.monotonic() - at:.0f}s")

    # Not `WHERE v.[at] IS NULL`. The staged pairs already have the live server's copy over the backup's, and
    # stopping at the empty ones would quietly keep a backup value where live has since changed it -- the exact
    # thing `fetch_live.py`'s rule exists to prevent. Writing both cases keeps this re-runnable and keeps live
    # winning on every run, not only the first.
    at = time.monotonic()
    cur.execute("""UPDATE v SET v.[at] = m.moment
                     FROM acc_vouchers v JOIN #voucher_moment m ON m.src = v.source
                    WHERE v.[at] IS NULL OR v.[at] <> m.moment""")
    changed = cur.rowcount
    conn.commit()
    say(f"   wrote a moment on {changed:,} voucher(s) in {time.monotonic() - at:.0f}s")

    # A moment that falls on a different day from the voucher's own `date` is dropped rather than kept. Not because
    # the match was wrong -- the source key is theirs, exact, and these came from the row the voucher was built
    # from -- but because THEY RE-DATE ENTRIES AFTER WE HAVE LOADED THEM. BR-414424056 in book FC is dated
    # 20 September here and its only ledger row now reads 21 September 09:03, with no row left on the 20th.
    #
    # Keeping it would mean one voucher that a report filtering on `date` puts in one day and a report filtering
    # on `at` puts in another, which is the single thing this whole change exists to avoid. One voucher losing its
    # time costs nothing. A voucher whose time contradicts its date is a fault nobody would find for months.
    #
    # The stale date is a real finding, and it belongs to the reconcile sweep that repairs a month from their live
    # server, not here: this script does not touch `date`.
    moved = cur.execute("""SELECT number, book, [date], [at] FROM acc_vouchers
                            WHERE [at] IS NOT NULL AND CAST([at] AS date) <> [date]""").fetchall()
    if moved:
        cur.execute("""UPDATE acc_vouchers SET [at] = NULL
                        WHERE [at] IS NOT NULL AND CAST([at] AS date) <> [date]""")
        conn.commit()

    now = cur.execute("SELECT COUNT(*) FROM acc_vouchers WHERE [at] IS NOT NULL").fetchone()[0]
    say("")
    say(f"   {now:,} of {total:,} voucher(s) now carry a moment")
    if moved:
        say(f"   {len(moved)} left without one, because their ledger has since moved the entry to another day:")
        for number, book, day, moment in moved[:10]:
            say(f"      {number} in {book}: dated {day}, their ledger now says {moment}")
        if len(moved) > 10:
            say(f"      and {len(moved) - 10} more")
        say("      Their `date` is what is stale, not the moment. Repairing that is the sweep's job, from their")
        say("      live server rather than the backup, and this script leaves `date` alone.")
    say("   Every moment that was kept falls on the day its voucher is already dated, which is the check that it")
    say("   matched the right rows rather than merely matching something.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
