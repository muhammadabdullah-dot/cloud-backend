"""Head office's godown stock position, from their own stock snapshot.

    python -m scripts.load_legacy_godown_stock                  what it would load
    python -m scripts.load_legacy_godown_stock --yes            load it
    python -m scripts.load_legacy_godown_stock --source <db>    a different staging copy

The Accounts dashboard compares what the books say head office holds in stock against what the godown actually
holds, and the second number came from `warehouse_stock_movements`, which had no rows at all. So the check read
"books say Rs 8,233,874, godown holds Rs 0.00" and showed as an integrity error on every visit. It was not
comparing anything: one side had never been loaded.

Their snapshot is `dbo.StockReport`, one row per godown and item, with the quantity and the average cost. Their six
godowns, at cost:

    GSno 1  GARDEN TOWN       Rs 48,584,739   a shop's own stock, which belongs to the branch
    GSno 2  HEADOFFICE        Rs  2,982,709   head office
    GSno 3  NEW HEADOFFICE    Rs  6,921,345   head office
    GSno 4  GULGASHT          Rs 27,589,809   a shop
    GSno 5  MODELTOWN         Rs 41,224,630   a shop
    GSno 6  FORTCOLONY        Rs  6,815,361   a shop

Only 2 and 3 are head office's, which `dbo.Godown` says plainly: both carry BranchSno 3. Together they are
Rs 9,904,053, and that is the figure the dashboard should be holding the books to.

Their `dbo.GodownWiseStock` looks like the obvious source and is not: it reports three godowns only, and its
HEADOFFICE and GT WAREHOUSE columns are zero on all 40,680 rows. StockReport is the one their software fills.

WHY THESE ARE `receive` MOVEMENTS AND NOT A COUNT
-------------------------------------------------
`accounts_posting_service` posts `count-correction` movements to the books as a stock gain or loss. Loading a
position as corrections would post Rs 9.9 million of "found in count" income on top of the stock the deliveries
already put there, so the books would be wrong in a new and larger way. `receive` is not posted, so the godown gets
its quantities and the books are left exactly as the documents made them.

That is the point of the check: the two sides are built independently, and comparing them is only useful while they
stay independent. A difference is a question for somebody, not something to paper over here.

It replaces its own rows each run, keyed on `reason`, so running it twice leaves one position rather than two.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

HERE = pathlib.Path(__file__).resolve().parent
STAGING = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging" / "live.db"
LEGACY_BIN_ID = "bin-legacy-unplaced"
# Their GSno for head office's own godowns. Both carry BranchSno 3 in dbo.Godown, which is head office.
HEAD_OFFICE_GODOWNS = (2, 3)
REASON = "legacy:godown-position"
ZERO = Decimal("0")


def staged(source: pathlib.Path) -> list[tuple[str, Decimal, Decimal]]:
    """(sku, qty, avg cost) per item, their two head office godowns added together."""
    if not source.exists():
        raise SystemExit(f"No staging copy at {source}. Run ops/legacy-import/fetch_live.py first.")
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        marks = ", ".join("?" * len(HEAD_OFFICE_GODOWNS))
        # Summed per item across the two godowns, because our godown is one place. Weighted average cost, so the
        # value is the same whether it is totalled here or recomputed from qty times cost downstream.
        rows = conn.execute(f"""
            SELECT CAST(ISno AS TEXT) AS sku,
                   SUM(CAST(Stock AS REAL)) AS qty,
                   SUM(CAST(Stock AS REAL) * CAST(AvgPrice AS REAL)) AS value
              FROM "dbo.StockReport"
             WHERE GSno IN ({marks})
             GROUP BY CAST(ISno AS TEXT)
            HAVING SUM(CAST(Stock AS REAL)) <> 0
        """, list(HEAD_OFFICE_GODOWNS)).fetchall()
    finally:
        conn.close()
    out = []
    for sku, qty, value in rows:
        q = Decimal(str(qty or 0))
        cost = (Decimal(str(value or 0)) / q) if q else ZERO
        out.append((sku, q, cost))
    return out


async def run(args) -> int:
    position = staged(pathlib.Path(args.source))
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Bin, Product, StockMovement

        known = {str(pid) for pid in await Product.all().values_list("id", flat=True)}
        placed = [(sku, qty, cost) for sku, qty, cost in position if sku in known]
        unknown = len(position) - len(placed)
        value = sum((qty * cost for _, qty, cost in placed), ZERO)
        negative = sum(1 for _, qty, _ in placed if qty < 0)

        print(f"   their head office godowns hold {len(position):,} item(s)")
        print(f"   of those, in our product list   {len(placed):,}" + (f"   ({unknown:,} not, skipped)" if unknown else ""))
        print(f"   at their average cost           Rs {value:,.2f}")
        if negative:
            print(f"   {negative:,} item(s) at a negative quantity, loaded as they are: their figure, not ours")
        already = await StockMovement.filter(reason=REASON).count()
        if already:
            print(f"   replacing the {already:,} row(s) a previous run of this script left")

        if not args.yes:
            print("\n   Nothing written. Add --yes.")
            return 0

        spot = await Bin.get_or_none(id=LEGACY_BIN_ID)
        if spot is None:
            print(f"   No {LEGACY_BIN_ID} bin. Run scripts/load_legacy_godown.py first; it makes that bin.")
            return 2

        await StockMovement.filter(reason=REASON).delete()
        at = datetime.now(timezone.utc)
        made = 0
        batch: list = []
        for sku, qty, cost in placed:
            batch.append(StockMovement(
                id=uuid.uuid4(), product_id=sku, bin_id=LEGACY_BIN_ID, kind="receive", qty=qty,
                # No user: nobody here caused this, it is their position as at the snapshot.
                origin_user=None, at=at, unit_cost=cost, reason=REASON,
            ))
            if len(batch) >= 1000:
                await StockMovement.bulk_create(batch, batch_size=500)
                made += len(batch)
                batch = []
        if batch:
            await StockMovement.bulk_create(batch, batch_size=500)
            made += len(batch)
        print(f"\n   {made:,} movement(s) written. The godown now holds a position for the dashboard to check.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(STAGING))
    ap.add_argument("--yes", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
