"""Land a branch's figures, stock list and books at head office from a file, exactly as its branch server's sync would.

    python -m scripts.load_branch_story --file <scratch>/fc-story.json            # dry run: says what it would do
    python -m scripts.load_branch_story --file <scratch>/fc-story.json --yes      # writes it (back up cloud.db first)
    python -m scripts.load_branch_story --file <scratch>/fc-story.json --yes --replace   # loaded before: its old copy goes first

For a branch with no branch server of its own (Fort Colony), whose trading was built on a copy by
`branch-server/scripts/story_fort_colony.py`. The same head office code a real sync runs does the work:
snapshot_service.apply_aggregates for the figures, begin_stock / add_stock_chunk / complete_stock for the stock list, and
registration_service.ingest for the events (the books). The branch stays a branch without its own server, so nothing is
ever sent down to it.

When the file says how head office opened the branch (`headOffice`), head office's own book records its side once: the
owner's added capital into head office's bank, and the money and fit-out paid for the branch against the branch's current
account, so the two books' accounts with each other agree.

It refuses a branch that already has figures unless --replace, and --replace refuses a branch with its own server: a real
branch's books are its own and are never overwritten from a file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from decimal import Decimal
from pathlib import Path

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM


async def head_office_side(branch, plan: dict) -> None:
    from app.models import Account, User, Voucher
    from app.services import vouchers_service

    reference = plan["reference"]
    if await Voucher.filter(book="HO", reference_no=reference).exists():
        print(f"  head office's side ({reference}) is already in its books")
        return
    user = await User.filter(role_id="executive", active=True).first() or await User.filter(role_id="system-admin", active=True).first()
    bank = await Account.get(book="HO", system_key="bank.main")
    capital = await Account.get(book="HO", system_key="equity.capital")
    current = await Account.get(book="HO", system_key=f"interoffice.branch.{branch.code}")
    day = plan["day"]
    owner = Decimal(plan["ownerCapital"])
    if owner > 0:
        draft = await vouchers_service.create_draft(user, {
            "vtype": "BRV", "date": day, "headerAccountId": str(bank.id), "referenceNo": f"{reference}-CAPITAL",
            "description": f"Owner's added capital to open {branch.name}",
            "lines": [{"accountId": str(capital.id), "debit": "0", "credit": str(owner), "description": f"Owner's capital for {branch.name}"}]})
        await vouchers_service.post(user, str(draft.id))
    lines = [{"accountId": str(current.id), "debit": row["amount"], "credit": "0", "description": row["text"]} for row in plan["funding"]]
    draft = await vouchers_service.create_draft(user, {
        "vtype": "BPV", "date": day, "headerAccountId": str(bank.id), "referenceNo": reference,
        "description": f"Head office opens {branch.name}: money, fit-out and the rent deposit", "lines": lines})
    posted = await vouchers_service.post(user, str(draft.id))
    total = sum((Decimal(r["amount"]) for r in plan["funding"]), Decimal("0"))
    print(f"  head office's side: owner's capital Rs {owner:,.0f}; {posted.number} pays Rs {total:,.0f} into {current.name}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--replace", action="store_true", help="take out this branch's earlier copy (figures and books) first")
    args = parser.parse_args()
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Account, Branch, BranchDailyStat, SyncInboxEvent, Voucher, VoucherLine
        from app.services import registration_service, snapshot_service

        branch = await Branch.get_or_none(code=data["branch"])
        if not branch:
            raise SystemExit(f"No branch {data['branch']} at head office.")
        # An empty books settings row is made just by opening a branch's books, so it doesn't count as having books.
        loaded = (await BranchDailyStat.filter(branch=branch).exists() or await Account.filter(book=branch.code).exists()
                  or await Voucher.filter(book=branch.code).exists())
        if loaded and not args.replace:
            raise SystemExit(f"{branch.name} already has figures or books at head office. Use --replace to load it again.")
        if args.replace and branch.verified_at is not None:
            raise SystemExit(f"{branch.name} has its own branch server: its figures and books come from it, never from a file.")
        aggregates, stock, events = data["aggregates"], data["stock"], data["events"]
        print(f"  {branch.name}: {len(aggregates.get('daily', []))} trading days, {len(aggregates.get('products', []))} Item-days, "
              f"{len(stock)} stock rows, {len(events)} events")
        if not args.yes:
            print("  Dry run: nothing written. Back up cloud.db, then run with --yes.")
            return

        if args.replace:
            vouchers = list(await Voucher.filter(book=branch.code).values_list("id", flat=True))
            await VoucherLine.filter(voucher_id__in=vouchers).delete()
            removed = await Voucher.filter(book=branch.code).delete()
            await SyncInboxEvent.filter(branch=branch).delete()
            print(f"  earlier copy taken out: {removed} vouchers and the events they came in")

        summary = await snapshot_service.apply_aggregates(branch, aggregates, source="sync")
        print(f"  figures: {summary}")
        snapshot_id = uuid.uuid4().hex
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
        if data.get("headOffice"):
            await head_office_side(branch, data["headOffice"])
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
