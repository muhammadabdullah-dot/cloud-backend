"""Make sure there are sign-ins that work after a load, and put their passwords where passwords are kept.

    python -m scripts.ensure_test_users                  which sign-ins exist, and whether they are active
    python -m scripts.ensure_test_users --reset --yes      give them fresh passwords

WHY THIS EXISTS
---------------
Loading their 138 people adds accounts that cannot sign in, by design. That leaves a question nobody wants to
discover at a demonstration: can anybody sign in at all? This answers it, and can fix it.

WHERE THE PASSWORDS GO
----------------------
Into `Reqs\\ALL_LOGIN_CREDENTIALS.txt`, appended as a dated block. That is the one file whose job this is. They are
**never printed**: this prints the email addresses and the file path, and nothing else. A password that goes through
a terminal, a log or a chat is a password that has been published, and the `dmarina_read` login learned that lesson
on 30 September 2026 the hard way.

Each password is generated here from `secrets`, and this script is the only thing that ever sees it.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import secrets
import sys
from datetime import datetime

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM
from app.core.security import hash_password

HERE = pathlib.Path(__file__).resolve().parent
CREDENTIALS = HERE.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"

# The sign-ins a person needs to look at what was loaded. Their emails are the ones the fresh test already seeds, so
# this normally finds them and only reports; --reset gives them known passwords again.
WANTED = (
    ("executive@cloud.dmarina.pk", "Owner / Executive", "executive",
     "the shops, their days, their Items, their bills"),
    ("hamza.accounts@test.dmarina.pk", "Hamza Accounts", "accountant",
     "the books: the chart of accounts and the vouchers, one book per shop"),
    ("sysadmin@cloud.dmarina.pk", "System Admin", "system-admin",
     "users, roles, branches, sync health"),
    ("warehousemanager@cloud.dmarina.pk", "Warehouse Manager", "warehouse-manager",
     "the warehouse, purchase orders and GRNs of head office's own"),
)

WORDS = ("marina", "harbour", "anchor", "compass", "lantern", "cedar", "quartz", "meadow", "falcon", "amber")


def readable_password() -> str:
    """Long enough to be safe, shaped so somebody can type it from a printed page without a mistake."""
    return f"{secrets.choice(WORDS).capitalize()}-{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"


async def run(args) -> int:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Role, User

        roles = {r.id for r in await Role.all()}
        total = await User.all().count()
        signed_in_able = 0
        report: list[tuple[str, str, str, str]] = []
        changes: list[tuple[User, str, str]] = []

        for email, name, role_id, what in WANTED:
            user = await User.get_or_none(email=email)
            if user:
                state = "active" if user.active else "INACTIVE"
                report.append((email, user.name, f"{user.role_id}, {state}", what))
                if args.reset:
                    changes.append((user, readable_password(), "password reset"))
            else:
                report.append((email, name, f"{role_id}, DOES NOT EXIST", what))
                if args.reset:
                    if role_id not in roles:
                        print(f"   no role {role_id} at head office; skipping {email}")
                        continue
                    changes.append((None, readable_password(), f"create|{email}|{name}|{role_id}"))

        print(f"   {total:,} account(s) in users. {len(WANTED)} are the ones for testing:")
        print()
        for email, name, state, what in report:
            print(f"      {email:<40} {state:<28} {what}")
        print()
        legacy = await User.filter(email__endswith="@legacy.multitec.invalid").count()
        if legacy:
            print(f"   {legacy:,} of the {total:,} are their people, loaded as records. None can sign in: every one")
            print("   has a bcrypt hash of a random secret that was thrown away.")
            print()

        if not args.reset:
            print("   Nothing changed. Add --reset --yes to give these four fresh passwords.")
            return 0
        if not args.yes:
            print("   --reset needs --yes as well. Nothing changed.")
            return 0

        lines = [
            "",
            "=" * 78,
            f"FRESH TEST HEAD OFFICE (port 4205) -- test sign-ins set {datetime.now():%Y-%m-%d %H:%M}",
            "Set by backend/cloud-server/scripts/ensure_test_users.py after the Multi-Tec load.",
            "These four can sign in. Their 138 people cannot: those accounts hold a hash of a",
            "random secret that was discarded, so no password authenticates them.",
            "=" * 78,
        ]
        for user, password, action in changes:
            if action == "password reset":
                user.password_hash = hash_password(password)
                user.active = True
                await user.save(update_fields=["password_hash", "active"])
                lines.append(f"  {user.email:<42} {password:<26} {user.role_id}")
            else:
                _c, email, name, role_id = action.split("|")
                await User.create(name=name, email=email, role_id=role_id, active=True,
                                  password_hash=hash_password(password))
                lines.append(f"  {email:<42} {password:<26} {role_id}")
        lines.append("")

        CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
        with CREDENTIALS.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

        print(f"   {len(changes)} sign-in(s) set. The passwords are appended to:")
        print(f"      {CREDENTIALS}")
        print("   They are not printed here, and nothing else holds them.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="give the test sign-ins fresh passwords")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
