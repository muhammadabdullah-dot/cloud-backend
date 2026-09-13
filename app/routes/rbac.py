from fastapi import APIRouter, Depends, status

from app.controllers import rbac_controller
from app.core.resources import RBAC_MANAGEMENT_RESOURCE
from app.middlewares.auth import get_current_user, require_permission
from app.models import User
from app.schemas.auth import PermissionOut
from app.schemas.rbac import (
    RoleOut,
    UpdatePermissionsRequest,
    UserCreate,
    UserNameOut,
    UserSummaryOut,
    UserUpdate,
)

router = APIRouter(prefix="/rbac", tags=["rbac"])
users_router = APIRouter(prefix="/users", tags=["rbac"])

_read = require_permission(RBAC_MANAGEMENT_RESOURCE, "R")
_write = require_permission(RBAC_MANAGEMENT_RESOURCE, "W")


@router.get("/resources", response_model=list[str])
async def resources(user: User = Depends(_read)) -> list[str]:
    return rbac_controller.resources()


@router.get("/roles", response_model=list[RoleOut])
async def roles(user: User = Depends(_read)) -> list[RoleOut]:
    """The roles a new user can be created into. Needed by the create-user form — without it the
    client would have to hardcode the role list and drift from the server's."""
    return await rbac_controller.list_roles()


@users_router.get("", response_model=list[UserSummaryOut])
async def list_users(user: User = Depends(_read)) -> list[UserSummaryOut]:
    return await rbac_controller.list_users()


@users_router.get("/names", response_model=list[UserNameOut])
async def list_user_names(user: User = Depends(get_current_user)) -> list[UserNameOut]:
    """Id → display name, readable by any signed-in user. Records across the app store a user id;
    gating names behind the admin resource means everyone else reads them as a raw id."""
    return await rbac_controller.list_user_names()


@users_router.post("", response_model=UserSummaryOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, caller: User = Depends(_write)) -> UserSummaryOut:
    return await rbac_controller.create_user(payload)


@users_router.patch("/{user_id}", response_model=UserSummaryOut)
async def update_user(user_id: str, payload: UserUpdate, caller: User = Depends(_write)) -> UserSummaryOut:
    return await rbac_controller.update_user(user_id, payload, caller)


@users_router.get("/{user_id}/permissions", response_model=list[PermissionOut])
async def get_permissions(user_id: str, user: User = Depends(_read)) -> list[PermissionOut]:
    return await rbac_controller.get_permissions(user_id)


@users_router.put("/{user_id}/permissions", response_model=list[PermissionOut])
async def update_permissions(
    user_id: str, payload: UpdatePermissionsRequest, caller: User = Depends(_write)
) -> list[PermissionOut]:
    return await rbac_controller.update_permissions(user_id, payload, caller)
