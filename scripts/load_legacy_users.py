"""Keep their people OUT of head office's user list, and say where they belong instead.

    python -m scripts.load_legacy_users              what it would remove
    python -m scripts.load_legacy_users --yes         remove them

THIS SCRIPT USED TO DO THE OPPOSITE, AND THAT WAS WRONG
-------------------------------------------------------
Until 1 October 2026 this added their 138 people to `users`, head office's own sign-in list, on the reasoning that
a bill naming a cashier should have somebody to point at. The accounts were locked -- a bcrypt hash of a random
secret generated, used once and thrown away, and an `@legacy.multitec.invalid` address that RFC 2606 guarantees
can never resolve -- so none of them could ever sign in.

Locked is not the same as harmless. `users` is the list the app means by "the people who work at head office",
and it is where an administrator looks to answer "who has access". Putting 138 shop till operators in it put ten
of them on screen as **System Admin** and six as **Owner / Executive**, because their 45 role names were mapped
onto head office's five and a shop manager maps to an executive. Anyone reading that list would have had to take
it on trust that the hashes were unusable. A list of who has access should be readable as exactly that.

They are shop staff. Their home is `branch_users`, the per-shop roster, which `load_legacy_shop.py --only roster`
already fills from what each person actually did at each shop, with `can_sign_in=False` on every row. That roster
is derived from activity, so it holds the 90 people who appear on a document rather than all 138: the file this
script used to read, `payload-ALL-users.csv`, carries no branch at all, so it cannot say which shop the other 48
belong to. Guessing from a name suffix would be a guess.

WHAT THIS DOES NOW
------------------
Removes any account in `users` whose address is in the legacy domain, after checking that nothing references it.
It refuses rather than cascades: if a voucher, a price change or anything else has come to point at one of these
accounts since, that is a real link and deleting the row would either fail or orphan something, so it stops and
says which.

`finish_cloud.cmd` and `resume_and_finish.cmd` both call this, which is the point: a full rebuild now enforces
the right state instead of recreating the wrong one.

STILL TO DO, AND IT IS NOT THIS SCRIPT'S JOB
--------------------------------------------
Nothing in the app reads `branch_users` yet -- no route, no screen. So once these rows are gone, their people are
correctly absent from the head office user list and not yet visible anywhere else. The roster needs an endpoint
and a screen before this is finished.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

EMAIL_DOMAIN = "legacy.multitec.invalid"


async def referencing(conn, ids: list[str]) -> list[tuple[str, str, int]]:
    """Every foreign key into `users`, and how many of its rows point at one of `ids`.

    Asked of the database rather than the models, because a model list goes stale the moment somebody adds a
    column and this question has to be right on the day it is asked.
    """
    keys = await conn.execute_query_dict(
        "SELECT OBJECT_NAME(fk.parent_object_id) AS tbl, "
        "       COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS col "
        "  FROM sys.foreign_keys fk "
        "  JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
        " WHERE fk.referenced_object_id = OBJECT_ID('users')")
    found = []
    for key in keys:
        rows = await conn.execute_query_dict(
            f"SELECT COUNT_BIG(*) AS n FROM [{key['tbl']}] t "
            f"JOIN users u ON u.id = t.[{key['col']}] WHERE u.email LIKE ?", [f"%@{EMAIL_DOMAIN}"])
        count = int(rows[0]["n"] or 0)
        if count:
            found.append((key["tbl"], key["col"], count))
    return found


async def run(args) -> int:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import BranchUser

        conn = Tortoise.get_connection("default")
        # Raw SQL, not `User.filter(email__endswith=...)`. That ORM filter returns 3 of these 138 rows on SQL
        # Server: `LIKE '%@legacy.multitec.invalid'`, `LIKE '%legacy.multitec%'` and `RIGHT(email, 24) = ...` all
        # agree on 138, and the ORM does not. Whatever it escapes, a delete built on it would have left 135
        # accounts behind and reported success.
        suffix = f"%@{EMAIL_DOMAIN}"
        theirs = await conn.execute_query_dict(
            "SELECT id, name, role_id FROM users WHERE email LIKE ? ORDER BY name", [suffix])
        others = (await conn.execute_query_dict(
            "SELECT COUNT_BIG(*) AS n FROM users WHERE email NOT LIKE ?", [suffix]))[0]["n"]
        roster = await BranchUser.all().count()

        print(f"   in users, from the legacy load   {len(theirs):,}")
        print(f"   in users, real accounts          {others:,}   (untouched)")
        print(f"   in branch_users, the rosters     {roster:,}   (where shop people belong)")

        if not theirs:
            print("\n   Nothing to remove. Head office's user list holds only its own people.")
            return 0

        by_role: dict[str, int] = {}
        for person in theirs:
            role = person["role_id"] or "(none)"
            by_role[role] = by_role.get(role, 0) + 1
        print("\n   the head office roles they were given, which is the problem:")
        for role, count in sorted(by_role.items(), key=lambda kv: -kv[1]):
            loud = "   <-- reads as head office access" if role in ("system-admin", "executive") else ""
            print(f"      {role:<22} {count:>4}{loud}")

        blocked = await referencing(conn, [p["id"] for p in theirs])
        if blocked:
            print("\n   REFUSING. Something has come to reference these accounts since they were made:")
            for tbl, col, count in blocked:
                print(f"      {tbl}.{col}  {count:,} row(s)")
            print("   Deleting them would orphan real records. Re-point those rows first.")
            return 1
        print("\n   nothing references them: every foreign key into users comes back empty for these rows.")

        if not args.yes:
            print("\n   Nothing written. Add --yes to remove them.")
            return 0

        removed = len(theirs)
        await conn.execute_query("DELETE FROM users WHERE email LIKE ?", [suffix])
        print(f"\n   {removed:,} account(s) removed from head office's user list.")
        print(f"   The {others:,} real account(s) are exactly as they were, and the {roster:,} roster rows are untouched.")
        print("   Those people are shop staff; branch_users is their record, and it still needs a screen.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
