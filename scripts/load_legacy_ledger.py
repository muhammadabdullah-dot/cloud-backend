"""Load Multi-Tec's posted general ledger, replacing the one-sided vouchers taken from their entry forms.

    set DB_URL=mssql://...                                  (or sqlite://...)
    python scripts/load_legacy_ledger.py                     what it would do
    python scripts/load_legacy_ledger.py --yes                 do it
    python scripts/load_legacy_ledger.py --yes --no-backup     only for a database that is already a copy

IT WRITES TO WHICHEVER ENGINE DB_URL NAMES
------------------------------------------
Head office runs on SQL Server. An earlier version of this file wrote to the SQLite copy and then relied on
`migrate_to_sqlserver.py` to carry the two tables across, which reused a verified path but made the retired engine the
place new data lands first. That is backwards, so the engine is read from `DB_URL` and the only difference between the
two is how a value is handed to the driver: SQLite takes a Decimal, a date and a timestamp as text, SQL Server takes
them as themselves, because its columns are `decimal(16,2)`, `date` and `datetime2` rather than free text.

Running it against both engines is also the strongest check there is on it: the same reader, the same grouping and the
same arithmetic, landing in two different databases, has to produce the same counts and the same totals. That is what
`compare_engines.py` then confirms.

WHY THIS REPLACES RATHER THAN ADDS
----------------------------------
Head office's books were built from `dbo.JVHeader` and `dbo.JournalVoucher`: 14,545 vouchers and 76,819 lines. That is
their **entry form**, not their ledger, and it holds one side of each entry. Their `dbo.AccountLedger` is the posted
ledger and holds both.

Voucher CP-400038014, which is ordinary rather than chosen to flatter the point:

    JournalVoucher    9 lines, every one a debit, Rs 27,600 and nothing to balance it
    AccountLedger    18 lines, each of those debits paired with a credit to account 169, cash. Balances to zero.

That is the whole difference, and it is why 11,215 of the 14,545 vouchers only balanced once their header account was
counted, and why their trial balance balances to a rupee while ours did not. 99.2% of JournalVoucher's rows are in
AccountLedger, and 97.4% match on account and amount too, so loading both would count the same money twice.

So the vouchers sourced `legacy:` are deleted and rebuilt from AccountLedger. `header_account_id` is left null on the
new ones: with both sides on the lines there is no other side to point at.

It deletes before it loads, which also makes it the repair for its own interrupted run: a half-loaded ledger is
replaced rather than added to.

WHAT ARRIVES
------------
1,101,527 vouchers and 5,343,432 lines, 1 Oct 2022 to 31 Dec 2026, against the 14,545 and 76,819 they replace. Most of
them are documents posting themselves: 5,023,328 lines are type SV, which is 4.85 per bill across 1,036,271 bills,
and 100,214 are PV from purchases. Those are the postings head office never had.

HOW A VOUCHER IS PUT BACK TOGETHER
----------------------------------
`(Type, VCode)` is the voucher, measured rather than assumed: 1,101,527 distinct pairs for 1,101,527 vouchers, none
spanning two branches, and `<Type>-<VCode>` is at most 15 characters with no two alike, so it is the number.

Their manual vouchers also have a `dbo.JVHeader` row, and where there is one it supplies what a ledger does not keep:
the voucher number their staff see, who entered and posted it, and whether it was posted. The 1,086,982 that are a
document posting itself have no header, so theirs is built from the ledger: the date and remarks of its first line.

The `source` stays `legacy:<book>:<Type>-<VCode>`. It has to begin `legacy:` exactly, because `remove_demo_data.py`
tells demonstration data from theirs by that prefix and would take 1.1 million real vouchers for seeded ones.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import uuid
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

SOURCE = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\ops\legacy-import\staging\source.db")
CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"

# their branch number -> our book. From ops/legacy-import/crosswalk.py, where the reasoning for GT having two of
# their numbers is written down.
BOOK_OF_BRANCH = {"1": "GT", "6": "GT", "2": "GG", "3": "HO", "4": "MT", "5": "FC"}

# Their voucher type -> ours. The first group is what map_books already used for the manual vouchers. The second is
# new here, because these types only exist in the ledger: a document posting itself.
VTYPE = {
    "JV": "JV", "BR": "BRV", "CP": "CPV", "BP": "BPV", "CR": "CRV",
    "BPAP": "BPV", "CPAP": "CPV", "BPAPP": "BPV", "CPAPP": "CPV",
    "BRAS": "BRV", "CRAS": "CRV", "CPAS": "CPV", "BPAS": "BPV",
    # A post-dated cheque paid out is a bank payment whose date is the cheque's.
    "PDBP": "BPV",
    # Documents. `vtype` holds four characters, which all of these fit.
    "SV": "SV", "PV": "PV", "SRV": "SRV", "PRV": "PRV", "OP": "OP",
}

# The types that are a document posting itself rather than somebody entering a voucher. They are marked `auto`, the
# same flag head office sets on its own GRN and transfer postings.
AUTOMATIC = {"SV", "PV", "SRV", "PRV"}

VOUCHER_COLUMNS = ("id", "book", "number", "vtype", "date", "at", "status", "auto", "source", "reference_no",
                   "description", "cheque_no", "cheque_date", "total", "created_by_name", "posted_by_name",
                   "version", "mirrored", "reversed", "created_at", "updated_at")
LINE_COLUMNS = ("id", "voucher_id", "line_no", "account_id", "debit", "credit", "description", "reference_no")

VOUCHERS_PER_COMMIT = 5_000


def money(value) -> Decimal:
    """Their amount, as a number. Blank and unreadable both become zero: a ledger line with no amount is a line for
    nothing, and refusing the row would drop the other side of a balanced entry with it."""
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value).strip() or "0").quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def cut(value, length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:length] or None


def a_moment(value) -> datetime | None:
    """The whole of one of their timestamps, for `acc_vouchers.at`, beside the date part that `a_date` keeps.

    Their `Date` is `YYYY-MM-DD HH:MM:SS` on all 5,343,432 rows and 5,340,870 carry a real clock time. We kept only
    the first ten characters, so every date range on the books answered "whole days only" for want of a column to
    compare against. Midnight is treated as no time at all, the same rule `app/core/when.py` applies everywhere:
    midnight and nothing-at-all are the same value, so recording one as a minute somebody chose would be a guess.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) < 19 or text[11:19] == "00:00:00":
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def a_date(value) -> Date | None:
    """The date part of one of their timestamps. They are all `YYYY-MM-DD HH:MM:SS` and none is blank, checked over
    all 5,343,432 of them, so anything else is worth refusing rather than guessing at."""
    if value is None:
        return None
    text = str(value).strip()[:10]
    if not text:
        return None
    try:
        return Date.fromisoformat(text)
    except ValueError:
        return None


