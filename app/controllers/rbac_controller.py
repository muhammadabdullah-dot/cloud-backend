from fastapi import HTTPException, status

from app.models import User
from app.schemas.rbac import UpdatePermissionsRequest, UserSummaryOut
from app.services import rbac_service


def resources() -> list[str]:
    return rbac_service.list_resources()


async def list_users() -> list[UserSummaryOut]:
    users = await rbac_service.list_users()
    return [UserSummaryOut(id=str(u.id), name=u.name, email=u.email, roleId=u.role_id, active=u.active) for u in users]


async def get_permissions(user_id: str):
    permissions = await rbac_service.get_user_permissions(user_id)
    if permissions is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return permissions


async def update_permissions(user_id: str, payload: UpdatePermissionsRequest, caller: User):
    if user_id == str(caller.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot edit your own permissions")
    permissions = await rbac_service.replace_user_permissions(user_id, payload.permissions, caller)
    if permissions is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return permissions
