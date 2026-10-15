r"""Give each person the dashboards they could already see, now that each one has a tick of its own.

    python -m scripts.backfill_dashboard_grants            what it would do
    python -m scripts.backfill_dashboard_grants --yes      do it

WHY THIS HAS TO RUN
-------------------
A dashboard used to be gated by the resource of the SCREEN it draws: Sales Analysis rode on
`executive.dashboard`, Stock Health on `executive.stock`, the books ones on `accounts.*`. Each now has its own
resource so it can be administered per person, which is the point -- and it means that the moment the new code
is deployed, nobody holds any of them and every board in the company goes empty. Not a fault anybody could
diagnose from the screen: it just looks like the dashboards were removed.

So this grants `dashboards.X` to exactly the people who could open X yesterday, by the old rule. Nobody gains
anything they did not already have, and nobody loses anything either. After this, access is a tick like any
other and this script never needs running again.

IT IS RE-RUNNABLE AND IT NEVER TAKES ANYTHING AWAY. A person already granted a dashboard is left alone, which
matters because the second run of this is usually somebody checking whether the first one worked. It also
never revokes: a dashboard taken away deliberately after the first run stays taken away.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from tortoise import Tortoise  # noqa: E402

from app.core.config import TORTOISE_ORM  # noqa: E402


def say(line: str = "") -> None:
    print(line, flush=True)


async def run(apply: bool) -> int:
    from app import dashboards as registry
    from app.models import User, UserPermission

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        # The old rule, read off the modules themselves rather than typed out here: whatever each dashboard
        # still names as the screen it belongs beside is the resource that used to gate it.
        was_gated_by = {registry.resource_of(key): module.META["resource"]
                        for key, module in registry.registry().items()}
        say(f"{len(was_gated_by)} dashboard(s), by the resource that used to gate each:")
        for new, old in sorted(was_gated_by.items()):
            say(f"   {new:30s} was {old}")
        say()

        planned: list[tuple[str, str]] = []
        for user in await User.all():
            held = set(await UserPermission.filter(user_id=user.id, can_read=True)
                       .values_list("resource", flat=True))
            already = set(await UserPermission.filter(user_id=user.id).values_list("resource", flat=True))
            wants = sorted(new for new, old in was_gated_by.items() if old in held and new not in already)
            if wants:
                say(f"   {user.email:44s} {len(wants):2d}: {', '.join(w.split('.', 1)[1] for w in wants)}")
                planned += [(str(user.id), w) for w in wants]

        if not planned:
            say("Nothing to do: every person already holds the dashboards their access covered.")
            return 0

        say()
        say(f"{len(planned)} grant(s) to write.")
        if not apply:
            say("Nothing written. Run again with --yes.")
            return 0

        # Read only. A dashboard has nothing to write and nothing to execute, and granting W on one would put
        # a tick on the access screen that means nothing and that somebody would later wonder about.
        await UserPermission.bulk_create([
            UserPermission(user_id=user_id, resource=resource,
                           can_read=True, can_write=False, can_execute=False, granted_by=None)
            for user_id, resource in planned], batch_size=500)
        say(f"Done: {len(planned)} grant(s) written.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="write the grants")
    args = parser.parse_args()
    return asyncio.run(run(args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
