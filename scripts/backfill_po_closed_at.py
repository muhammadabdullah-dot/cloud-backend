"""Give the godown's legacy purchase orders the closing date their own software recorded.

    .venv\\Scripts\\python.exe scripts\\backfill_po_closed_at.py            what it would do
    .venv\\Scripts\\python.exe scripts\\backfill_po_closed_at.py --write    do it

WHY THIS EXISTS AS A ONE-OFF
----------------------------
`ops/checks/legacy_blank_columns.py` reported `warehouse_purchase_orders.closed_at` null on all 863 godown orders.
That is a field on the Purchase Orders screen that is blank however many orders anybody opens, and it is not blank
because their software does not keep it: their `dbo.PurOrderHeader.PostDate` is filled on all 14,019 of their
orders, and 547 of ours are closed.

Two separate things were wrong, and this fixes only the second.

  * The LOADER skipped an order that was already here, whole, the moment its lines were complete, so a header
    field the mapper learned to send later never reached it. That is why `approved_at` is filled on 807 and
    `closed_at` on none, from the SAME column of theirs. Fixed in `scripts/load_legacy_godown.py`, which now
    rewrites a header that has moved on and says so in the nightly log.
  * These 863 orders will not be reached by that fix, because nothing regenerates them: their payload is
    `staging/payload-HO-buying.json.stale-no-mapper-makes-this`, named by whoever retired it. They came from a
    one-off historical load, so they need a one-off repair. This is it.

THE RULE, AND WHY IT IS NARROWER THAN THE MAPPER'S
--------------------------------------------------
The per-shop mapper sets `closedAt` to `PostDate` whenever the order is not cancelled -- including while it is
still open. Their PostDate is filled on orders they have not posted (`Posted` = 'N'), so copying it straight
across would stamp a closing date on an order nobody has closed, which reads worse on screen than a blank.

So: a closing date only where the order IS closed, which is where their `Posted` says Y. Our own statuses say the
same thing from this side, and both have to agree before anything is written -- an order we call closed that they
call unposted is a disagreement worth leaving alone and looking at, not worth papering over with a date.

Read-only without `--write`. Nothing is deleted and nothing but `closed_at` is touched, on rows where it is null.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

LIVE = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "ops" / "legacy-import" / "staging" / "live.db"
# Our statuses that mean the order is finished with. A draft or an open order has no closing date by definition.
FINISHED = {"closed", "received", "cancelled"}


def at_of(value) -> datetime | None:
    text = str(value or "").replace("T", " ")[:19]
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="actually write the dates")
    args = parser.parse_args()

    if not LIVE.is_file():
        print(f"   No fetched copy of their server at {LIVE}. Run the nightly fetch first.")
        return 1

    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM

    await Tortoise.init(config=TORTOISE_ORM)
    from app.models import PurchaseOrder

    ours = await PurchaseOrder.filter(source="legacy").values_list("id", "po_number", "status", "closed_at")
    con = sqlite3.connect(f"file:{LIVE.as_posix()}?mode=ro", uri=True)
    theirs = {str(r[0]): r for r in con.execute(
        'SELECT POSno, Posted, PostDate, OrderCancelStatus, OrderCancelDate FROM "dbo.PurOrderHeader"')}
    con.close()

    setting, already, not_finished, disagree, unmatched = [], 0, 0, [], 0
    for oid, number, status, closed_at in ours:
        row = theirs.get(str(number))
        if row is None:
            unmatched += 1
            continue
        posted = str(row[1] or "").strip() in ("Y", "y", "1")
        cancelled = str(row[3] or "").strip() in ("Y", "y", "1")
        if closed_at is not None:
            already += 1
            continue
        if str(status) not in FINISHED:
            not_finished += 1
            continue
        if not posted and not cancelled:
            disagree.append((number, status))
            continue
        when = at_of(row[4]) if cancelled else at_of(row[2])
        if when is not None:
            setting.append(PurchaseOrder(id=oid, closed_at=when))

    print()
    print(f"   {len(ours):,} legacy orders here, {unmatched:,} with no match on their server")
    print(f"   {already:,} already have a closing date")
    print(f"   {not_finished:,} are not finished with (draft or open), so they correctly have none")
    print(f"   {len(disagree):,} we call finished and they have not posted -- left alone")
    for number, status in disagree[:5]:
        print(f"        {number}  ours says {status}, theirs says not posted")
    print(f"   {len(setting):,} would get the closing date their software recorded")

    if setting and args.write:
        await PurchaseOrder.bulk_update(setting, ["closed_at"], batch_size=500)
        left = await PurchaseOrder.filter(source="legacy", closed_at__isnull=True).count()
        done = await PurchaseOrder.filter(source="legacy", closed_at__isnull=False).count()
        print()
        print(f"   written. {done:,} now carry a closing date, {left:,} still null")
    elif setting:
        print()
        print("   nothing written. Run it again with --write.")

    await Tortoise.close_connections()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
