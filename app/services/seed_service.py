"""Day-one seed: exactly the accounts in frontend-baseline.md §1.1, with RoleDefaultPermission
templates materialized into each seeded user's real UserPermission rows (contracts.md §2.3).
Idempotent — a no-op if roles already exist, so it's safe to run on every startup.
"""
from app.core.resources import excluded_resources_for_role, resources_for_role
from app.core.security import hash_password
from app.models import Branch, Role, RoleDefaultPermission, User, UserPermission

ROLES = [
    ("warehouse-manager", "Warehouse Manager", "/warehouse/dashboard"),
    ("picker", "Picker / Packer", "/warehouse/picking"),
    ("executive", "Owner / Executive", "/executive/dashboard"),
    ("system-admin", "System Admin", "/admin"),
    ("accountant", "Accountant", "/accounts/dashboard"),
]

USERS = [
    ("warehousemanager@cloud.dmarina.pk", "warehouse123", "warehouse-manager", "Warehouse Manager"),
    ("picker@cloud.dmarina.pk", "picker123", "picker", "Picker / Packer"),
    ("executive@cloud.dmarina.pk", "exec123", "executive", "Owner / Executive"),
    ("sysadmin@cloud.dmarina.pk", "admin123", "system-admin", "System Admin"),
]

# The two branches cloud-app's fixtures/branches.ts has always shown (Head Office, Fort Colony),
# now as real rows. Requisitions and transfers point at these instead of carrying a free-text
# branch name, which is the structural gap frontend-baseline.md §4 flags.
# Head office is deliberately NOT in this list, and that is a modelling decision rather than an
# omission.
#
# Head office is *this* — the Cloud. It holds the godown, the catalog, procurement and the company
# view. It does not trade over a counter, so it has no till, no cashier and no branch database, and
# a "Head Office" row sitting in the branch list made every company-wide total count the same shop
# twice the moment a real branch reported.
#
# A shop that happens to stand next door to head office is still a branch: its own branch server,
# its own code on its own bills, its own verification key, and — the point that matters — its own
# local database, so it keeps trading through a power cut or a dead DSL line exactly like a branch
# three hours away. There is no "main branch" special case to write, because there is no special
# case.
BRANCHES = [
    ("FC", "Fort Colony", "Fort Colony Road", "Multan", "061-2116303"),
]


async def seed_branches_if_empty() -> None:
    """Separate from seed_if_empty: that one is guarded on Role.exists() and so never runs again
    on an already-seeded database — which is every database that existed before branches did."""
    if await Branch.exists():
        return
    for code, name, address, city, phone in BRANCHES:
        await Branch.create(code=code, name=name, address=address, city=city, phone=phone)


async def seed_if_empty() -> None:
    if await Role.exists():
        return

    for role_id, name, landing in ROLES:
        role = await Role.create(id=role_id, name=name, landing=landing)
        for resource in resources_for_role(role_id):
            await RoleDefaultPermission.create(role=role, resource=resource, can_read=True, can_write=True, can_execute=True)

    for email, password, role_id, name in USERS:
        user = await User.create(
            name=name, email=email.lower(), password_hash=hash_password(password), role_id=role_id
        )
        templates = await RoleDefaultPermission.filter(role_id=role_id)
        for template in templates:
            await UserPermission.create(
                user=user,
                resource=template.resource,
                can_read=template.can_read,
                can_write=template.can_write,
                can_execute=template.can_execute,
                granted_by=None,
            )


async def sync_role_resource_grants() -> None:
    """Backfills any resource added to `core/resources.py` (and therefore to a role's template)
    onto every existing user of that role, and onto the role's own default-permission rows.

    Without this, a resource added after a user was seeded never reaches them —
    RoleDefaultPermission templates only materialize into real UserPermission rows at
    User.create() time. Runs on every startup.

    Additive for ordinary grants — it never narrows a permission a System Admin has hand-edited.
    The one thing it *does* remove is a resource the role template explicitly excludes, because
    that is a standing policy ("a Picker must never approve their own count"), not a per-user
    customization someone might legitimately have made.
    """
    for role_id, _name, _landing in ROLES:
        template_resources = resources_for_role(role_id)
        existing = set(await RoleDefaultPermission.filter(role_id=role_id).values_list("resource", flat=True))
        for resource in template_resources - existing:
            await RoleDefaultPermission.create(
                role_id=role_id, resource=resource, can_read=True, can_write=True, can_execute=True,
            )
        # A stale template row here would silently re-grant an excluded resource to the next hire.
        excluded = excluded_resources_for_role(role_id)
        if excluded:
            await RoleDefaultPermission.filter(role_id=role_id, resource__in=list(excluded)).delete()

    # Only resources new to a role since the last startup go out to its people. Re-adding the whole
    # template every time quietly undid every screen a System Admin took away from someone.
    new_for_role: dict[str, set[str]] = {}
    for role in await Role.all():
        template = resources_for_role(role.id)
        # The first startup that keeps track hands out the whole template once more — exactly what every
        # earlier startup did — so resources added in the same release still reach existing people.
        new_for_role[role.id] = template if role.rolled_out_resources is None else template - set(role.rolled_out_resources)
        if role.rolled_out_resources != sorted(template):
            role.rolled_out_resources = sorted(template)
            await role.save(update_fields=["rolled_out_resources"])

    for user in await User.all():
        template_resources = new_for_role.get(user.role_id, set())
        granted = set(await UserPermission.filter(user=user).values_list("resource", flat=True))
        for resource in template_resources - granted:
            await UserPermission.create(
                user=user, resource=resource, can_read=True, can_write=True, can_execute=True, granted_by=None,
            )
        excluded = excluded_resources_for_role(user.role_id)
        if excluded:
            await UserPermission.filter(user=user, resource__in=list(excluded)).delete()