# ── where it is written ─────────────────────────────────────────────────────────────────────────────

class Target:
    """The database being written to, and the few things that differ between the two engines."""

    def __init__(self, url: str):
        self.url = url
        self.kind = "mssql" if url.startswith("mssql://") else "sqlite"
        self.conn = None

    # The name to show, never the password.
    def label(self) -> str:
        if self.kind == "mssql":
            tail = self.url.split("@")[-1].split("?")[0]
            return f"SQL Server, {tail}"
        return f"SQLite, {self.url[len('sqlite://'):]}"

    def open(self):
        if self.kind == "sqlite":
            path = pathlib.Path(self.url[len("sqlite://"):])
            if not path.exists():
                raise SystemExit(f"No database at {path}")
            self.path = path
            self.conn = sqlite3.connect(path, timeout=180)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            # Head office may be running against this file and posts the books every five minutes, so a write here
            # and a write there will collide. Waiting is cheaper than failing a load two thirds of the way through.
            self.conn.execute("PRAGMA busy_timeout = 180000")
        else:
            import pyodbc

            user, _, rest = self.url[len("mssql://"):].partition(":")
            secret, _, hostpart = rest.rpartition("@")
            host, _, database = hostpart.partition("/")
            database = database.split("?")[0]
            server, _, port = host.partition(":")
            self.conn = pyodbc.connect(
                f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:{server},{port or 1433};"
                f"DATABASE={database};UID={user};PWD={secret};Encrypt=no;TrustServerCertificate=yes",
                timeout=180, autocommit=False)
            self.database = database
        return self

    def adapt(self, row: tuple) -> tuple:
        """One row, for this driver. SQL Server's columns are `decimal(16,2)`, `date` and `datetime2`, so they take
        the values as they are. SQLite stores all three as text, which is what Tortoise put there, so a Decimal
        handed to it unchanged would come back as a float and lose its last paisa."""
        if self.kind == "mssql":
            return row
        out = []
        for v in row:
            if isinstance(v, Decimal):
                out.append(str(v))
            elif isinstance(v, datetime):
                out.append(v.strftime("%Y-%m-%d %H:%M:%S"))
            elif isinstance(v, Date):
                out.append(v.isoformat())
            else:
                out.append(v)
        return tuple(out)

    def query(self, sql: str, args: tuple = ()):  # noqa: D102
        cur = self.conn.cursor()
        cur.execute(sql, args) if args else cur.execute(sql)
        return cur.fetchall()

    def one(self, sql: str):
        return self.query(sql)[0]

    def execute(self, sql: str) -> int:
        cur = self.conn.cursor()
        cur.execute(sql)
        return cur.rowcount

    def insert_many(self, table: str, columns: tuple, rows: list[tuple]) -> None:
        if not rows:
            return
        marks = ", ".join("?" * len(columns))
        names = ", ".join(f'"{c}"' if self.kind == "sqlite" else f"[{c}]" for c in columns)
        quoted = f'"{table}"' if self.kind == "sqlite" else f"[{table}]"
        sql = f"INSERT INTO {quoted} ({names}) VALUES ({marks})"
        cur = self.conn.cursor()
        if self.kind == "mssql":
            cur.fast_executemany = True
        cur.executemany(sql, rows)

    def commit(self) -> None:
        self.conn.commit()

    def backup(self, say) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        at = time.monotonic()
        if self.kind == "sqlite":
            target = self.path.with_name(f"{self.path.name}.bak-before-ledger-{stamp}")
            say(f"   backing up to {target.name} ...")
            ro = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            ro.execute("VACUUM INTO ?", (str(target),))
            ro.close()
            say(f"      {target.stat().st_size / 1e9:.2f} GB in {time.monotonic() - at:.0f}s")
            return
        # SQL Server has no VACUUM INTO, and backup_service has not been ported, so this uses the engine's own.
        was = self.conn.autocommit
        self.conn.autocommit = True
        cur = self.conn.cursor()
        # WHERE THE BACKUP GOES, AND WHY IT IS NOT ALWAYS THE INSTANCE DEFAULT
        # --------------------------------------------------------------------
        # The instance default is under C:\Program Files, and these are 5.4 GB each. The nightly rotates its own
        # by weekday so it keeps at most seven, but every one-off taken before a load is kept for ever under its
        # own stamped name, and on 13 October 2026 five of those had filled C: to 6 GB free -- so the next backup
        # failed with "There is not enough space on the disk" and took the load with it, which is the right way
        # round but a poor way to find out.
        #
        # LEDGER_BACKUP_DIR moves them to a drive with room. SQL Server writes the file itself, under its own
        # service account, so the folder has to be one that account can write; it is left to the environment
        # rather than guessed here.
        folder = (os.environ.get("LEDGER_BACKUP_DIR") or "").strip().rstrip("\\")
        if not folder:
            cur.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(400))")
            folder = cur.fetchone()[0].rstrip("\\")
        path = f"{folder}\\{self.database}-before-ledger-{stamp}.bak"
        say(f"   backing up to {path} ...")
        cur.execute(f"BACKUP DATABASE [{self.database}] TO DISK = N'{path}' WITH INIT, COMPRESSION, "
                    f"NAME = N'before the ledger load'")
        while cur.nextset():
            pass
        say(f"      done in {time.monotonic() - at:.0f}s")
        self.conn.autocommit = was


