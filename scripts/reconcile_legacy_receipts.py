"""What was actually delivered against each legacy purchase order, from our own delivery rows.

    python -m scripts.reconcile_legacy_receipts            what it would change
    python -m scripts.reconcile_legacy_receipts --yes      change it

Their software does not record a received quantity on an order line. `PurOrderDetail.QtySatisfied` exists and is the
string '0.0000' on all 4,482 rows of it, the same way `PurchaseHeader.Approved` exists and is never written. What
their software does instead is name the order on the delivery, and their staff read fulfilment off that.

So the mappers carried QtySatisfied faithfully and every one of 298,921 order lines arrived here with nothing
received. Our side reads an order as outstanding when `qty - received_qty` is positive and the status is one of
`purchasing_service.OPEN`, so every legacy order read as awaiting delivery in full. That is the "if all purchase
orders are pending, where was the stock coming from" question, and the answer is that the stock came in on
deliveries we hold, against orders we never joined them to.

This joins them. For each order line it sums the delivery lines for the same item on deliveries that name that
order, and writes that as the received quantity. Then an order whose every line is satisfied reads received, one
with something against it reads partially received, and one with nothing is left exactly as it was.

It reads only our own tables, so it is idempotent and needs nothing from their server: run it twice and the second
run reports no change. That is why it recomputes from zero rather than adding to what is there -- an increment
would double every quantity on a second run, and a nightly stage must be safe to re-run after a failure.

Branch order status is deliberately left alone. Their `Posted` is the authority there and already reads closed on
12,519 of 13,905; it is the quantities that were missing, not the state.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

CHUNK = 400          # 400 ids is 800 parameters in the CASE and the IN; SQL Server allows 2,100 in one statement


def back_up(url: str, label: str) -> str:
    """A full backup before the change, on its own connection.

    It must be its own connection, with autocommit on. BACKUP DATABASE cannot run inside a transaction, and sent
    down Tortoise's pooled connection it does not back anything up and does not raise either: the call returns, the
    script reports success, and `msdb.dbo.backupset` has no new row. That is the worst possible outcome for a
    safety step, so this one asks msdb afterwards and returns what it found rather than what it hoped.
    """
    import re
    import pyodbc

    m = re.match(r"mssql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", url)
    if not m:
        raise SystemExit(f"Cannot read the server out of the database URL: {url[:28]}...")
    user, secret, host, port, database = m.groups()
    name = f"{database}-before-{label}.bak"
    path = r"C:\Program Files\Microsoft SQL Server\MSSQL17.MSSQLSERVER\MSSQL\Backup" + "\\" + name
    cn = pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={host},{port};DATABASE=master;UID={user};PWD={secret};"
        "Encrypt=no;TrustServerCertificate=yes", timeout=3600, autocommit=True)
    try:
        cur = cn.cursor()
        cur.execute(f"BACKUP DATABASE [{database}] TO DISK = ? WITH INIT, COMPRESSION, CHECKSUM, NAME = ?",
                    path, f"before {label}")
        while cur.nextset():                     # BACKUP returns its progress messages as result sets
            pass
        # compressed_backup_size, not backup_size. These backups are taken WITH COMPRESSION, and backup_size
        # is how much data went in, not how big the file is: it reported 15,331 MB for a file of 5,378.
        # Overstating a backup by three times in the one line that says the safety step worked is the wrong
        # direction to be wrong in -- somebody sizing a disk from this would be badly out.
        cur.execute("SELECT TOP 1 s.backup_finish_date, "
                    "       COALESCE(s.compressed_backup_size, s.backup_size)/1048576.0, "
                    "       m.physical_device_name "
                    "FROM msdb.dbo.backupset s JOIN msdb.dbo.backupmediafamily m "
                    "ON m.media_set_id = s.media_set_id WHERE s.database_name = ? "
                    "ORDER BY s.backup_set_id DESC", database)
        row = cur.fetchone()
        if not row or name not in str(row[2]):
            raise SystemExit(f"   The backup did not appear in msdb. Nothing changed. Last there: "
                             f"{row[2] if row else 'nothing'}")
        return f"{row[2].split(chr(92))[-1]}, {row[1]:,.0f} MB, finished {row[0]}"
    finally:
        cn.close()


async def allocate(conn, delivered_sql: str, lines_sql: str) -> dict[str, Decimal]:
    """line id -> quantity delivered against it, shared out when one item sits on several lines of an order.

    Summing per line and joining on the item looks right and is wrong: 370 of their orders list one item on two or
    more lines, and a join credits every one of those lines with the whole delivered quantity. The order then reads
    fully received on twice what arrived. So the delivery total is gathered per (order, item) and poured into that
    order's lines in line order, each taking at most what it asked for.

    Anything left over after every line is full goes on the last line rather than being dropped, because their
    staff do over-deliver and a received quantity that quietly disagrees with the delivery notes is worse than one
    that visibly exceeds the order.
    """
    _, rows = await conn.execute_query(delivered_sql)
    pot = {(str(r["po"]), str(r["item"])): Decimal(str(r["got"] or 0)) for r in rows}
    _, lines = await conn.execute_query(lines_sql)
    grouped: dict[tuple[str, str], list[tuple[str, Decimal]]] = {}
    for r in lines:
        key = (str(r["po"]), str(r["item"]))
        if key in pot:                                   # only orders something was delivered against
            grouped.setdefault(key, []).append((str(r["line_id"]), Decimal(str(r["qty"] or 0))))
    out: dict[str, Decimal] = {}
    for key, members in grouped.items():
        left = pot[key]
        for index, (line_id, wanted) in enumerate(members):
            last = index == len(members) - 1
            take = left if last else min(left, max(wanted, Decimal(0)))
            out[line_id] = take
            left -= take
            if left <= 0 and not last:
                left = Decimal(0)
    return out


async def apply(conn, table: str, got: dict[str, Decimal], write: bool) -> int:
    """Write the quantities, in chunks, as one CASE per chunk rather than one statement per line."""
    ids = [i for i, q in got.items() if q > 0]
    # The sums above are built from legacy deliveries, so every id here belongs to a legacy document. Said out loud
    # because the status update below had to be given the filter explicitly and this one must not lose it quietly.
    if not write:
        return len(ids)
    done = 0
    for part in range(0, len(ids), CHUNK):
        chunk = ids[part:part + CHUNK]
        cases = " ".join(f"WHEN '{i}' THEN {got[i]}" for i in chunk)
        names = ", ".join(f"'{i}'" for i in chunk)
        # `id` is compared as it is, never wrapped in CAST. It used to read
        # `CASE CAST(id AS varchar(64)) ... WHERE CAST(id AS varchar(64)) IN (...)`, and a cast around the column
        # is a cast around every row: the primary key could not be seeked, so each chunk read the whole table.
        # 322,271 branch order lines by about 500 chunks is 161 million rows read to update 200,000, which is why
        # this stage ran for 831 seconds and then sat still. The ids are char(36) and the literals are varchar;
        # both are in the same family, so the comparison is exact and the key is used.
        await conn.execute_query(
            f"UPDATE {table} SET received_qty = CASE id {cases} ELSE received_qty END "
            f"WHERE id IN ({names})")
        done += len(chunk)
    return done


async def run(args) -> int:
    import os
    if args.yes and not args.no_backup:
        url = os.environ.get("DB_URL", "")
        if url.startswith("mssql://"):
            print(f"   backed up: {back_up(url, 'receipt-reconcile')}")
        else:
            print("   not SQL Server, so no backup taken here; back it up yourself before --yes")
    await Tortoise.init(config=TORTOISE_ORM)
    conn = Tortoise.get_connection("default")
    try:
        # A branch delivery names its order in `po_number`, in the same branch. '0' means no order, which is most of
        # them: their staff receive stock without raising an order first.
        branch = await allocate(conn, """
            SELECT o.id AS po, gl.product_sku AS item, SUM(gl.qty) AS got
              FROM branch_purchase_orders o
              JOIN branch_grns g ON g.branch_id = o.branch_id AND g.po_number = o.po_number
              JOIN branch_grn_lines gl ON gl.grn_id = g.id
             WHERE o.po_number NOT IN ('0', '')
             GROUP BY o.id, gl.product_sku
        """, """
            SELECT id AS line_id, purchase_order_id AS po, product_sku AS item, qty
              FROM branch_purchase_order_lines ORDER BY purchase_order_id, product_sku, line_no, id
        """)
        # A godown delivery carries the order as a foreign key, so no string match is needed.
        godown = await allocate(conn, """
            SELECT g.purchase_order_id AS po, gl.product_id AS item, SUM(gl.qty) AS got
              FROM warehouse_grns g
              JOIN warehouse_grn_lines gl ON gl.grn_id = g.id
             WHERE g.purchase_order_id IS NOT NULL
             GROUP BY g.purchase_order_id, gl.product_id
        """, """
            SELECT id AS line_id, purchase_order_id AS po, product_id AS item, qty
              FROM warehouse_purchase_order_lines ORDER BY purchase_order_id, product_id, position, id
        """)
        # Counted, not quoted. These read "of 282,004-ish" and "of 16,917-ish" until a reload of the buying documents
        # recovered 4,483 more deliveries for Garden Town alone and the lines went to 320,536, so the report was
        # telling whoever ran it that half the lines did not exist.
        from app.models import BranchPurchaseOrderLine, PurchaseOrderLine

        branch_total = await BranchPurchaseOrderLine.all().count()
        godown_total = await PurchaseOrderLine.all().count()
        print(f"   branch order lines with a delivery against them   {sum(1 for q in branch.values() if q > 0):>7,}"
              f"  of {branch_total:,}")
        print(f"   godown order lines with a delivery against them   {sum(1 for q in godown.values() if q > 0):>7,}"
              f"  of {godown_total:,}")

        if not args.yes:
            print("\n   Nothing written. Add --yes.")
            return 0

        n = await apply(conn, "branch_purchase_order_lines", branch, True)
        m = await apply(conn, "warehouse_purchase_order_lines", godown, True)
        print(f"\n   {n:,} branch line(s) and {m:,} godown line(s) now carry what came in")

        # Status follows the quantities, for the godown only. `cancelled` is a decision somebody made and is never
        # overwritten here; `draft` with nothing against it stays draft.
        _, before = await conn.execute_query(
            "SELECT status v, COUNT(*) n FROM warehouse_purchase_orders GROUP BY status")
        # `closed`, not `partially_received`, where something arrived but not every line was matched. Their
        # software posts an order and is done with it; the shortfall is our item-level matching, not an outstanding
        # commitment. `partially_received` sits in purchasing_service.OPEN, so using it kept 478 orders in the late
        # notice and in `on_order` -- the same fault as carrying `approved`, just one step along.
        #
        # And only legacy rows. Without the source filter this would restyle a purchase order somebody at D.Marina
        # raised this morning, on the strength of a legacy delivery that happened to share a number.
        await conn.execute_query("""
            UPDATE o SET status = CASE
                     WHEN t.short = 0 THEN 'received'
                     WHEN t.got > 0 THEN 'closed'
                     ELSE o.status END
              FROM warehouse_purchase_orders o
              JOIN (SELECT ol.purchase_order_id AS po,
                           SUM(CASE WHEN ol.received_qty >= ol.qty THEN 0 ELSE 1 END) AS short,
                           SUM(ol.received_qty) AS got
                      FROM warehouse_purchase_order_lines ol
                     GROUP BY ol.purchase_order_id) t ON t.po = o.id
             WHERE o.source = 'legacy' AND o.status NOT IN ('cancelled', 'rejected')
        """)
        # The ones no delivery of theirs cites at all. Posted or not, an order from September 2024 is not something
        # anybody is going to chase, and leaving it in OPEN is what produced the overdue notice in the first place.
        await conn.execute_query("""
            UPDATE warehouse_purchase_orders SET status = 'closed'
             WHERE source = 'legacy' AND status IN ('approved', 'pending_approval', 'partially_received')
        """)
        _, after = await conn.execute_query(
            "SELECT status v, COUNT(*) n FROM warehouse_purchase_orders GROUP BY status")
        was = ", ".join(f"{r['v']}={r['n']:,}" for r in before)
        now = ", ".join(f"{r['v']}={r['n']:,}" for r in after)
        print(f"   godown order status was  {was}")
        print(f"   godown order status now  {now}")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="for a run inside the nightly, which has already backed the database up")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
