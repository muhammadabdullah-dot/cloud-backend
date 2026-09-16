from decimal import Decimal

from pydantic import BaseModel, field_validator

from app.schemas.auth import PermissionOut


class UserSummaryOut(BaseModel):
    id: str
    name: str
    email: str
    roleId: str
    active: bool
    # The largest purchase order they may approve; null = their role's usual limit.
    poLimit: str | None = None


class UserNameOut(BaseModel):
    """Just enough to render "who did this" on a record — no email, role or permissions."""
    id: str
    name: str


class RoleOut(BaseModel):
    id: str
    name: str
    landing: str


class UserCreate(BaseModel):
    name: str
    email: str
    password: str
    roleId: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        email = v.strip().lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("Name is required")
        return name

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class UserUpdate(BaseModel):
    """All optional — a PATCH touches only what it names. `password` here is an admin reset, not
    a change-your-own-password flow (that needs the old password and doesn't exist yet)."""
    name: str | None = None
    email: str | None = None
    roleId: str | None = None
    active: bool | None = None
    password: str | None = None
    poLimit: Decimal | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        email = v.strip().lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("Enter a valid email address")
        return email

    @field_validator("password")
    @classmethod
    def _password(cls, v: str | None) -> str | None:
        if v is not None and len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class UpdatePermissionsRequest(BaseModel):
    permissions: list[PermissionOut]
