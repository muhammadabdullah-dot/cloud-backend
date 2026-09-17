from decimal import Decimal

from fastapi import HTTPException, status

from app.models import User
from app.schemas.rbac import (
    RoleOut,
    UpdatePermissionsRequest,
    UserCreate,
    UserNameOut,
    UserSummaryOut,
    UserUpdate,
)
from app.services import rbac_service


def _to_summary(u: User) -> UserSummaryOut:
    return UserSummaryOut(id=str(u.id), name=u.name, email=u.email, roleId=u.role_id, active=u.active, poLimit=None if u.po_limit is None else format(u.po_limit.quantize(Decimal('0.01')), 'f'))


def resources() -> list[str]:
    return rbac_service.list_resources()


async def abilities() -> dict:
    return await rbac_service.abilities_catalog()


async def list_users() -> list[UserSummaryOut]:
    return [_to_summary(u) for u in await rbac_service.list_users()]


async def list_user_names() -> list[UserNameOut]:
    return [UserNameOut(id=str(u.id), name=u.name) for u in await rbac_service.list_users()]


async def list_roles() -> list[RoleOut]:
    return [RoleOut(id=r.id, name=r.name, landing=r.landing) for r in await rbac_service.list_roles()]


async def create_user(payload: UserCreate, caller: User | None = None) -> UserSummaryOut:
    try:
        return _to_summary(await rbac_service.create_user(payload, caller))
    except rbac_service.RbacError as exc:
        raise HTTPException(exc.status, exc.message) from exc


async def update_user(user_id: str, payload: UserUpdate, caller: User) -> UserSummaryOut:
    if user_id == str(caller.id) and payload.model_dump(exclude_unset=True).get("password"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Change your own password from My account, because it needs your current password.")
    try:
        user = await rbac_service.update_user(user_id, payload, caller)
    except rbac_service.RbacError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return _to_summary(user)


async def get_permissions(user_id: str):
    permissions = await rbac_service.get_user_permissions(user_id)
    if permissions is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return permissions


async def update_permissions(user_id: str, payload: UpdatePermissionsRequest, caller: User):
    if user_id == str(caller.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't change your own access. Ask another admin.")
    try:
        permissions = await rbac_service.replace_user_permissions(user_id, payload.permissions, caller)
    except rbac_service.RbacError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if permissions is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return permissions