def db_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("DB_URL") or ""
    if url:
        return url
    found = re.findall(r"mssql://\S+", CREDENTIALS.read_text(encoding="utf-8")) if CREDENTIALS.exists() else []
    if not found:
        raise SystemExit("Set DB_URL to the database to load, or pass --target.")
    return found[-1].strip()


def days_that_would_vanish(src, out, scope: str) -> list[str]:
    """The days of books here that this staging copy has nothing for, sorted.

    A run without a window clears every legacy voucher and loads the staging copy in its place. Any day we hold that
    the copy cannot supply is therefore deleted with nothing put back. That is not hypothetical: it happened three
    times in one morning, to the figures, to the bills and to this ledger, because the restored backup stops where
    the backup was taken and the days after it live only in the copy fetched over the VPN. Each time the screens went
    quiet and looked like a sync failure, and each time the sync had been fine.

    WHY THIS COMPARES DAYS AND NOT THE LAST DAY
    -------------------------------------------
    The first version of this guard compared `MAX(date)` on each side, and it passed while the load destroyed eight
    days. Their books carry post dated cheques out to 2026-12-31 and *both* sides held one, so both maxima were
    2026-12-31 and the guard saw nothing wrong. The days that went missing were in the middle, between where the
    backup was taken and where our books actually reached, and a maximum cannot see a hole underneath it. Only the
    set of days can. `ops/checks/ledger_gap_guard.py` holds that case as a test, because the guard was wrong once and
    nothing about it looked wrong.
    """
    theirs = {str(day)[:10] for (day,) in
              src.execute('SELECT DISTINCT substr(Date, 1, 10) FROM "dbo.AccountLedger"') if day}
    ours = {str(day)[:10] for (day,) in
            out.query(f"SELECT DISTINCT date FROM acc_vouchers WHERE {scope}") if day}
    return sorted(ours - theirs)


