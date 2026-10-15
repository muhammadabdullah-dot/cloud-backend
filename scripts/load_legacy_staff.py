"""Their 139 people into `branch_staff`, as accounts that can sign in at the branch they work at.

    python -m scripts.load_legacy_staff                        what it would do
    python -m scripts.load_legacy_staff --yes                   do it
    python -m scripts.load_legacy_staff --source <live.db>      a different staging copy

WHY THIS EXISTS, AND WHAT CHANGED
---------------------------------
Until 1 October 2026 these people went into `users`, head office's own sign-in list, as locked accounts. That was
wrong twice over: they are shop staff, not head office staff, and ten of them read on screen as System Admin
because their 45 role names were squeezed onto head office's five. `load_legacy_users.py` now removes any that
are left there.

`branch_people.BranchUser` -- the roster -- was the other half of that decision, and its docstring gives the
reason: "Nobody from the old software arrives with a password of theirs and nobody is given a new one, so filing
them there would mean inventing 138 email addresses and 138 credentials for people who must not have any."

Both halves of that are now false by the owner's instruction (1 October 2026): they do arrive with a password,
and addresses are to be invented. So they belong in `branch_staff`, which is where a branch sign-in lives, is
mirrored down to each branch's own database by `staff_sync_service`, and already carries a `password_hash`.
The roster stays as it is: it is the record of who worked where, and it is not a sign-in.

THEIR PASSWORDS ARE NOT HASHED
------------------------------
`dbo.Users.password` is `varchar(50)` holding the password in clear text, for all 139 of them. There is no hash
to copy. So each person's own password is read from staging and **bcrypt-hashed here**, which means they sign in
tomorrow with the password they already use today, and our side never stores the clear text.

The clear text is never printed, never written to a payload file, and never leaves this process. That is why this
script reads staging directly rather than taking a CSV from `map_masters.py`: a payload file would be a second
copy of 139 plaintext passwords sitting on disk, which is exactly what we are getting away from.

Worth raising with their vendor separately: storing passwords in clear text in a readable column is their design,
not ours, and it is the reason this migration is possible at all.

ADDRESSES
---------
Invented, because they have none, and shaped so the address says where somebody works:

    head office      <name>@cloud.dmarina.pk
    a branch         <name>@<branchcode>.dmarina.pk        gt, fc, mt, gg

The name is lower-cased and anything not a letter or digit becomes a dot. Two people with the same name at the
same shop would collide, so the second one carries their own user number: `ali.2.gt@...`. Nobody is given an
address that already belongs to somebody else.

WHICH SHOP SOMEBODY BELONGS TO
------------------------------
Their `dbo.Users` has no branch column, so it is worked out and the order matters:

 1. **What they actually did.** The shop whose bills name them most often, from staging. Evidence beats labels.
 2. **What their group is called.** `SALE FORTCOLONY`, `SALE MODELTOWN`, `SALE GULGASHT` name the shop outright.
 3. **Head office**, for the rest, who are mostly administrators and buyers with no till of their own.

Every person's reason is printed, so a wrong one is visible rather than buried.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sqlite3
import sys
from collections import Counter

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM
from app.core.security import hash_password

HERE = pathlib.Path(__file__).resolve().parent
STAGING = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging" / "live.db"

HEAD_OFFICE = "HO"
DOMAIN_HEAD_OFFICE = "cloud.dmarina.pk"
DOMAIN_SUFFIX = "dmarina.pk"

# Their branch numbers onto our shop codes. Garden Town is two of theirs.
BRANCH_OF = {"1": "GT", "6": "GT", "2": "GG", "3": HEAD_OFFICE, "4": "MT", "5": "FC"}

# A group name that names its shop outright.
NAMED_SHOP = (("FORTCOLONY", "FC"), ("FORT COLONY", "FC"), ("MODELTOWN", "MT"), ("MODEL TOWN", "MT"),
              ("GULGASHT", "GG"), ("GARDENTOWN", "GT"), ("GARDEN TOWN", "GT"))

# Their 33 group names onto the roles a branch has. Visible so it can be argued with rather than inferred from
# behaviour. Anything unrecognised becomes a cashier, which is the least a shop account can be.
ROLE = (("PHARMAC", "pharmacist"),
        ("ADMINISTRATOR", "branch-manager"), ("OPERATION MANAGER", "branch-manager"),
        ("MANAGER", "branch-manager"), ("ACCOUNT HEAD", "branch-manager"), ("MASTER", "branch-manager"))
DEFAULT_ROLE = "cashier"


def role_for(group: str | None) -> str:
    text = (group or "").strip().upper()
    for word, role in ROLE:
        if word in text:
            return role
    return DEFAULT_ROLE


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", (name or "").strip().lower()).strip(".")[:48] or "person"


def address(name: str, book: str, code: str, taken: set[str]) -> str:
    domain = DOMAIN_HEAD_OFFICE if book == HEAD_OFFICE else f"{book.lower()}.{DOMAIN_SUFFIX}"
    base = slug(name)
    email = f"{base}@{domain}"
    if email.lower() in taken:
        email = f"{base}.{code}@{domain}"
    taken.add(email.lower())
    return email


def their_people(source: pathlib.Path) -> tuple[list[dict], dict[str, str]]:
    """Their users with their group and their password, and the shop each one's bills say they work at."""
    if not source.exists():
        raise SystemExit(f"No staging copy at {source}. Run ops/legacy-import/fetch_live.py first.")
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        people = [dict(r) for r in conn.execute("""
            SELECT u.USno AS code, u.Name AS name, u.password AS password, u.Active AS active,
                   g.GroupName AS group_name
              FROM "dbo.Users" u LEFT JOIN "dbo.GroupHeader" g ON g.GroupSno = u.GroupSno
             ORDER BY u.USno
        """)]
        # Where each person's bills were rung. Counted rather than taken from the first one found, because
        # somebody who covered a shift elsewhere should not be filed at the shop they visited once.
        seen: dict[str, Counter] = {}
        for row in conn.execute("""
            SELECT h.USno AS code, x.BranchSno AS branch, COUNT(*) AS n
              FROM "dbo.SaleHeader" h JOIN "dbo.SaleExtraInfo" x ON x.SSno = h.SSno
             GROUP BY h.USno, x.BranchSno
        """):
            book = BRANCH_OF.get(str(row["branch"]))
            if book:
                seen.setdefault(str(row["code"]), Counter())[book] += int(row["n"])
        return people, {code: counts.most_common(1)[0][0] for code, counts in seen.items()}
    finally:
        conn.close()


