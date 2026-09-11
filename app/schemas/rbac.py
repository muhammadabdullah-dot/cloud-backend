from pydantic import BaseModel

from app.schemas.auth import PermissionOut


class UserSummaryOut(BaseModel):
    id: str
    name: str
    email: str
    roleId: str
    active: bool


class UpdatePermissionsRequest(BaseModel):
    permissions: list[PermissionOut]