async def ensure_roles() -> int:
    """Roles added after the first start (the Accountant) exist on every database, not only new ones."""
    made = 0
    for role_id, name, landing in ROLES:
        if not await Role.exists(id=role_id):
            await Role.create(id=role_id, name=name, landing=landing)
            made += 1
    return made


async def split_the_books_access() -> int:
    """Once: the accounts ticks there were become a tick per screen and action, and per area of accounts
    (core/abilities.py LEGACY_ACCOUNTS says what each old tick stands for), for every head office user and role, so
    nobody gains or loses anything. "See the books" itself goes; every branch's books stays as it is.

    The branch role access head office keeps, and its copy of every branch account, get the branch's version of the
    same (BRANCH_LEGACY_ACCOUNTS), so sending either to a branch never takes the books away from anyone. Runs before
    the startup backfill, and counts the new names as already handed out to each role, so the backfill doesn't give a
    full set of accounts ticks to someone whose access was deliberately narrower."""
    from app.core.abilities import BRANCH_LEGACY_ACCOUNTS, LEGACY_ACCOUNTS, RETIRED_RESOURCES, translate_legacy, translate_legacy_resources
    from app.models import BranchRoleTemplate, BranchStaff, Counter

    if await Counter.exists(id="rollout:accounts-split"):
        return 0
    fields = {"R": "can_read", "W": "can_write", "X": "can_execute"}

    async def rewrite(rows, make) -> bool:
        before = {row.resource: {a for a, f in fields.items() if getattr(row, f)} for row in rows}
        after = translate_legacy(before)
        by_resource = {row.resource: row for row in rows}
        touched = False
        for resource, actions in after.items():
            row = by_resource.get(resource)
            if row is None:
                await make(resource, actions)
                touched = True
            elif before.get(resource, set()) != actions:
                # Merged, never narrowed: `after` holds everything a row had.
                for action, field in fields.items():
                    setattr(row, field, action in actions)
                await row.save()
                touched = True
        for resource in RETIRED_RESOURCES:
            if resource in by_resource:
                await by_resource[resource].delete()
                touched = True
        return touched

    changed = 0
    for user in await User.all():
        async def make_user_row(resource, actions, user=user):
            await UserPermission.create(user=user, resource=resource, can_read="R" in actions, can_write="W" in actions,
                                        can_execute="X" in actions, granted_by=None)
        if await rewrite(await UserPermission.filter(user=user), make_user_row):
            changed += 1

    new_names = {target for targets in LEGACY_ACCOUNTS.values() for target, _ in targets}
    for role in await Role.all():
        async def make_role_row(resource, actions, role=role):
            await RoleDefaultPermission.create(role=role, resource=resource, can_read="R" in actions, can_write="W" in actions,
                                               can_execute="X" in actions)
        await rewrite(await RoleDefaultPermission.filter(role=role), make_role_row)
        if role.rolled_out_resources is not None:
            role.rolled_out_resources = sorted((set(role.rolled_out_resources) - RETIRED_RESOURCES) | (resources_for_role(role.id) & new_names))
            await role.save(update_fields=["rolled_out_resources"])

    for template in await BranchRoleTemplate.all():
        resources = translate_legacy_resources(template.resources or [], BRANCH_LEGACY_ACCOUNTS)
        if resources != set(template.resources or []):
            template.resources = sorted(resources)
            await template.save()
    for staff in await BranchStaff.all():
        held: dict[str, set[str]] = {}
        for grant in staff.permissions or []:
            held.setdefault(grant.get("resource") or "", set()).update(grant.get("actions") or [])
        after = translate_legacy(held, BRANCH_LEGACY_ACCOUNTS)
        if after != held:
            # Not a change anyone made, so the revision stays: the branch sends its own copy of the same change.
            staff.permissions = [{"resource": r, "actions": sorted(a)} for r, a in sorted(after.items())]
            await staff.save(update_fields=["permissions", "updated_at"])
    await Counter.create(id="rollout:accounts-split", value=1)
    return changed
