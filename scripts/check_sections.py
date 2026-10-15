"""Time each dashboard section on its own, and check the split changed nothing about the figures.

    python scripts/check_sections.py                   every section, 30 days
    python scripts/check_sections.py --period ytd
    python scripts/check_sections.py --period ytd --branch GT

The exit code is the verdict.

WHAT IT IS FOR
--------------
The dashboard used to be one request behind one loader, so the page showed nothing until the slowest tile finished
and a single failure took the whole screen. This measures the thing that replaced it: how long each section takes on
its own, which is what decides when something first appears.

It also checks that splitting the grid did not change it. Every tile the sections produce is compared with the tile
the whole grid produces, by id and by what it displays, so a refactor that quietly dropped a tile or changed a figure
is a failure here rather than a surprise on screen.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TIMEOUT = 15.0


async def run(args) -> int:
    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings

    print(f"   {settings.db_url.split('@')[-1] if '@' in settings.db_url else settings.db_url}")
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.controllers import executive_controller as ec

        every = ec.sections()
        print(f"   {len(every)} section(s), period {args.period}"
              f"{', branch ' + args.branch if args.branch else ', every branch'}\n")

        print(f"   {'section':<26} {'seconds':>8} {'tiles':>6} {'charts':>7}   verdict")
        got: dict[str, object] = {}
        slow, failed = [], []
        for ref in every:
            at = time.monotonic()
            try:
                out = await ec.section_for(ref.id, args.period, branch=args.branch)
                took = time.monotonic() - at
                for tile in out.items:
                    got[tile.id] = tile
                verdict = "ok" if took < TIMEOUT else f"OVER the {TIMEOUT:.0f}s timeout"
                if took >= TIMEOUT:
                    slow.append((ref.label, took))
                print(f"   {ref.label:<26} {took:>8.2f} {len(out.items):>6} {len(out.charts):>7}   {verdict}")
            except Exception as exc:  # noqa: BLE001
                failed.append((ref.label, exc))
                print(f"   {ref.label:<26} {time.monotonic()-at:>8.2f} {'-':>6}   FAILED  "
                      f"{str(exc).splitlines()[0][:80]}")

        # The whole grid, for comparison. This is what the page used to wait for in one go.
        at = time.monotonic()
        whole = await ec.list_kpis(args.period, branch=args.branch)
        one_go = time.monotonic() - at
        print(f"\n   {'the whole grid, as before':<26} {one_go:>8.2f} {len(whole.items):>6}")

        print()
        if failed:
            print(f"   {len(failed)} section(s) failed:")
            for label, exc in failed:
                print(f"      {label}: {type(exc).__name__}: {' '.join(str(exc).split())[:240]}")
            return 1

        # Same tiles, same figures.
        mine = {t.id: t for t in whole.items}
        missing = sorted(set(mine) - set(got))
        extra = sorted(set(got) - set(mine))
        differing = [i for i in set(mine) & set(got)
                     if (mine[i].display, mine[i].sub) != (got[i].display, got[i].sub)]
        if missing or extra or differing:
            print("   THE SPLIT CHANGED THE GRID:")
            if missing:
                print(f"      {len(missing)} tile(s) the sections do not produce: {', '.join(missing)}")
            if extra:
                print(f"      {len(extra)} tile(s) only the sections produce: {', '.join(extra)}")
            for i in differing[:10]:
                print(f"      {i}: grid says {mine[i].display!r}/{mine[i].sub!r}, "
                      f"section says {got[i].display!r}/{got[i].sub!r}")
            return 1
        print(f"   The {len(mine)} tile(s) are identical whether read as one grid or section by section.")

        if slow:
            worst = max(t for _l, t in slow)
            print(f"\n   {len(slow)} section(s) are past the {TIMEOUT:.0f}s the app waits, worst {worst:.1f}s.")
            print("   The rest still appear: that is the point of the split. But these want fixing.")
            for label, took in slow:
                print(f"      {label}: {took:.1f}s")
            return 1
        print(f"   Every section is inside the {TIMEOUT:.0f}s the frontend waits.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="30d")
    ap.add_argument("--branch")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
