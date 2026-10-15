"""Give the branches' legacy purchase orders the approver their own software recorded.

    .venv\\Scripts\\python.exe scripts\\backfill_po_approver.py            what it would do
    .venv\\Scripts\\python.exe scripts\\backfill_po_approver.py --write    do it

`ops/checks/legacy_blank_columns.py` reported `branch_purchase_orders.approved_by_name` null on all 15,681 rows.
It was not blank because their software does not keep it. Their `dbo.PurOrderHeader.PostedBy` is filled on all
14,020 of their orders, and it holds a user ID (2, 20, 42, 1010). The mapper joined it to `dbo.Users.Name`:

    LEFT JOIN "dbo.Users" pb ON pb.Name = h.PostedBy        matched 0 of 14,020
    LEFT JOIN "dbo.Users" pb ON pb.USno = h.PostedBy        matches 14,020 of 14,020

So every branch purchase order read as approved by nobody. Fixed in `ops/legacy-import/map_buying.py`, which is
where every other user join in the file already joined on USno; this was the one that did not. That fix feeds new
and re-mapped orders. This fills the ones already here.

It is the fourth time this check has caught the same shape: a mapper reading a column their software does not
write, or reading the right column the wrong way. The others are in the check's own docstring.

Read-only without `--write`. Only `approved_by_name` is touched, and only where it is null.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

LIVE = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "ops" / "legacy-import" / "staging" / "live.db"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    if not LIVE.is_file():
        print(f"   No fetched copy of their server at {LIVE}.")
        return 1

    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM

    await Tortoise.init(config=TORTOISE_ORM)
    from app.models import BranchPurchaseOrder

    con = sqlite3.connect(f"file:{LIVE.as_posix()}?mode=ro", uri=True)
    approver = {str(r[0]): (r[1] or "").strip() for r in con.execute('''
        SELECT h.POSno, u.Name FROM "dbo.PurOrderHeader" h
        JOIN "dbo.Users" u ON u.USno = h.PostedBy''')}
    con.close()
    print(f"   their server names an approver on {len(approver):,} orders")

    ours = await BranchPurchaseOrder.filter(source="legacy").values_list("id", "po_number", "approved_by_name")
    setting, already, unmatched = [], 0, 0
    for oid, number, name in ours:
        if name:
            already += 1
            continue
        who = approver.get(str(number))
        if not who:
            unmatched += 1
            continue
        setting.append(BranchPurchaseOrder(id=oid, approved_by_name=who[:120]))

    print(f"   {len(ours):,} legacy branch orders here")
    print(f"   {already:,} already name an approver")
    print(f"   {unmatched:,} have no match on their server, so they keep none")
    print(f"   {len(setting):,} would get the approver their software recorded")

    if setting and args.write:
        # 300, not 1000. bulk_update writes a CASE arm per row and an IN list beside it, which is about three
        # parameters a row, and SQL Server refuses a statement with more than 2,100. At 1000 it failed with
        # "COUNT field incorrect", which names neither the limit nor the cause.
        await BranchPurchaseOrder.bulk_update(setting, ["approved_by_name"], batch_size=300)
        done = await BranchPurchaseOrder.filter(source="legacy").exclude(approved_by_name=None).count()
        print(f"\n   written. {done:,} now name an approver.")
    elif setting:
        print("\n   nothing written. Run it again with --write.")

    await Tortoise.close_connections()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
