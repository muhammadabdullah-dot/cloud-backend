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
    # The one branch this account may raise purchase orders for, and whose orders it sees. Null for everybody who
    # buys for the company, which is everybody until somebody makes a scoped account. It is a scope and not a
    # permission: what they may DO is still their grants, this only narrows who they may do it for.
    #
    # The id alone, not the code or the name. `_to_summary` is handed a User with nothing prefetched, so reading
    # the branch row would be a query for every user in the list, and the screen that shows this already holds
    # the branch list for its own dropdown.
    poBranchId: str | None = None


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
    # Set it here to make a branch-scoped buying account in one step, which is what the pharmacy case needs: the
    # screen asks for the branch, creates the account, and grants it the purchasing resources. Left out, the
    # account buys for the company like any other.
    poBranchId: str | None = None

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
    # A PATCH touches only what it names, so leaving this out keeps whatever scope the account has. To REMOVE a
    # scope, send it explicitly as null -- see `rbac_service.update_user`, which distinguishes the two.
    poBranchId: str | None = None

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
