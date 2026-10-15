"""Their post-dated cheques into `ho_cheques`, with the invoices each one covers.

    python -m scripts.load_legacy_cheques                   what it would load
    python -m scripts.load_legacy_cheques --yes              load it
    python -m scripts.load_legacy_cheques --source <db>      a different staging copy

The table was designed for this data and then never filled, which is why the Cheques screen was empty: the screen
was right and had nothing to show. `cheques.py` even cites the counts it was shaped around.

`source` is `legacy:<their InvSno>` and unique, so running this twice updates rather than doubles.

THEIR COLUMNS ONTO OURS
-----------------------
    InvSno          number, and the key behind `source`
    BranchSno       book, through the same 1+6=GT mapping the rest of the pipeline uses
    ChequeNo        cheque_no. Often blank on theirs; kept blank rather than invented.
    TotalAmount     amount
    RecordDate      received_on, when it was written down
    DueDate         cheque_date, the date on the face, which is what makes it post dated
    finalizedate    cleared_on
    CancelDate      cancelled_on
    DebitAccSno     party_account -- the supplier being paid
    CreditAccSno    bank_account -- the account it is drawn on
    Post, Finalize  posted, finalized. Kept apart because their register asks the two questions separately.
    ReferenceNo     ref_no_1
    Remarks         note

Their account numbers are our `acc_accounts.code`, per book. An account we cannot resolve is a cheque we do not
load, counted and reported rather than loaded against the wrong party -- a cheque pointing at the wrong supplier
is worse than a cheque that is missing.

This is PostDateChequePayble: every one is money going out, so `direction` is `issued` throughout. If a
receivable equivalent turns up in their schema it needs its own pass, not a flag here.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sqlite3
import sys
from datetime import date
from decimal import Decimal

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

HERE = pathlib.Path(__file__).resolve().parent
STAGING = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging" / "live.db"
BOOK_OF = {"1": "GT", "6": "GT", "2": "GG", "3": "HO", "4": "MT", "5": "FC"}


def yes(value) -> bool:
    """Their way of saying yes. They write 'Y' and 'N', not 1 and 0.

    This tested for "1"/"True" only, so Post and Finalize came back False on all 4,055 cheques while their data
    says Y on 3,980 and 3,954. The Cheques screen asks those two questions separately and was answering no to both
    for every cheque ever written.
    """
    return str(value or "").strip().upper() in ("Y", "YES", "1", "TRUE", "T")


def day(value) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def money(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def staged(source: pathlib.Path) -> tuple[list[dict], dict[str, list[dict]]]:
    if not source.exists():
        raise SystemExit(f"No staging copy at {source}. Run ops/legacy-import/fetch_live.py first.")
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        heads = [dict(r) for r in conn.execute('SELECT * FROM "dbo.PostDateChequePaybleHeader"')]
        lines: dict[str, list[dict]] = {}
        for row in conn.execute('SELECT * FROM "dbo.PostDateChequePaybleDetail" ORDER BY InvSno, RowId'):
            lines.setdefault(str(row["InvSno"]), []).append(dict(row))
        return heads, lines
    finally:
        conn.close()


async def run(args) -> int:
    heads, lines = staged(pathlib.Path(args.source))
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Account, Cheque, ChequeLine

        # Their AccSno is NOT our code. Our code is their `Code` column, which is a different number and is not
        # even unique on their side -- 201 of their accounts share the code '0'. The books payload carries the
        # pairing the chart loader actually used (`theirAccSno` -> `code`), so the join goes through that.
        # Matching AccSno against code directly left 1,836 of 4,054 cheques unable to find their supplier.
        payload_dir = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging"
        code_of: dict[str, str] = {}
        for book_file in sorted(payload_dir.glob("payload-*-books.json")):
            import json
            for acc in json.loads(book_file.read_text(encoding="utf-8")).get("accounts", []):
                if acc.get("theirAccSno") is not None:
                    code_of[str(acc["theirAccSno"])] = acc["code"]
        if not code_of:
            raise SystemExit("No books payload in staging. Run ops/legacy-import/map_books.py first.")

        by_code: dict[tuple[str, str], str] = {}
        for acc in await Account.all().only("id", "book", "code"):
            by_code[(acc.book, str(acc.code))] = str(acc.id)

        def account_for(book: str, their_sno) -> str | None:
            code = code_of.get(str(their_sno))
            return by_code.get((book, code)) if code else None

        existing = {c.source: c for c in await Cheque.filter(source__startswith="legacy:")}

        plan, no_book, no_party = [], 0, 0
        for head in heads:
            book = BOOK_OF.get(str(head.get("BranchSno")))
            if not book:
                no_book += 1
                continue
            party = account_for(book, head.get("DebitAccSno"))
            if not party:
                no_party += 1
                continue
            received = day(head.get("RecordDate"))
            due = day(head.get("DueDate")) or received
            if not received:
                continue
            cancelled = day(head.get("CancelDate"))
            cleared = day(head.get("finalizedate"))
            status = "cancelled" if (cancelled or yes(head.get("Cancel"))) \
                else "cleared" if cleared else "pending"
            plan.append({
                "source": f"legacy:{head['InvSno']}", "book": book, "number": str(head["InvSno"])[:30],
                "direction": "issued", "party_account_id": party,
                "bank_account_id": account_for(book, head.get("CreditAccSno")),
                "cheque_no": str(head.get("ChequeNo") or "").strip()[:30],
                "amount": money(head.get("TotalAmount")), "received_on": received, "cheque_date": due,
                "cleared_on": cleared, "cancelled_on": cancelled, "status": status,
                "posted": yes(head.get("Post")),
                "finalized": yes(head.get("Finalize")),
                "ref_no_1": (str(head.get("ReferenceNo") or "").strip() or None),
                "note": (str(head.get("Remarks") or "").strip() or None),
                "created_by_name": (str(head.get("CreatedBy") or "").strip() or None),
                "lines": lines.get(str(head["InvSno"]), []),
            })

        by_book: dict[str, int] = {}
        for p in plan:
            by_book[p["book"]] = by_book.get(p["book"], 0) + 1
        print(f"   their cheques        {len(heads):,}")
        print(f"   already loaded       {len(existing):,}")
        print(f"   would load           {len(plan):,}   ({sum(len(p['lines']) for p in plan):,} invoice lines)")
        if no_book:
            print(f"   skipped, unknown branch        {no_book}")
        if no_party:
            print(f"   skipped, supplier not in our chart  {no_party}")
        print("   by book: " + ", ".join(f"{b}={n:,}" for b, n in sorted(by_book.items())))
        print("   by status: " + ", ".join(
            f"{s}={sum(1 for p in plan if p['status'] == s):,}" for s in ("pending", "cleared", "cancelled")))

        if not args.yes:
            print("\n   Nothing written. Add --yes.")
            return 0

        made = updated = made_lines = 0
        for p in plan:
            rows = p.pop("lines")
            was = existing.get(p["source"])
            if was:
                for key, value in p.items():
                    setattr(was, key, value)
                await was.save()
                await ChequeLine.filter(cheque=was).delete()
                cheque = was
                updated += 1
            else:
                cheque = await Cheque.create(**p)
                made += 1
            for row in rows:
                await ChequeLine.create(
                    cheque=cheque, line_no=int(row.get("RowId") or 0),
                    invoice_no=str(row.get("InvNo") or "")[:40], invoice_date=day(row.get("InvDate")),
                    invoice_amount=money(row.get("InvAmount")),
                    outstanding_before=money(row.get("InvOutStandingAmount")),
                    return_amount=money(row.get("purreturnamount")), paid_amount=money(row.get("PaidAmount")))
                made_lines += 1
        print(f"\n   {made:,} cheque(s) created, {updated:,} updated, {made_lines:,} invoice line(s).")
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
