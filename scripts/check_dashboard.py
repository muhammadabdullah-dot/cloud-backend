"""Run the real dashboard against whatever DB_URL points at, and time it.

    python scripts/check_dashboard.py                        every period
    python scripts/check_dashboard.py --period ytd --branch GG

This calls `executive_controller.list_kpis`, which is exactly what `GET /executive/kpis` calls, so it exercises the
hand-written SQL in executive_service, analytics_service and the rollups rather than a simplified version of it. Run
it with DB_URL on the SQLite file and again on SQL Server and the two runs answer two questions at once: whether the
SQL is portable, and which engine is faster at it.

THE NUMBER THAT MATTERS
-----------------------
The frontend gives up at 15 seconds. The year view took 18.8 on SQLite, which is why the screen showed nothing at
all rather than showing it slowly. So the pass mark for each period is not "it returned" but "it returned inside the
timeout the app actually enforces", and that is printed per period rather than left to be worked out.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TIMEOUT = 15.0   # what cloud-app waits before it gives up

PERIODS = ("today", "7d", "30d", "90d", "ytd")


async def run(args) -> int:
    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings

    engine = settings.db_url.split("://")[0]
    shown = settings.db_url.split("://")[0] + "://" + (
        settings.db_url.split("@")[-1] if "@" in settings.db_url else settings.db_url.split("://")[1])
    print(f"   {shown}")

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.controllers import executive_controller

        periods = [args.period] if args.period else list(PERIODS)
        print(f"   {len(periods)} period(s), each the whole dashboard grid"
              f"{', branch ' + args.branch if args.branch else ', every branch'}\n")
        print(f"   {'period':<10} {'seconds':>8}  {'tiles':>6} {'charts':>7} {'blank':>6}   verdict")
        slow = []
        failed = []
        empty = []
        for period in periods:
            started = time.monotonic()
            try:
                out = await executive_controller.list_kpis(period, branch=args.branch)
                took = time.monotonic() - started
                tiles = len(out.items or [])
                charts = len(out.charts or [])
                # A tile that could not be built is reported rather than left out, so an empty grid is a finding and
                # not a fast pass. A dashboard that answers in a second with nothing on it is not working.
                blank = len(out.notBuilt or [])
                if not tiles:
                    empty.append(period)
                verdict = "ok" if took < TIMEOUT else f"OVER the {TIMEOUT:.0f}s timeout"
                if took >= TIMEOUT:
                    slow.append((period, took))
                if not tiles:
                    verdict = "NO TILES AT ALL"
                print(f"   {period:<10} {took:>8.2f}  {tiles:>6} {charts:>7} {blank:>6}   {verdict}")
            except Exception as exc:  # noqa: BLE001: the point is to find what this engine refuses
                took = time.monotonic() - started
                failed.append((period, exc))
                message = str(exc).replace("\n", " ")[:150]
                print(f"   {period:<10} {took:>8.2f}  {'-':>6}   FAILED: {message}")

        print()
        if failed:
            print(f"   {len(failed)} period(s) failed on {engine}:")
            for period, exc in failed:
                print(f"      {period}: {type(exc).__name__}: {str(exc)[:220]}")
            return 1
        if empty:
            print(f"   {len(empty)} period(s) returned no tiles at all: {', '.join(empty)}")
            return 1
        if slow:
            print(f"   every period returned, but {len(slow)} is past the {TIMEOUT:.0f}s the app waits:")
            for period, took in slow:
                print(f"      {period}: {took:.1f}s")
            return 1
        print(f"   Every period returned inside the {TIMEOUT:.0f}s the frontend waits.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", help="one of " + ", ".join(PERIODS))
    ap.add_argument("--branch", help="a branch code, or leave out for every branch")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
