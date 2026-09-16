"""Services are where ORM calls happen — no repository layer. RBAC enforcement and management both live here."""
from app.core.resources import resources_for_role
from app.core.resources import RESOURCES
from app.core.security import hash_password
from app.models import Role, RoleDefaultPermission, User, UserPermission
from app.schemas.auth import PermissionOut
from app.schemas.rbac import UserCreate, UserUpdate

_ACTION_FIELDS = {"R": "can_read", "W": "can_write", "X": "can_execute"}


class RbacError(Exception):
    def __init__(self, message: str):
        self.message = message


def _actions_from_flags(perm: UserPermission) -> list[str]:
    return [action for action, field in _ACTION_FIELDS.items() if getattr(perm, field)]


async def effective_permissions(user: User) -> list[PermissionOut]:
    rows = await UserPermission.filter(user=user)
    return [PermissionOut(resource=r.resource, actions=_actions_from_flags(r)) for r in rows if _actions_from_flags(r)]


async def has_permission(user: User, resource: str, action: str) -> bool:
    field = _ACTION_FIELDS[action]
    perm = await UserPermission.get_or_none(user=user, resource=resource)
    return bool(perm and getattr(perm, field))


# What stock cost the company is buying-side information: the people who receive it, value it or own the
# Item master see it. Floor staff (a Picker) find and move Items without it.
COST_VIEWERS = (
    ("warehouse.items.manage", "W"), ("warehouse.dashboard", "R"), ("warehouse.receiving", "R"),
    ("executive.stock", "R"), ("executive.dashboard", "R"),
)


async def can_see_costs(user: User) -> bool:
    for resource, action in COST_VIEWERS:
        if await has_permission(user, resource, action):
            return True
    return False


def list_resources() -> list[str]:
    return RESOURCES


async def list_users() -> list[User]:
    return await User.all().order_by("name")


async def list_roles() -> list[Role]:
    return await Role.all().order_by("name")


async def create_user(data: UserCreate) -> User:
    """Creates the account AND materializes its role's permission template into real
    UserPermission rows — the same thing the seed does. Without that second half the new user can
    sign in and then see nothing, which reads as a broken login rather than a missing grant."""
    if not await Role.exists(id=data.roleId):
        raise RbacError(f"No such role: {data.roleId}")
    if await User.filter(email=data.email).exists():
        raise RbacError(f"A user with email {data.email} already exists")

    user = await User.create(
        name=data.name, email=data.email, password_hash=hash_password(data.password), role_id=data.roleId,
    )
    templates = await RoleDefaultPermission.filter(role_id=data.roleId)
    if templates:
        for template in templates:
            await UserPermission.create(
                user=user, resource=template.resource, can_read=template.can_read,
                can_write=template.can_write, can_execute=template.can_execute, granted_by=None,
            )
    else:
        # A role created outside the seed has no template rows yet; fall back to the code-level
        # template so the account is still usable rather than silently permission-less.
        for resource in resources_for_role(data.roleId):
            await UserPermission.create(
                user=user, resource=resource, can_read=True, can_write=True, can_execute=True, granted_by=None,
            )
    return user


async def update_user(user_id: str, data: UserUpdate, caller: User) -> User | None:
    user = await User.get_or_none(id=user_id)
    if not user:
        return None

    fields = data.model_dump(exclude_unset=True)
    if "active" in fields and str(user.id) == str(caller.id) and fields["active"] is False:
        raise RbacError("You can't deactivate your own account")
    if "roleId" in fields and fields["roleId"] is not None:
        if not await Role.exists(id=fields["roleId"]):
            raise RbacError(f"No such role: {fields['roleId']}")
        user.role_id = fields["roleId"]
    if "email" in fields and fields["email"] is not None:
        if await User.filter(email=fields["email"]).exclude(id=user.id).exists():
            raise RbacError(f"A user with email {fields['email']} already exists")
        user.email = fields["email"]
    if "name" in fields and fields["name"] is not None:
        user.name = fields["name"].strip() or user.name
    if "active" in fields and fields["active"] is not None:
        user.active = fields["active"]
    if "password" in fields and fields["password"] is not None:
        user.password_hash = hash_password(fields["password"])
    if "poLimit" in fields:
        user.po_limit = fields["poLimit"]

    await user.save()
    return user


async def get_user_permissions(user_id: str) -> list[PermissionOut] | None:
    user = await User.get_or_none(id=user_id)
    if not user:
        return None
    return await effective_permissions(user)


async def replace_user_permissions(
    target_user_id: str, grants: list[PermissionOut], granted_by: User
) -> list[PermissionOut] | None:
    target = await User.get_or_none(id=target_user_id)
    if not target:
        return None
    await UserPermission.filter(user=target).delete()
    for grant in grants:
        await UserPermission.create(
            user=target,
            resource=grant.resource,
            can_read="R" in grant.actions,
            can_write="W" in grant.actions,
            can_execute="X" in grant.actions,
            granted_by=granted_by,
        )
    return await effective_permissions(target)
