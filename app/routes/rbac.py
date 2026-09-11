from fastapi import APIRouter, Depends

from app.controllers import rbac_controller
from app.core.resources import RBAC_MANAGEMENT_RESOURCE
from app.middlewares.auth import require_permission
from app.models import User
from app.schemas.auth import PermissionOut
from app.schemas.rbac import UpdatePermissionsRequest, UserSummaryOut

router = APIRouter(prefix="/rbac", tags=["rbac"])
users_router = APIRouter(prefix="/users", tags=["rbac"])

_read = require_permission(RBAC_MANAGEMENT_RESOURCE, "R")
_write = require_permission(RBAC_MANAGEMENT_RESOURCE, "W")


@router.get("/resources", response_model=list[str])
async def resources(user: User = Depends(_read)) -> list[str]:
    return rbac_controller.resources()


@users_router.get("", response_model=list[UserSummaryOut])
async def list_users(user: User = Depends(_read)) -> list[UserSummaryOut]:
    return await rbac_controller.list_users()


@users_router.get("/{user_id}/permissions", response_model=list[PermissionOut])
async def get_permissions(user_id: str, user: User = Depends(_read)) -> list[PermissionOut]:
    return await rbac_controller.get_permissions(user_id)


@users_router.put("/{user_id}/permissions", response_model=list[PermissionOut])
async def update_permissions(
    user_id: str, payload: UpdatePermissionsRequest, caller: User = Depends(_write)
) -> list[PermissionOut]:
    return await rbac_controller.update_permissions(user_id, payload, caller)
