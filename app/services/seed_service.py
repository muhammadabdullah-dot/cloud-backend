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
