from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.controllers import rbac_controller
from app.core.resources import RBAC_MANAGEMENT_RESOURCE
from app.middlewares.auth import get_current_user, require_permission
from app.models import Branch, User
from app.routes.registration import branch_credential
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
_branch_staff_read = require_permission("admin.branch-staff", "R")


class AbilityOut(BaseModel):
    key: str
    resource: str
    action: str
    label: str
    hint: str = ""


class GridColumnOut(BaseModel):
    action: str
    label: str


class GridRowOut(BaseModel):
    resource: str
    label: str
    hint: str = ""


class GridOut(BaseModel):
    columns: list[GridColumnOut]
    rows: list[GridRowOut]


class AbilityGroupOut(BaseModel):
    key: str
    label: str
    abilities: list[AbilityOut]
    # A group that reads best as rows and columns (the account areas: See and Use).
    grid: GridOut | None = None


class PresetOut(BaseModel):
    roleId: str
    label: str
    abilities: list[str]
    # Branch starting points carry their discount limit; head office roles don't have one.
    discountLimit: str | None = None


class AbilitiesOut(BaseModel):
    groups: list[AbilityGroupOut]
    presets: list[PresetOut]


class AckOut(BaseModel):
    recorded: int


@router.get("/resources", response_model=list[str])
async def resources(user: User = Depends(_read)) -> list[str]:
    return rbac_controller.resources()


@router.get("/abilities", response_model=AbilitiesOut)
async def abilities(user: User = Depends(_read)) -> AbilitiesOut:
    """Everything a person at head office can be given, grouped and in plain words, with each role's standard access."""
    return AbilitiesOut(**await rbac_controller.abilities())


@router.get("/branch-abilities", response_model=AbilitiesOut)
async def branch_abilities(user: User = Depends(_branch_staff_read)) -> AbilitiesOut:
    """What a branch account can be given, in the branch's own words, as the branch reported it."""
    from app.services import staff_sync_service

    return AbilitiesOut(**await staff_sync_service.branch_abilities())


@router.post("/branch-abilities", response_model=AckOut)
async def branch_abilities_push(payload: AbilitiesOut, branch: Branch = Depends(branch_credential)) -> AckOut:
    """A branch server's access screen wording, sent when it starts, after its manifest."""
    from app.services import staff_sync_service

    await staff_sync_service.save_branch_abilities(branch, payload.model_dump())
    return AckOut(recorded=1)


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
    return await rbac_controller.create_user(payload, caller)


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
