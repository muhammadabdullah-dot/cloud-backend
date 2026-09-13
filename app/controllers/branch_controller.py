from fastapi import HTTPException, status

from app.models import Branch
from app.schemas.branches import (
    BranchCreate,
    BranchCreatedOut,
    BranchListOut,
    BranchOut,
    BranchUpdate,
)
from app.services import branch_service, registration_service


def _fields(b: Branch) -> dict:
    return dict(
        id=str(b.id), code=b.code, name=b.name, address=b.address, city=b.city, phone=b.phone,
        timezone=b.timezone, syncUrl=b.sync_url, status=b.status,
        lastSeenAt=b.last_seen_at, createdAt=b.created_at,
        verified=b.verified_at is not None, verifiedAt=b.verified_at, claimedFrom=b.claimed_from,
        hasPairingKey=bool(b.pairing_code), pairingExpiresAt=b.pairing_expires_at,
    )


def _to_out(b: Branch) -> BranchOut:
    return BranchOut(**_fields(b))


async def list_all(q: str | None, limit: int, offset: int) -> BranchListOut:
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    items, total = await branch_service.list_all(q, limit, offset)
    return BranchListOut(items=[_to_out(b) for b in items], total=total)


async def get(branch_id: str) -> BranchOut:
    branch = await branch_service.get(branch_id)
    if not branch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")
    return _to_out(branch)


async def create(payload: BranchCreate) -> BranchCreatedOut:
    """Registering a branch mints its verification key in the same act. Two steps would mean a
    branch could sit in the list looking registered while being unable to connect to anything —
    the state a person reads as "done" has to be the state that actually works."""
    try:
        branch = await branch_service.create(payload)
    except branch_service.BranchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message)
    key = await registration_service.issue_pairing(branch)
    return BranchCreatedOut(**_fields(branch), pairingKey=key)


async def update(branch_id: str, payload: BranchUpdate) -> BranchOut:
    try:
        branch = await branch_service.update(branch_id, payload)
    except branch_service.BranchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    if not branch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")
    return _to_out(branch)


async def delete(branch_id: str) -> None:
    try:
        removed = await branch_service.delete(branch_id)
    except branch_service.BranchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")
