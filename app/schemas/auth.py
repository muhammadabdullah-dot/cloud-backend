from pydantic import BaseModel, Field


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


class ChangePasswordRequest(BaseModel):
    """Changing your own password: the current one proves it's you, the new one twice guards against a typo."""

    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=1, max_length=128)
    confirmPassword: str = Field(min_length=1, max_length=128)


class MyNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
