from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    roleId: str
    landing: str


class PermissionOut(BaseModel):
    resource: str
    actions: list[str]


class LoginResponse(BaseModel):
    token: str
    user: UserOut
    permissions: list[PermissionOut]


class MeResponse(BaseModel):
    user: UserOut
    permissions: list[PermissionOut]
