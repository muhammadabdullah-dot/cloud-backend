"""Branch stock requests, whole: every Item on a request, the godown's stock beside each, and deciding with changed
quantities or a written reason. See services/requisition_service.py. The older one-Item routes under
/warehouse/requisitions stay for the Decisions inbox and the dashboard."""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.schemas.requisitions import ApproveIn, BranchRequestListOut, BranchRequestOut, DeclineIn, RecordIn
from app.services import requisition_service
from app.services.requisition_service import RequisitionError

router = APIRouter(prefix="/warehouse/branch-requests", tags=["warehouse"])

_read = require_any_permission(("warehouse.requisitions", "R"), ("warehouse.decisions", "R"))
_record = require_permission("warehouse.requisitions", "W")
_decide = require_permission("warehouse.requisitions.approve", "X")


async def _one(requisition_id: str) -> BranchRequestOut:
    rows, _ = await requisition_service.list_detailed(None, 1, 0, only_id=requisition_id)
    if rows:
        return BranchRequestOut(**rows[0])
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")


@router.get("", response_model=BranchRequestListOut)
async def list_requests(
    status_: str | None = Query(default=None, alias="status"), limit: int = 200, offset: int = 0, user: User = Depends(_read),
) -> BranchRequestListOut:
    rows, total = await requisition_service.list_detailed(status_, limit, offset)
    return BranchRequestListOut(items=[BranchRequestOut(**row) for row in rows], total=total)


@router.post("", response_model=BranchRequestOut)
async def record(payload: RecordIn, user: User = Depends(_record)) -> BranchRequestOut:
    """Write down a request a branch phoned in, usually one without its own server."""
    try:
        req = await requisition_service.record_for_branch(
            user, payload.branchId, payload.sourceBranchId, payload.reason, payload.neededBy, [(l.productId, l.qty) for l in payload.lines],
        )
    except RequisitionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    return await _one(str(req.id))


@router.post("/{requisition_id}/approve", response_model=BranchRequestOut)
async def approve(requisition_id: str, payload: ApproveIn, user: User = Depends(_decide)) -> BranchRequestOut:
    """Approve with the quantities head office will send. The transfer is made at once."""
    quantities = {l.productId: l.qty for l in payload.lines} if payload.lines is not None else None
    try:
        await requisition_service.approve(user, requisition_id, quantities, payload.note)
    except RequisitionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    return await _one(requisition_id)


@router.post("/{requisition_id}/decline", response_model=BranchRequestOut)
async def decline(requisition_id: str, payload: DeclineIn, user: User = Depends(_decide)) -> BranchRequestOut:
    try:
        await requisition_service.decline(user, requisition_id, payload.reason)
    except RequisitionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    return await _one(requisition_id)
