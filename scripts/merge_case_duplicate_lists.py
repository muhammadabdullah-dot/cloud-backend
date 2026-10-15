"""Merge lookup-list entries that are the same value typed in different letter case, and the products that use them.

    python scripts/merge_case_duplicate_lists.py            what it would merge
    python scripts/merge_case_duplicate_lists.py --yes       merge it

WHAT THIS IS FIXING
-------------------
`item_list_entries` holds the departments, categories, brands, manufacturers and units that the Item screens offer.
It was derived from Multi-Tec's 67,804 Items, and their data carries 28 brands spelled two ways: DOVE with 411
products and Dove with 2, NESTLE with 147 and Nestle with 1, and so on for 28 pairs. SQLite compares text byte for
byte, so both spellings sat in the list as separate brands and the brand picker showed each of them twice.

It surfaced because SQL Server's collation is case insensitive, so the unique rule on (kind, code) refused the 28
second spellings. That makes it a migration blocker as well as a visible defect, and the migration is the reason it
is being fixed now rather than later.

HOW THE WINNER IS CHOSEN, AND WHY IT IS NOT A COIN TOSS
-------------------------------------------------------
The spelling more products already use wins. In every one of the 28 pairs that is decisive rather than close: the
smallest margin is 6 products against 2, and most are a hundred or more against one. So the merge moves a handful of
products onto the spelling their hundreds of siblings already carry, which is what somebody doing this by hand would
do.

Products are updated **before** the losing entry is deleted, because the point is to keep every product's brand
pointing at a brand that is still in the list. Both happen in one transaction, so a failure leaves neither.

WHY THE PRODUCTS HAVE TO CHANGE TOO
-----------------------------------
`item_lists_service.sync_list` rebuilds these lists from the products themselves. Deleting a duplicate entry without
fixing the product that uses it would put the duplicate straight back on the next sync, so fixing only the list is
not a fix at all.

A kind with duplicates that this cannot trace to a product column is reported and left alone rather than guessed at.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sqlite3
import sys
import time

SQLITE_DB = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\fresh-test\headoffice\db\cloud.db")

# Which column on a product holds each kind of list value. A kind absent from here cannot be merged safely, because
# the products using the losing spelling could not be moved, and the merge would lose that information.
ON_PRODUCT = {
    "department": "department",
    "category": "category",
    "brand": "brand",
    "manufacturer": "manufacturer",
    "unit": "unit",
    "pack-unit": "pack_unit",
}


def groups(db: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    """Every (kind, folded code) that has more than one spelling."""
    folded: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for kind, code in db.execute('SELECT "kind", "code" FROM "item_list_entries"'):
        folded[(kind, (code or "").lower())].append(code)
    return {key: spellings for key, spellings in folded.items() if len(spellings) > 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--source", default=str(SQLITE_DB))
    ap.add_argument("--no-backup", action="store_true", help="only for a database that is already a copy")
    args = ap.parse_args()

    path = pathlib.Path(args.source)
    if not path.exists():
        raise SystemExit(f"No database at {path}")

    db = sqlite3.connect(path if args.yes else f"file:{path}?mode=ro", uri=not args.yes)
    clashes = groups(db)
    if not clashes:
        print("   Every list entry is distinct even ignoring letter case. Nothing to merge.")
        return 0

    unmapped = sorted({kind for kind, _low in clashes if kind not in ON_PRODUCT})
    plan: list[tuple[str, str, list[str], int, dict[str, int]]] = []
    for (kind, _low), spellings in sorted(clashes.items()):
        if kind not in ON_PRODUCT:
            continue
        column = ON_PRODUCT[kind]
        # How many products use each spelling. `=` in SQLite is case sensitive for ASCII, so these counts are per
        # spelling and not per folded value, which is exactly what deciding a winner needs.
        used = {s: db.execute(f'SELECT COUNT(*) FROM "products" WHERE "{column}" = ?', (s,)).fetchone()[0]
                for s in spellings}
        winner = max(spellings, key=lambda s: (used[s], s))
        losers = [s for s in spellings if s != winner]
        moving = sum(used[s] for s in losers)
        plan.append((kind, winner, losers, moving, used))

    print(f"   {path}")
    print(f"   {len(plan)} list value(s) spelled more than one way, {sum(p[3] for p in plan)} product(s) to move\n")
    print(f"   {'kind':<13} {'keeping':<22} {'used':>6}    {'removing':<22} {'used':>6}")
    for kind, winner, losers, _moving, used in plan:
        for n, loser in enumerate(losers):
            keep = f"{winner!r}" if n == 0 else ""
            count = f"{used[winner]:,}" if n == 0 else ""
            print(f"   {kind if n == 0 else '':<13} {keep:<22} {count:>6}    {loser!r:<22} {used[loser]:>6,}")
    if unmapped:
        print(f"\n   *** {len(unmapped)} kind(s) have duplicates but no product column to move them to, so they are")
        print(f"       left exactly as they are: {', '.join(unmapped)}")

    if not args.yes:
        print("\n   Nothing changed. Add --yes.")
        return 0

    if not args.no_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-before-case-merge-{stamp}")
        print(f"\n   backing up to {backup.name} first ...", flush=True)
        started = time.monotonic()
        db.execute("VACUUM INTO ?", (str(backup),))
        print(f"   {backup.stat().st_size/1e9:.2f} GB in {time.monotonic()-started:,.0f}s", flush=True)

    moved_products = 0
    removed_entries = 0
    db.execute("BEGIN")
    try:
        for kind, winner, losers, _moving, _used in plan:
            column = ON_PRODUCT[kind]
            for loser in losers:
                cur = db.execute(f'UPDATE "products" SET "{column}" = ? WHERE "{column}" = ?', (winner, loser))
                moved_products += cur.rowcount
                cur = db.execute('DELETE FROM "item_list_entries" WHERE "kind" = ? AND "code" = ?', (kind, loser))
                removed_entries += cur.rowcount
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    left = groups(db)
    still = {k: v for k, v in left.items() if k[0] in ON_PRODUCT}
    print(f"\n   {moved_products} product(s) moved onto the kept spelling, {removed_entries} list entry(ies) removed.")
    print(f"   {(db.execute('SELECT COUNT(*) FROM item_list_entries').fetchone()[0]):,} list entry(ies) remain.")
    if still:
        print(f"   *** {len(still)} mergeable duplicate(s) still present, which should not happen.")
        return 1
    print("   No list value is spelled two ways any more.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