def book_for(person: dict, roster: dict[str, str], by_activity: dict[str, str]) -> tuple[str, str]:
    """The roster first, because it was built from the whole history.

    `live.db` holds only the nightly fetch window, so asking it alone put 112 of 139 people at head office purely
    for not having rung a bill in the last six days. `branch_users` is keyed on the same user number and was
    filled from every document loaded, which is years rather than days.
    """
    code = str(person["code"])
    if code in roster:
        return roster[code], "the roster, from every document loaded"
    if code in by_activity:
        return by_activity[code], "their bills in the fetch window"
    text = (person.get("group_name") or "").upper()
    for word, book in NAMED_SHOP:
        if word in text:
            return book, "their group names the shop"
    return HEAD_OFFICE, "no documents anywhere and no shop in the group name"


async def run(args) -> int:
    people, by_activity = their_people(pathlib.Path(args.source))
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Branch, BranchStaff, BranchStaffAssignment, BranchUser

        branches = {b.code: b for b in await Branch.all()}
        by_id = {str(b.id): b.code for b in branches.values()}
        roster = {u.code: by_id[str(u.branch_id)] for u in await BranchUser.all() if str(u.branch_id) in by_id}
        missing = {book for book, _ in (book_for(p, roster, by_activity) for p in people)} - set(branches)
        if missing:
            raise SystemExit(f"No branch row for {', '.join(sorted(missing))}. Nothing written.")

        here = {s.id: s for s in await BranchStaff.all()}
        taken = {s.email.lower() for s in here.values()}

        plan, no_password = [], 0
        for person in people:
            secret = (person.get("password") or "").strip()
            if not secret:
                no_password += 1
                continue
            book, why = book_for(person, roster, by_activity)
            staff_id = f"legacy-{person['code']}"
            if staff_id in here:
                continue
            plan.append({
                "id": staff_id, "name": str(person["name"]).strip()[:120],
                "email": address(str(person["name"]), book, str(person["code"]), taken),
                "role_id": role_for(person.get("group_name")), "book": book, "why": why,
                "active": str(person.get("active") or "").strip() in ("1", "1.0", "Y", "y", "yes", "True"),
                "secret": secret,
            })

        print(f"   their people            {len(people):,}")
        print(f"   already in branch_staff {len(here):,}, left alone")
        print(f"   would add               {len(plan):,}"
              + (f", {no_password} skipped for having no password at all" if no_password else ""))
        print(f"   active                  {sum(1 for p in plan if p['active']):,} of {len(plan):,}")
        print()
        print("   by shop, and why each was put there:")
        for book in sorted({p["book"] for p in plan}):
            rows = [p for p in plan if p["book"] == book]
            reasons = Counter(p["why"] for p in rows)
            print(f"      {book:<4} {len(rows):>4}   " + ", ".join(f"{n} by {why}" for why, n in reasons.most_common()))
        print()
        print("   by role:")
        for role, n in Counter(p["role_id"] for p in plan).most_common():
            print(f"      {role:<18} {n:>4}")
        print()
        print("   a few addresses, so the shape is visible (never a password):")
        for p in plan[:6]:
            print(f"      {p['name'][:22]:<22} {p['email']:<40} {p['role_id']:<16} {p['book']}")

        if not args.yes:
            print("\n   Nothing written. Add --yes.")
            return 0

        made = 0
        for p in plan:
            staff = await BranchStaff.create(
                id=p["id"], name=p["name"], email=p["email"], role_id=p["role_id"], active=p["active"],
                # Their clear text, bcrypt-hashed here and not kept. They sign in with what they already use.
                password_hash=hash_password(p["secret"]),
                permissions=[], rev=1, last_changed_at="cloud", last_changed_by="legacy import")
            await BranchStaffAssignment.create(staff=staff, branch=branches[p["book"]])
            made += 1
        print(f"\n   {made:,} branch staff account(s) created, each assigned to one shop.")
        print("   Each signs in with the password they already use in Multi-Tec. Nothing holds the clear text here.")
        print("   They reach the branches themselves when a branch database exists and staff_sync_service runs.")
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
