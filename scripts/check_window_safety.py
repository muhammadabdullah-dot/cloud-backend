"""Prove a windowed load touches only its window, and leaves everything outside it exactly as it was.

    python scripts/check_window_safety.py --shop GT --window 2026-09-10:2026-09-15

The exit code is the verdict. It counts what is there, runs the windowed load for real, counts again, and compares
**row by row outside the window**, not just totals: a load that deleted a hundred old rows and inserted a hundred new
ones would keep the total and still have destroyed history.

WHY THIS EXISTS
---------------
`load_legacy_shop` replaced a branch's whole purchase history, roster and party list on every run, which is right for
a full load and ruinous for a nightly one: a two day window would have deleted 13,789 purchase orders, 25,068
deliveries, 4,044 returns and 1,158 parties, then put back only what those two days mentioned. Nothing would have
complained, because no constraint fires when rows merely stop existing.

The fix is `window=`, and a fix to a silent fault needs a test that would have caught it. This is that test.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import subprocess
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

HERE = pathlib.Path(__file__).resolve().parent

# table, the model, and how to read its day. Everything a windowed run must not disturb outside its window.
WATCHED = [
    ("purchase orders", "BranchPurchaseOrder"),
    ("deliveries", "BranchGrn"),
    ("purchase returns", "BranchPurchaseReturn"),
    ("bills", "BranchSale"),
    ("returns", "BranchReturnRecord"),
]
# Lists that have no day at all: a windowed run must only ever add to or update these, never shrink them.
LISTS = [("parties", "BranchParty"), ("roster", "BranchUser")]


async def census(branch, start: date, end: date) -> dict:
    """What is there now: how many rows sit outside the window, and their identity, so a swap is as visible as a
    deletion."""
    from tortoise.expressions import Q

    from app import models

    # Written as an explicit OR rather than `.exclude(day__gte=..., day__lte=...)`. The exclude form reported zero
    # rows outside the window for a branch holding 547,396 bills, so whatever it negates it is not the conjunction,
    # and a safety check that cannot see the rows it is protecting protects nothing.
    beyond = Q(day__lt=start) | Q(day__gt=end)

    out: dict = {}
    for label, name in WATCHED:
        Model = getattr(models, name)
        outside = await Model.filter(beyond, branch=branch).count()
        inside = await Model.filter(branch=branch, day__gte=start, day__lte=end).count()
        ids = sorted(str(i) for i in
                     await Model.filter(beyond, branch=branch).values_list("id", flat=True))
        out[label] = {"outside": outside, "inside": inside, "ids": ids}
    for label, name in LISTS:
        Model = getattr(models, name)
        out[label] = {"outside": await Model.filter(branch=branch).count(), "inside": 0, "ids": []}
    return out


async def run(args) -> int:
    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings
    from app.models import Branch

    a, _, b = args.window.partition(":")
    start, end = date.fromisoformat(a), date.fromisoformat(b)

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        branch = await Branch.filter(code=args.shop).first()
        if not branch:
            raise SystemExit(f"no branch {args.shop!r}")
        print(f"   {settings.db_url.split('@')[-1] if '@' in settings.db_url else settings.db_url}")
        print(f"   {args.shop}, window {start} to {end}\n")
        before = await census(branch, start, end)
        print(f"   {'':<18} {'outside the window':>19} {'inside':>8}")
        for label, _m in WATCHED + LISTS:
            print(f"   {label:<18} {before[label]['outside']:>19,} {before[label]['inside']:>8,}")
    finally:
        await Tortoise.close_connections()

    if not args.yes:
        print("\n   Nothing run. Add --yes to do the windowed load and compare.")
        return 0

    print(f"\n   running the windowed load ...\n{'-' * 90}", flush=True)
    done = subprocess.run(
        # `--replace` is only the guard that says "yes, this shop already has figures". What is actually deleted is
        # decided by `--window`, which is the thing under test.
        [sys.executable, "-m", "scripts.load_legacy_shop", "--shop", args.shop,
         "--window", args.window, "--only", args.only, "--yes", "--replace", "--no-backup"],
        cwd=str(HERE.parent), capture_output=True, text=True, timeout=7200)
    for line in (done.stdout or "").splitlines()[-18:]:
        print("   " + line)
    if done.returncode != 0:
        print(f"{'-' * 90}\n   the load exited {done.returncode}")
        for line in (done.stderr or "").splitlines()[-12:]:
            print("   " + line)
        return 1
    print("-" * 90)

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Branch as B
        branch = await B.filter(code=args.shop).first()
        after = await census(branch, start, end)
    finally:
        await Tortoise.close_connections()

    print(f"\n   {'':<18} {'outside before':>15} {'outside after':>14}   verdict")
    findings = []
    for label, _m in WATCHED:
        was, now = before[label], after[label]
        lost = set(was["ids"]) - set(now["ids"])
        gained = set(now["ids"]) - set(was["ids"])
        if lost or gained:
            findings.append(f"{label}: {len(lost):,} row(s) outside the window disappeared, "
                            f"{len(gained):,} appeared")
            verdict = f"*** {len(lost):,} lost, {len(gained):,} appeared"
        else:
            verdict = "untouched, row for row"
        print(f"   {label:<18} {was['outside']:>15,} {now['outside']:>14,}   {verdict}")
    for label, _m in LISTS:
        was, now = before[label]["outside"], after[label]["outside"]
        if now < was:
            findings.append(f"{label}: the list shrank from {was:,} to {now:,}")
            verdict = f"*** SHRANK by {was - now:,}"
        else:
            verdict = "kept" + (f", {now - was:,} added" if now > was else "")
        print(f"   {label:<18} {was:>15,} {now:>14,}   {verdict}")

    print()
    if findings:
        print("   NOT SAFE. A windowed run changed things outside its window:")
        for f in findings:
            print(f"      {f}")
        return 1
    print("   Safe. Every row outside the window is the same row it was, and no list shrank.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shop", required=True)
    ap.add_argument("--window", required=True, help="FROM:TO")
    ap.add_argument("--only", default="buying,roster,documents")
    ap.add_argument("--yes", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
