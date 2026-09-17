"""Land a branch's figures, stock list and books at head office from a file, exactly as its branch server's sync would.

    python -m scripts.load_branch_story --file <scratch>/fc-story.json            # dry run: says what it would do
    python -m scripts.load_branch_story --file <scratch>/fc-story.json --yes      # writes it (back up cloud.db first)

For a branch with no branch server of its own (Fort Colony), whose trading was built on a copy by
`branch-server/scripts/story_fort_colony.py`. The same head office code a real sync runs does the work:
snapshot_service.apply_aggregates for the figures, begin_stock / add_stock_chunk / complete_stock for the stock list, and
registration_service.ingest for the events (the books, and who did what). The branch stays a branch without its own
server, so nothing is ever sent down to it. It refuses to run twice for a branch that already has figures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Account, Branch, BranchDailyStat, SyncInboxEvent, Voucher
        from app.services import registration_service, snapshot_service

        branch = await Branch.get_or_none(code=data["branch"])
        if not branch:
            raise SystemExit(f"No branch {data['branch']} at head office.")
        # An empty books settings row is made just by opening a branch's books, so it doesn't count as having books.
        if (await BranchDailyStat.filter(branch=branch).exists() or await Account.filter(book=branch.code).exists()
                or await Voucher.filter(book=branch.code).exists()):
            raise SystemExit(f"{branch.name} already has figures or books at head office. Restore the backup from before to load it again.")
        aggregates, stock, events = data["aggregates"], data["stock"], data["events"]
        print(f"  {branch.name}: {len(aggregates.get('daily', []))} trading days, {len(aggregates.get('products', []))} Item-days, "
              f"{len(stock)} stock rows, {len(events)} events")
        if not args.yes:
            print("  Dry run: nothing written. Back up cloud.db, then run with --yes.")
            return

        summary = await snapshot_service.apply_aggregates(branch, aggregates, source="sync")
        print(f"  figures: {summary}")
        snapshot_id = snapshot_service.new_snapshot_id() if hasattr(snapshot_service, "new_snapshot_id") else __import__("uuid").uuid4().hex
        await snapshot_service.begin_stock(branch, snapshot_id)
        received = 0
        for start in range(0, len(stock), 2000):
            received += await snapshot_service.add_stock_chunk(branch, snapshot_id, stock[start:start + 2000])
        done = await snapshot_service.complete_stock(branch, snapshot_id)
        print(f"  stock list: {received} rows, {done}")
        accepted = duplicates = 0
        for start in range(0, len(events), 300):
            run, _ids = await registration_service.ingest(branch, events[start:start + 300])
            accepted += run.accepted_count
            duplicates += run.duplicate_count
        failed = await SyncInboxEvent.filter(branch=branch, status="failed").values_list("aggregate_type", "apply_error")
        print(f"  events: {accepted} accepted, {duplicates} already here, {len(failed)} head office couldn't apply")
        for kind, error in failed[:10]:
            print(f"    {kind}: {error}")
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
