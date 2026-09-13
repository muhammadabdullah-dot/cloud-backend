from datetime import datetime

from pydantic import BaseModel, field_validator


class BranchCreate(BaseModel):
    code: str
    name: str
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    timezone: str = "Asia/Karachi"
    syncUrl: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        # Upper-cased and stripped on the way in so "ho", "HO " and "Ho" can never become three
        # different branches — the code is the natural key a branch server will identify by.
        code = v.strip().upper()
        if not code:
            raise ValueError("Branch code is required")
        return code

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("Branch name is required")
        return name


class BranchUpdate(BaseModel):
    """Every field optional — a PATCH only touches what it names. `code` is deliberately absent:
    it is the identity other records point at, so it is set once at registration."""
    name: str | None = None
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    timezone: str | None = None
    syncUrl: str | None = None
    status: str | None = None


class BranchOut(BaseModel):
    id: str
    code: str
    name: str
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    timezone: str
    syncUrl: str | None = None
    status: str
    lastSeenAt: datetime | None = None
    createdAt: datetime

    # --- Pairing state -------------------------------------------------------------------------
    # Note what is NOT here: the pairing key itself. Reading the branch list is open to Executive
    # and Warehouse (they need to name the branch they're working with), so a plaintext credential
    # on this shape would hand a branch's identity to anyone who can see a dropdown. The key is
    # returned by exactly two endpoints, both behind System Admin: creating the branch, and
    # explicitly issuing a new key.
    verified: bool = False
    verifiedAt: datetime | None = None
    claimedFrom: str | None = None
    hasPairingKey: bool = False
    pairingExpiresAt: datetime | None = None


class BranchCreatedOut(BranchOut):
    """The create response, and the one moment the new branch's key is visible. Whoever registered
    the branch is the person who has to hand the key over, so this is where it belongs — and if it
    is lost before it reaches the shop, `POST /branches/{id}/pairing` issues a fresh one."""

    pairingKey: str


class BranchListOut(BaseModel):
    items: list[BranchOut]
    total: int
