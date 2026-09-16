from datetime import datetime

from pydantic import BaseModel, Field


class PermissionIn(BaseModel):
    resource: str
    actions: list[str]


class StaffBranchOut(BaseModel):
    branchId: str
    code: str
    name: str
    # Where head office's latest change for this person stands at this branch.
    delivery: str  # waiting · delivered · applied · failed · none
    deliveryError: str | None = None


class BranchStaffOut(BaseModel):
    id: str
    name: str
    email: str
    roleId: str
    active: bool
    permissions: list[PermissionIn]
    rev: int
    lastChangedAt: str
    lastChangedBy: str | None = None
    updatedAt: datetime
    branches: list[StaffBranchOut]


class BranchStaffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=180)
    password: str = Field(min_length=6, max_length=120)
    roleId: str
    branchIds: list[str] = Field(min_length=1)
    permissions: list[PermissionIn] | None = None


class BranchStaffUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, min_length=3, max_length=180)
    password: str | None = Field(default=None, min_length=6, max_length=120)
    roleId: str | None = None
    active: bool | None = None
    branchIds: list[str] | None = None
    permissions: list[PermissionIn] | None = None


class PasswordResetOut(BaseModel):
    staff: BranchStaffOut
    temporaryPassword: str


class BranchRoleOut(BaseModel):
    id: str
    name: str
    resources: list[str]
    exclude: list[str] = []
    managedByHeadOffice: bool = False


class BranchManifestOut(BaseModel):
    resources: list[str]
    roles: list[BranchRoleOut]


class RoleTemplateIn(BaseModel):
    resources: list[str]
    applyToExisting: bool = False


class RoleTemplateOut(BaseModel):
    role: BranchRoleOut
    staffUpdated: int


# ── sync: downstream ───────────────────────────────────────────────────────────────────────────

class PulledMessage(BaseModel):
    seq: int
    kind: str
    payload: dict
    createdAt: datetime


class BranchDirectoryEntry(BaseModel):
    code: str
    name: str
    city: str | None = None


class PullOut(BaseModel):
    messages: list[PulledMessage]
    latestSeq: int
    # Other branches this branch can send stock to.
    branches: list[BranchDirectoryEntry]
    serverTime: datetime


class AckResult(BaseModel):
    seq: int
    ok: bool
    error: str | None = None


class AckIn(BaseModel):
    results: list[AckResult]


class AckOut(BaseModel):
    recorded: int


class ManifestRole(BaseModel):
    id: str
    name: str
    resources: list[str]
    exclude: list[str] = []


class ManifestIn(BaseModel):
    resources: list[str]
    roles: list[ManifestRole]
