"""Head office's side of branch staff and branch sync."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.middlewares.auth import require_permission
from app.models import Branch, BranchMessage, BranchStaff, User
from app.routes.registration import branch_credential
from app.schemas.branch_staff import (
    AckIn, AckOut, BranchDirectoryEntry, BranchManifestOut, BranchRoleOut, BranchStaffCreate, BranchStaffOut,
    BranchStaffUpdate, ManifestIn, PasswordResetOut, PermissionIn, PullOut, PulledMessage, RoleTemplateIn,
    RoleTemplateOut, StaffBranchOut,
)
from app.services import downstream_service, staff_sync_service

router = APIRouter(prefix="/branch-staff", tags=["branch-staff"])
sync_router = APIRouter(prefix="/sync", tags=["sync"])

_read = require_permission("admin.branch-staff", "R")
_write = require_permission("admin.branch-staff", "W")


def _raise(exc: staff_sync_service.StaffError) -> None:
    raise HTTPException(exc.status, exc.message)


async def _delivery_index() -> dict[tuple[str, str], BranchMessage]:
    """The latest staff message per (person, branch), to say whether head office's change has landed."""
    latest: dict[tuple[str, str], BranchMessage] = {}
    for message in await BranchMessage.filter(kind__in=["staff.upsert", "staff.remove"]).order_by("-created_at").limit(5000):
        body = message.payload or {}
        person = (body.get("user") or {}).get("id") or body.get("userId")
        key = (str(person), str(message.branch_id))
        latest.setdefault(key, message)
    return latest


def _delivery(message: BranchMessage | None) -> tuple[str, str | None]:
    if message is None:
        return "none", None
    if message.apply_error:
        return "failed", message.apply_error
    if message.applied_at:
        return "applied", None
    if message.delivered_at:
        return "delivered", None
    return "waiting", None


def _out(staff: BranchStaff, branches: list[Branch], deliveries: dict) -> BranchStaffOut:
    rows = []
    for branch in branches:
        state, error = _delivery(deliveries.get((staff.id, str(branch.id))))
        rows.append(StaffBranchOut(branchId=str(branch.id), code=branch.code, name=branch.name, delivery=state, deliveryError=error))
    return BranchStaffOut(
        id=staff.id, name=staff.name, email=staff.email, roleId=staff.role_id, active=staff.active,
        title=staff.title, discountLimit=None if staff.discount_limit is None else format(staff.discount_limit.normalize(), "f"),
        permissions=[PermissionIn(**p) for p in staff_sync_service._norm_permissions(staff.permissions)],
        rev=staff.rev, lastChangedAt=staff.last_changed_at, lastChangedBy=staff.last_changed_by,
        updatedAt=staff.updated_at, branches=sorted(rows, key=lambda r: r.code),
    )


async def _one(staff: BranchStaff) -> BranchStaffOut:
    listed = await staff_sync_service.list_staff()
    branches = next((b for s, b in listed if s.id == staff.id), [])
    return _out(staff, branches, await _delivery_index())


@router.get("", response_model=list[BranchStaffOut])
async def list_staff(branchId: str | None = None, q: str | None = None, user: User = Depends(_read)) -> list[BranchStaffOut]:
    """Every branch account head office knows of, with the branches each works at."""
    deliveries = await _delivery_index()
    return [_out(s, b, deliveries) for s, b in await staff_sync_service.list_staff(branchId, q)]


@router.get("/manifest", response_model=BranchManifestOut)
async def manifest(user: User = Depends(_read)) -> BranchManifestOut:
    """What a branch account can be granted, and the branch roles with their standard access."""
    resources, roles = await staff_sync_service.manifest()
    return BranchManifestOut(resources=resources, roles=[BranchRoleOut(**r) for r in roles])


@router.post("", response_model=BranchStaffOut)
async def create_staff(payload: BranchStaffCreate, user: User = Depends(_write)) -> BranchStaffOut:
    try:
        staff = await staff_sync_service.create(user, payload.model_dump())
    except staff_sync_service.StaffError as exc:
        _raise(exc)
    return await _one(staff)


@router.patch("/{staff_id}", response_model=BranchStaffOut)
async def update_staff(staff_id: str, payload: BranchStaffUpdate, user: User = Depends(_write)) -> BranchStaffOut:
    try:
        staff = await staff_sync_service.update(user, staff_id, payload.model_dump(exclude_unset=True))
    except staff_sync_service.StaffError as exc:
        _raise(exc)
    return await _one(staff)


@router.post("/{staff_id}/reset-password", response_model=PasswordResetOut)
async def reset_password(staff_id: str, user: User = Depends(_write)) -> PasswordResetOut:
    """A new temporary password, shown once. Give it to the person; it works at every branch they're on
    once those branches have collected the change."""
    try:
        staff, temporary = await staff_sync_service.reset_password(user, staff_id)
    except staff_sync_service.StaffError as exc:
        _raise(exc)
    return PasswordResetOut(staff=await _one(staff), temporaryPassword=temporary)


@router.put("/roles/{role_id}", response_model=RoleTemplateOut)
async def set_role(role_id: str, payload: RoleTemplateIn, user: User = Depends(_write)) -> RoleTemplateOut:
    """Set a branch role's standard access for every branch. With `applyToExisting`, everyone already in
    the role is given exactly that access too."""
    try:
        _, changed = await staff_sync_service.set_role_template(user, role_id, payload.resources, payload.applyToExisting)
    except staff_sync_service.StaffError as exc:
        _raise(exc)
    _, roles = await staff_sync_service.manifest()
    role = next(r for r in roles if r["id"] == role_id)
    return RoleTemplateOut(role=BranchRoleOut(**role), staffUpdated=changed)


# ── branch-facing sync ─────────────────────────────────────────────────────────────────────────

@sync_router.get("/pull", response_model=PullOut)
async def pull(after: int = 0, limit: int = 200, branch: Branch = Depends(branch_credential)) -> PullOut:
    """Messages for this branch after its cursor, oldest first. Every message is an idempotent upsert."""
    messages, latest = await downstream_service.pull(branch, after, limit)
    others = await Branch.filter(verified_at__not_isnull=True, status="active").exclude(id=branch.id).order_by("name")
    return PullOut(
        messages=[PulledMessage(seq=m.seq, kind=m.kind, payload=m.payload, createdAt=m.created_at) for m in messages],
        latestSeq=latest,
        branches=[BranchDirectoryEntry(code=b.code, name=b.name, city=b.city) for b in others],
        serverTime=datetime.now(timezone.utc),
    )


@sync_router.post("/ack", response_model=AckOut)
async def ack(payload: AckIn, branch: Branch = Depends(branch_credential)) -> AckOut:
    """What the branch did with each message it pulled."""
    return AckOut(recorded=await downstream_service.ack(branch, [r.model_dump() for r in payload.results]))


@sync_router.post("/manifest", response_model=AckOut)
async def manifest_push(payload: ManifestIn, branch: Branch = Depends(branch_credential)) -> AckOut:
    """The branch software's grantable resources and roles, sent when a branch server starts."""
    await staff_sync_service.save_manifest(branch, payload.resources, [r.model_dump() for r in payload.roles])
    return AckOut(recorded=1)
