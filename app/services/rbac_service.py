"""Services are where ORM calls happen — no repository layer. RBAC enforcement and management both live here."""
from app.core.resources import RESOURCES
from app.models import User, UserPermission
from app.schemas.auth import PermissionOut

_ACTION_FIELDS = {"R": "can_read", "W": "can_write", "X": "can_execute"}


def _actions_from_flags(perm: UserPermission) -> list[str]:
    return [action for action, field in _ACTION_FIELDS.items() if getattr(perm, field)]


async def effective_permissions(user: User) -> list[PermissionOut]:
    rows = await UserPermission.filter(user=user)
    return [PermissionOut(resource=r.resource, actions=_actions_from_flags(r)) for r in rows if _actions_from_flags(r)]


async def has_permission(user: User, resource: str, action: str) -> bool:
    field = _ACTION_FIELDS[action]
    perm = await UserPermission.get_or_none(user=user, resource=resource)
    return bool(perm and getattr(perm, field))


def list_resources() -> list[str]:
    return RESOURCES


async def list_users() -> list[User]:
    return await User.all()


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
