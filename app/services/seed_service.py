"""Day-one seed: exactly the accounts in frontend-baseline.md §1.1, with RoleDefaultPermission
templates materialized into each seeded user's real UserPermission rows (contracts.md §2.3).
Idempotent — a no-op if roles already exist, so it's safe to run on every startup.
"""
from app.core.resources import resources_for_role
from app.core.security import hash_password
from app.models import Role, RoleDefaultPermission, User, UserPermission

ROLES = [
    ("warehouse-manager", "Warehouse Manager", "/warehouse/dashboard"),
    ("picker", "Picker / Packer", "/warehouse/picking"),
    ("executive", "Owner / Executive", "/executive/dashboard"),
    ("system-admin", "System Admin", "/admin"),
]

USERS = [
    ("warehousemanager@cloud.dmarina.pk", "warehouse123", "warehouse-manager", "Warehouse Manager"),
    ("picker@cloud.dmarina.pk", "picker123", "picker", "Picker / Packer"),
    ("executive@cloud.dmarina.pk", "exec123", "executive", "Owner / Executive"),
    ("sysadmin@cloud.dmarina.pk", "admin123", "system-admin", "System Admin"),
]


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
