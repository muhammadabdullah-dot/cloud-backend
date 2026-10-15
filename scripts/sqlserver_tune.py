"""The indexes and statistics SQL Server needs that the models cannot express.

    python scripts/sqlserver_tune.py                 what is there and what is missing
    python scripts/sqlserver_tune.py --apply          create what is missing and rebuild statistics
    python scripts/sqlserver_tune.py --apply --statistics-only

WHY THESE ARE NOT IN THE MODELS
-------------------------------
Tortoise can declare an index on a list of columns. It cannot declare an INCLUDE list, and INCLUDE is the whole point
of the one below: a covering index lets an aggregate read a narrow copy of the columns it needs instead of the whole
wide row. So these live here, named and explained, rather than being invisible tuning somebody applied once by hand.

WHAT THE COVERING INDEX IS WORTH, MEASURED
------------------------------------------
`branch_product_stats` holds 1,818,232 rows and is clustered on `id`, a random uuid, so it has no useful physical
order. Four dashboard queries aggregate a date range out of it. Before this index, on the year view:

    top categories   18.70s        top products     15.92s
    top brands       17.57s        sold and unsold  12.01s

After it, the same four: 0.76s, 1.04s, 0.29s and 0.34s, and the whole year view went from 41.2s to 11.0s, which is
the difference between a screen that renders and one the frontend abandons at 15 seconds. The index costs 326 MB.

Statistics matter for a different reason and are worth saying plainly: a bulk load leaves them as whatever the load
happened to produce, so the planner is choosing between strategies using numbers that predate the data. Rebuilding
them with a full scan takes 45 seconds over 16.4 million rows. On its own it changed the year view by 0.2s, so it is
not what fixed the dashboard, but it is what makes the fix predictable rather than lucky.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"

# name, table, key columns, included columns, why.
COVERING = [
    # The godown's delivery lines, 92,509 of them, had only the clustered key on `id`: no index on the foreign
    # key at all. The executive goods-receipts drill-down rolls every line up by delivery, and that roll-up is
    # 0.081s with this and halves the whole screen, 0.60s to 0.30s.
    #
    # Measured and NOT added beside it: the same index on product_id, and the belief that this one would rescue
    # /warehouse/purchase-summary. That report was 27.2s because it asked in 67 chunks, not because of any index:
    # rewriting it as one query took it to 3.3s, and adding either index moved it to 3.32s, which is nothing. The
    # remaining time is reading 92,509 rows into Python and folding them, which no index touches.
    (
        "idx_wgl_grn", "warehouse_grn_lines",
        ["grn_id"],
        ["product_id", "qty", "bonus_qty", "unit_price", "disc_percent", "tax_rate"],
        "the executive goods-receipts drill-down, which rolls 92,509 delivery lines up by delivery. There was no"
        " index on the foreign key at all, and this halves the screen, 0.60s to 0.30s.",
    ),
    # Every automatic voucher is written through `Voucher.get_or_none(source=..., book=...)`, in three places in
    # vouchers_service, and there was no index on `source` at all: a full scan of 1,110,565 vouchers per document
    # posted. Measured at 34 seconds a voucher, which is why posting head office's 6,332 transfers to the books was
    # going to take days rather than minutes, and why the posting run looked hung rather than slow.
    #
    # `source` leads because it is nearly unique; `book` second because the lookup always names it. `number` and
    # `status` are included so the upsert's own read is answered from the index without touching the table.
    (
        "idx_acc_vouchers_source", "acc_vouchers",
        ["source", "book"],
        ["number", "status", "date", "auto"],
        "every automatic voucher, written through Voucher.get_or_none(source=..., book=...) in three places with no"
        " index on source: a full scan of 1,110,565 vouchers per document, measured at 34 seconds each.",
    ),
    # The executive dashboard folds its figures from these 1,067,857 bills whenever the window carries a time
    # (`executive_service._totals_from_bills`), filtering on the moment rather than the day. The only other index
    # on the table is the unique (branch_id, invoice_number), which leads on the wrong column and cannot help, so
    # without this every timed dashboard scans the lot.
    #
    # `branch_id` leads because a fold either names one branch or sweeps them all, and `at` second is what the
    # range is on. The INCLUDE list is exactly the columns the fold sums, so it never touches the wide row: a bill
    # carries 25 columns and the aggregate reads eight of them.
    (
        "idx_branch_sales_at", "branch_sales",
        ["branch_id", "at"],
        ["day", "gross", "disc_total", "gst", "net_value", "cogs", "cash_back", "party_code"],
        "the executive dashboard folded from the bills when a window carries a time, over 1,067,857 rows whose"
        " only other index leads on invoice_number and cannot serve a range on the moment.",
    ),

    (
        "idx_bps_day_covering", "branch_product_stats",
        ["day"],
        ["product_sku", "product_name", "department", "category", "brand",
         "qty", "net_sales", "cogs", "branch_id"],
        "the dashboard's four heaviest aggregates: top categories, top brands, top products, and sold against unsold."
        " All four filter on a day range and read nothing but these columns.",
    ),
    (
        "idx_avl_voucher", "acc_voucher_lines",
        ["voucher_id"],
        ["account_id", "debit", "credit", "line_no"],
        "joining a voucher to its lines. Tortoise does not index a foreign key, so with 5,343,432 ledger lines every"
        " such join read the whole table: the accounts registers went from seconds to minutes the moment their real"
        " ledger arrived.",
    ),
    (
        "idx_avl_account", "acc_voucher_lines",
        ["account_id"],
        ["voucher_id", "debit", "credit"],
        "the trial balance and every account register, which total debits and credits per account. The other foreign"
        " key on the same table, and unindexed for the same reason.",
    ),
    (
        "idx_branch_sales_day", "branch_sales",
        ["voided", "day", "invoice_number"],
        ["branch_id", "at", "cashier_name", "party_name", "party_code", "counter_name",
         "gross", "disc_total", "gst", "net_value", "cogs", "is_credit_sale"],
        "the three registers read off the bills: Invoice Wise Profit, Item Party Wise Sale Detail and Party Category"
        " Wise Sale. `branch_sales` holds 1,048,837 bills clustered on a random uuid and had no index on `day` at"
        " all, so a one month window read the whole table and then sorted it. A month of Invoice Wise Profit:"
        " 60,028 logical reads (469 MB) before, 1,078 (8 MB) after, the same 47,182 rows -- 56 times less work, and"
        " the sort gone because `voided` is an equality and the two columns after it are the ORDER BY. Equality"
        " first, range last, as with idx_acc_vouchers_posted below. Warm it is 1.11s against 0.73s, which is the"
        " wrong measurement to judge it by: warm pages are nearly free and the reads are what the cold case pays."
        " Invoice Wise Profit was 38.3s on a cold cache and 16.4s on a warm one under load. It costs 198 MB and"
        " took 7 seconds to build.",
    ),
    # The godown's transfer lines, 117,510 of them, with no index on the foreign key -- the same gap as
    # idx_wgl_grn above, from the same cause: Tortoise does not index a foreign key, and warehouse_grn_lines had
    # already been fixed while this one had not. Fetching the lines behind one page of the Transfers board went
    # from 92ms to 12ms, and rolling up a whole branch's transfer lines from 135ms to 87ms. 13 MB, one second to
    # build.
    #
    # Modest in absolute terms, and kept for two reasons beyond the clock. It is the table the branch page's
    # Transfers tab reads, which returned 500 on Garden Town until this morning because Tortoise prefetches with
    # one parameter per parent row and 3,587 is past SQL Server's 2,100 ceiling; that is now a grouped read over
    # exactly this key. And transfer lines grow with every delivery, so the scan only gets longer.
    #
    # Measured and NOT added beside it, both built, timed and dropped again:
    #   * idx_wpol_order on warehouse_purchase_order_lines (purchase_order_id) -- the identical foreign-key gap,
    #     17,148 rows, 2 MB. Saved about 7ms, which is inside the noise. Worth revisiting if purchase orders ever
    #     reach the scale of transfer lines.
    #   * idx_transfers_requested on transfers (requested_at) -- the board orders by it, but 6,421 rows sort in
    #     less time than the round trip takes, so it changed nothing. The branch page's own filter is
    #     `branch_id = ? OR source_branch_id = ?`, which this index cannot serve anyway and which also measured
    #     at nothing.
    (
        "idx_tl_transfer", "transfer_lines",
        ["transfer_id"],
        ["product_id", "qty_sent", "qty_received", "unit_cost"],
        "joining a transfer to its lines, 117,510 of them with no index on the foreign key at all. One page of the"
        " Transfers board went from 92ms to 12ms. It is also the key behind the branch page's Transfers tab, which"
        " answered 500 on Garden Town until it was rewritten as a grouped read over this column.",
    ),
    (
        "idx_acc_vouchers_posted", "acc_vouchers",
        ["status", "book", "date"],
        [],
        "every figure that comes from the books, which all filter `status = 'posted' AND book IN (...) AND date"
        " BETWEEN`. The index the models declare leads with `book`, so the two equality columns could not both be"
        " seeked and 1,101,527 vouchers were read to answer for a handful. Equality first, range last. This is what"
        " made the dashboard's Net Profit tile, and with it the whole Sales section, take 12.5 seconds.",
    ),
]


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


def apply(cursor, statistics: bool = True, indexes: bool = True, say=print) -> dict:
    """Create any missing covering index and rebuild statistics. Safe to call again: an index that exists is left
    alone, and statistics are meant to be rebuilt."""
    made, skipped, refreshed = [], [], 0

    if indexes:
        cursor.execute("SELECT name FROM sys.indexes WHERE name IS NOT NULL")
        have = {r[0] for r in cursor.fetchall()}
        cursor.execute("SELECT name FROM sys.tables")
        tables = {r[0] for r in cursor.fetchall()}
        for name, table, keys, included, _why in COVERING:
            if table not in tables:
                skipped.append(f"{name}: no table {table}")
                continue
            if name in have:
                skipped.append(f"{name}: already there")
                continue
            ddl = (f"CREATE NONCLUSTERED INDEX [{name}] ON [{table}] "
                   f"({', '.join(f'[{c}]' for c in keys)})")
            if included:
                ddl += f" INCLUDE ({', '.join(f'[{c}]' for c in included)})"
            at = time.monotonic()
            cursor.execute(ddl)
            say(f"      created {table}.{name} in {time.monotonic() - at:.0f}s")
            made.append(name)

    if statistics:
        cursor.execute("""
            SELECT t.name FROM sys.tables t JOIN sys.partitions p
                ON p.object_id = t.object_id AND p.index_id IN (0, 1)
             GROUP BY t.name HAVING SUM(p.rows) > 0 ORDER BY SUM(p.rows) DESC
        """)
        every = [r[0] for r in cursor.fetchall()]
        at = time.monotonic()
        for table in every:
            cursor.execute(f"UPDATE STATISTICS [{table}] WITH FULLSCAN")
            refreshed += 1
        say(f"      statistics rebuilt on {refreshed} table(s) in {time.monotonic() - at:.0f}s")

    return {"created": made, "skipped": skipped, "statistics": refreshed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--statistics-only", action="store_true")
    args = ap.parse_args()

    import pyodbc

    cn = pyodbc.connect(target_dsn(), timeout=3600, autocommit=True)
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM sys.indexes WHERE is_primary_key = 0 AND name IS NOT NULL")
    print(f"   {cur.fetchone()[0]} index(es) on {target_dsn().split('DATABASE=')[1].split(';')[0]}\n")

    cur.execute("SELECT name FROM sys.indexes WHERE name IS NOT NULL")
    have = {r[0] for r in cur.fetchall()}
    for name, table, keys, included, why in COVERING:
        state = "there" if name in have else "MISSING"
        print(f"   {name}  on {table} ({', '.join(keys)})  {state}")
        print(f"      including {', '.join(included)}")
        print(f"      for {why}\n")

    if not args.apply:
        print("   Nothing changed. Add --apply.")
        return 0

    print("   applying:")
    summary = apply(cur, statistics=True, indexes=not args.statistics_only, say=print)
    for line in summary["skipped"]:
        print(f"      {line}")
    cn.close()
    print(f"\n   {len(summary['created'])} index(es) created, statistics rebuilt on "
          f"{summary['statistics']} table(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