# HOW FAR OUT IS STILL BALANCED
# -----------------------------
# Their ledger keeps some figures to the whole rupee and some to the paisa, so a voucher of theirs that is
# perfectly good routinely lands a few paisa out when both sides are added in full precision. This is the line
# between that and a voucher genuinely missing a side.
#
# It was 0.01, and the cost of that was measured on 13 October 2026. Seven vouchers in the whole of their history
# were being held as drafts, out by 0.27, -0.47, -0.15, -0.16, -0.17, -0.40 and -0.18 rupees. One of them carries
# Rs 12,828,669. Because `accounts_reports_service._sums` counts only posted vouchers, their money was absent
# from our profit and loss while present in theirs, and that alone was the whole of the residual difference across
# eight months: Rs 1,047 of POS charges in May 2025, Rs 300,000 in December, Rs 492 in February 2026.
#
# A rupee, then. A voucher out by less than that is out by their rounding and is posted; one out by more is a
# voucher with a side missing and is still carried as a draft, visible in the books rather than quietly inside a
# trial balance that no longer adds up. Nothing in their history is out by between one rupee and a real amount,
# so this is not a threshold anybody has to tune.
ROUNDING = Decimal("1.00")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--backup-only", action="store_true",
                    help="back the database up and stop, reading nothing and writing nothing else. "
                         "`ops/legacy-import/reconcile_history.py` uses it to take one backup before a sweep "
                         "that then repairs month by month with --no-backup.")
    ap.add_argument("--source", default=str(SOURCE),
                    help="the staging file to read; staging/live.db for a windowed top-up")
    ap.add_argument("--window", help="FROM:TO. Only these posting dates are replaced; everything else is left alone")
    ap.add_argument("--force", action="store_true",
                    help="load without a window even though the books here reach past this staging copy, which "
                         "deletes the days in between. Only when losing them is what you meant")
    ap.add_argument("--target", help="the database to write to; DB_URL is used when this is left out")
    args = ap.parse_args()

    window = None
    if args.window:
        a, _, b = args.window.partition(":")
        window = (Date.fromisoformat(a), Date.fromisoformat(b))

    source_path = pathlib.Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"No staged backup at {source_path}")
    src = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    out = Target(db_url(args.target)).open()

    # ── what is there now, and what is coming ───────────────────────────────────────────────────────
    # With a window, "what is there now" and "what gets replaced" both mean the window and nothing else. Their
    # books carry post-dated entries, dated when they fall due, so a window replaced by posting date is the only
    # one that matches what a windowed fetch brought back.
    scope = "source LIKE 'legacy:%'"
    if window:
        scope += f" AND date >= '{window[0].isoformat()}' AND date <= '{window[1].isoformat()}'"
    have_vouchers = out.one(f"SELECT COUNT(*) FROM acc_vouchers WHERE {scope}")[0]
    have_lines = out.one(f"""SELECT COUNT(*) FROM acc_voucher_lines
                              WHERE voucher_id IN (SELECT id FROM acc_vouchers WHERE {scope})""")[0]
    others = out.one("SELECT COUNT(*) FROM acc_vouchers WHERE source IS NULL OR source NOT LIKE 'legacy:%'")[0]
    accounts: dict[tuple[str, str], str] = {
        (str(book), str(code)): str(aid) for book, code, aid in out.query("SELECT book, code, id FROM acc_accounts")}

    # THEIR `Code` IS NOT UNIQUE AND CANNOT BE THE JOIN
    # ------------------------------------------------
    # 201 of their 1,330 accounts carry the literal string '0' as their code -- CASH SALE WALKING CUSTOMER
    # (AccSno 37), MULLER & PHIPPS (AccSno 39), D.MARINA 1 (MODEL TOWN) and 198 more. This read the code and only
    # treated it as absent when it was *empty*, so all 201 resolved to whichever single account held code '0' in
    # our chart. 2,576,093 of 5,343,432 ledger lines -- 48% of the books -- belong to one of those 201, and every
    # one of them was posted to CASH SALE WALKING CUSTOMER. A bank payment to Muller & Phipps read as a payment to
    # a walking customer, which is how it was spotted: on screen, in one row, by somebody who knew the supplier.
    #
    # map_books._their_code() already solved this for the chart: it treats '0' as absent and renames the clashes to
    # A00037 and the like. The pairing it used travels in the books payload as `theirAccSno` -> `code`, so reading
    # that is the only way the ledger and the chart are guaranteed to agree. load_legacy_cheques.py joins the same
    # way, for the same reason.
    code_of_sno: dict[int, str] = {}
    books_dir = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "ops" / "legacy-import" / "staging"
    for book_file in sorted(books_dir.glob("payload-*-books.json")):
        for acc in json.loads(book_file.read_text(encoding="utf-8")).get("accounts", []):
            if acc.get("theirAccSno") is not None:
                code_of_sno[int(acc["theirAccSno"])] = acc["code"]
    if not code_of_sno:
        raise SystemExit(
            "\n   No books payload in staging, so their account numbers cannot be paired with our codes."
            "\n   Run ops/legacy-import/map_books.py first. Joining on their Code instead puts 48% of the"
            "\n   ledger on one account, which is worse than not loading it.")
    print(f"   {len(code_of_sno):,} of their accounts paired to ours through the chart's own numbering", flush=True)

    plan_where, plan_args = "", []
    if window:
        plan_where = " WHERE substr(Date, 1, 10) BETWEEN ? AND ?"
        plan_args = [window[0].isoformat(), window[1].isoformat()]
    coming_lines = src.execute(f'SELECT COUNT(*) FROM "dbo.AccountLedger"{plan_where}', plan_args).fetchone()[0]
    coming_vouchers = src.execute(
        f'SELECT COUNT(*) FROM (SELECT DISTINCT Type, VCode FROM "dbo.AccountLedger"{plan_where})',
        plan_args).fetchone()[0]

    print(f"   from  {source_path}")
    print(f"   into  {out.label()}\n")
    print(f"   {'':<36} {'now':>12} {'after':>12}")
    print(f"   {'vouchers marked legacy':<36} {have_vouchers:>12,} {coming_vouchers:>12,}")
    print(f"   {'their lines':<36} {have_lines:>12,} {coming_lines:>12,}")
    print(f"   {'head office own, untouched':<36} {others:>12,} {others:>12,}")
    print(f"\n   {len(accounts):,} account(s) in our chart across the books, {len(code_of_sno):,} in theirs")

    types = {str(r["Type"]).strip().upper(): r["n"] for r in src.execute(
        'SELECT Type, COUNT(*) AS n FROM "dbo.AccountLedger" GROUP BY Type')}
    unknown = sorted(t for t in types if t not in VTYPE)
    print(f"   {len(types)} voucher type(s) in their ledger")
    if unknown:
        print(f"   *** {len(unknown)} type(s) this does not map, which would all become JV: {unknown}")

    if not args.yes:
        print("\n   Nothing changed. Add --yes.")
        return 0

    if not args.no_backup:
        print()
        out.backup(lambda line: print(line, flush=True))
    if args.backup_only:
        print("\n   Backed up, and nothing else was read or written.")
        return 0

    # ── the manual vouchers' own details, which a ledger does not keep ─────────────────────────────
    heads: dict[tuple[str, str], sqlite3.Row] = {}
    for r in src.execute("""
        SELECT h.JvSno, h.Htype, h.Posted, h.Hremarks, h.HChequeNo, h.VoucherNo, h.CreatedBy, h.PostedBy
          FROM "dbo.JVHeader" h
    """):
        heads[(str(r["Htype"] or "").strip().upper(), str(r["JvSno"]).strip())] = r
    print(f"\n   {len(heads):,} of their vouchers have an entry-form header; the rest are documents posting"
          f" themselves", flush=True)

    if not window:
        vanishing = days_that_would_vanish(src, out, scope)
        if vanishing and not args.force:
            shown = vanishing[:8]
            print(f"\n   STOPPING. {len(vanishing):,} day(s) of books here have no entries in this staging copy,")
            print(f"   from {vanishing[0]} to {vanishing[-1]}. A run without a window deletes them and puts")
            print("   nothing back. The days are:")
            print("      " + ", ".join(shown) + (f", and {len(vanishing) - len(shown):,} more" if len(vanishing) > len(shown) else ""))
            print("   Either load those days from the copy that does have them:")
            print(f"      --source staging/live.db --window {vanishing[0]}:{vanishing[-1]}")
            print("   or pass --force if losing them is what you meant.")
            return 2

    print("   removing whatever is there marked legacy ...", flush=True)
    gone_lines = out.execute(f"""DELETE FROM acc_voucher_lines
                                  WHERE voucher_id IN (SELECT id FROM acc_vouchers WHERE {scope})""")
    gone_vouchers = out.execute(f"DELETE FROM acc_vouchers WHERE {scope}")
    out.commit()
    print(f"      {gone_vouchers:,} voucher(s) and {gone_lines:,} line(s) removed", flush=True)

    # ── in with the ledger ─────────────────────────────────────────────────────────────────────────
    print("   reading their ledger, grouped by voucher ...", flush=True)
    started = time.monotonic()
    where, params = "", []
    if window:
        where = " WHERE substr(Date, 1, 10) BETWEEN ? AND ?"
        params = [window[0].isoformat(), window[1].isoformat()]
    rows = src.execute(f"""
        SELECT Type, VCode, Date, AccSno, Debit, Credit, Remarks, ChequeNo, InvoiceNo, UseBranchSno, DueDate
          FROM "dbo.AccountLedger"{where}
         ORDER BY Type, VCode
    """, params)

    stamp_now = datetime.now().replace(microsecond=0)
    v_batch: list[tuple] = []
    l_batch: list[tuple] = []
    made = lines_made = balanced = unbalanced = no_account = skipped_no_book = 0
    per_book: collections.Counter = collections.Counter()
    worst: list[tuple[str, float]] = []
    reported = 0

    def flush() -> None:
        nonlocal v_batch, l_batch
        out.insert_many("acc_vouchers", VOUCHER_COLUMNS, v_batch)
        out.insert_many("acc_voucher_lines", LINE_COLUMNS, l_batch)
        out.commit()
        v_batch, l_batch = [], []

    def emit(key, group) -> None:
        nonlocal made, lines_made, balanced, unbalanced, no_account, skipped_no_book
        theirs_type, vcode = key
        book = BOOK_OF_BRANCH.get(str(group[0]["UseBranchSno"] or "").strip())
        if not book:
            skipped_no_book += 1
            return
        vid = str(uuid.uuid4())
        head = heads.get((theirs_type, str(vcode).strip()))

        built: list[tuple] = []
        debit_total = credit_total = Decimal("0.00")
        missing_here = 0
        for n, line in enumerate(group, 1):
            sno = line["AccSno"]
            code = code_of_sno.get(int(sno)) if sno is not None else None
            account_id = accounts.get((book, code)) if code else None
            if not account_id:
                missing_here += 1
                continue
            dr, cr = money(line["Debit"]), money(line["Credit"])
            debit_total += dr
            credit_total += cr
            built.append(out.adapt((str(uuid.uuid4()), vid, n, account_id, dr, cr,
                                    cut(line["Remarks"], 255), cut(line["ChequeNo"], 60))))
        no_account += missing_here
        if not built:
            return

        off = debit_total - credit_total
        if abs(off) < ROUNDING:
            balanced += 1
        else:
            unbalanced += 1
            if len(worst) < 12:
                worst.append((f"{theirs_type}-{vcode}", float(off)))

        # A voucher whose lines do not balance is carried as a draft rather than posted, so it is visible in the
        # books rather than quietly inside a trial balance that no longer adds up.
        posted = str(head["Posted"] or "").strip() in ("Y", "y", "1") if head else True
        ok = posted and not missing_here and abs(off) < ROUNDING

        v_batch.append(out.adapt((
            vid, book, f"{theirs_type}-{vcode}"[:30], VTYPE.get(theirs_type, "JV"),
            a_date(group[0]["Date"]), a_moment(group[0]["Date"]), "posted" if ok else "draft",
            1 if theirs_type in AUTOMATIC else 0,
            f"legacy:{book}:{theirs_type}-{vcode}"[:160],
            cut(head["VoucherNo"], 60) if head else cut(group[0]["InvoiceNo"], 60),
            cut(head["Hremarks"], 500) if head else cut(group[0]["Remarks"], 500),
            cut(head["HChequeNo"], 30) if head else cut(group[0]["ChequeNo"], 30),
            a_date(group[0]["DueDate"]),
            debit_total if debit_total >= credit_total else credit_total,
            cut(head["CreatedBy"], 120) if head else None,
            cut(head["PostedBy"], 120) if head else None,
            1, 0, 0, stamp_now, stamp_now,
        )))
        l_batch.extend(built)
        made += 1
        lines_made += len(built)
        per_book[book] += 1

    current: tuple | None = None
    group: list[sqlite3.Row] = []
    for row in rows:
        key = (str(row["Type"] or "").strip().upper(), str(row["VCode"] or "").strip())
        if current is not None and key != current:
            emit(current, group)
            group = []
            if len(v_batch) >= VOUCHERS_PER_COMMIT:
                flush()
                if made - reported >= 100_000:
                    reported = made
                    done = max(time.monotonic() - started, 0.01)
                    left = (coming_vouchers - made) / (made / done)
                    print(f"      {made:>9,} voucher(s), {lines_made:>10,} line(s)   "
                          f"{made / done:>7,.0f}/s   about {left / 60:>4.0f} min left", flush=True)
        current = key
        group.append(row)
    if current is not None and group:
        emit(current, group)
    flush()

    took = time.monotonic() - started
    print(f"\n   {made:,} voucher(s) and {lines_made:,} line(s) in {took:,.0f}s")
    print(f"      {balanced:,} balance on their own lines, {unbalanced:,} do not and are carried as drafts")
    if no_account:
        print(f"      {no_account:,} line(s) dropped: their account has no home in our chart")
    if skipped_no_book:
        print(f"      {skipped_no_book:,} voucher(s) skipped: their branch is not one of the five shops")
    for number, off in worst:
        print(f"         {number} out by {off:,.2f}")
    print("   per book: " + "  ".join(f"{b} {n:,}" for b, n in sorted(per_book.items())))

    # ── does it add up ─────────────────────────────────────────────────────────────────────────────
    # Both sides totalled over the same span. A windowed run compares the window against the window: totalling our
    # whole ledger against their window would report a difference of everything outside it and call a correct load
    # a failure.
    theirs = src.execute(
        f'SELECT SUM(CAST(Debit AS REAL)), SUM(CAST(Credit AS REAL)) FROM "dbo.AccountLedger"{plan_where}',
        plan_args).fetchone()
    mine = out.one(f"""SELECT SUM(CAST(l.debit AS float)), SUM(CAST(l.credit AS float))
                         FROM acc_voucher_lines l JOIN acc_vouchers v ON v.id = l.voucher_id
                        WHERE {scope}""".replace("WHERE source LIKE", "WHERE v.source LIKE")
                   .replace("AND date >=", "AND v.date >=").replace("AND date <=", "AND v.date <="))
    print(f"\n   {'':<22} {'debits':>20} {'credits':>20}")
    print(f"   {'their ledger':<22} {theirs[0]:>20,.2f} {theirs[1]:>20,.2f}")
    print(f"   {'ours':<22} {mine[0]:>20,.2f} {mine[1]:>20,.2f}")
    gap_dr, gap_cr = abs(theirs[0] - mine[0]), abs(theirs[1] - mine[1])
    print(f"   {'difference':<22} {gap_dr:>20,.2f} {gap_cr:>20,.2f}")
    own, theirs_own = abs(mine[0] - mine[1]), abs(theirs[0] - theirs[1])
    print(f"\n   our legacy books are out by {own:,.2f} across every voucher (theirs: {theirs_own:,.2f})")

    # The test is whether ours matches theirs, not whether ours is zero. Their own posted ledger is out by Rs 1.26
    # over 8.09 billion, and a load that "fixed" that would not be their books any more. So the imbalance has to be
    # the same imbalance, and an earlier version of this check demanded it be under a rupee and called a faithful
    # load a failure.
    #
    # The 0.34 on each side is this script quantizing to the paisa while their amounts carry four decimal places:
    # four parts in a hundred billion, spread over 5,343,432 lines, and it lands on both sides equally, which is
    # why the imbalance comes out identical rather than drifting.
    src.close()
    off_by = abs(own - theirs_own)
    if gap_dr >= 1.0 or gap_cr >= 1.0 or off_by >= 0.01:
        print("\n   NOT CLEAN: this does not reproduce their ledger. Nothing in staging was touched, and this"
              " deletes before it loads, so it can simply be run again.")
        return 1
    print(f"\n   Every posting arrived, and the books are out by exactly what theirs are out by ({own:,.2f}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
