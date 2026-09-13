from fastapi import APIRouter, Depends, Response, status

from app.controllers import branch_controller
from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.schemas.branches import (
    BranchCreate,
    BranchCreatedOut,
    BranchListOut,
    BranchOut,
    BranchUpdate,
)

router = APIRouter(prefix="/branches", tags=["branches"])

# Registering a branch is a System Admin act (it is what Admin → Sync Endpoints does today).
_write = require_permission("admin.sync-endpoints", "W")
# Reading the branch list is not: Executive rolls up by branch, and the Warehouse picks a
# destination branch for a requisition or transfer. Gating reads on the admin resource alone
# would leave those screens unable to name the branch they are working with.
_read = require_any_permission(
    ("admin.sync-endpoints", "R"), ("executive.branches", "R"), ("warehouse.requisitions", "R"), ("warehouse.transfers", "R"),
)


@router.get("", response_model=BranchListOut)
async def list_branches(q: str | None = None, limit: int = 100, offset: int = 0, user: User = Depends(_read)) -> BranchListOut:
    return await branch_controller.list_all(q, limit, offset)


@router.post("", response_model=BranchCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_branch(payload: BranchCreate, user: User = Depends(_write)) -> BranchCreatedOut:
    """Creates the branch and mints its one-time verification key. The key is in this response and
    nowhere else — show it to the admin now, or issue a fresh one later."""
    return await branch_controller.create(payload)


@router.get("/{branch_id}", response_model=BranchOut)
async def get_branch(branch_id: str, user: User = Depends(_read)) -> BranchOut:
    return await branch_controller.get(branch_id)


@router.patch("/{branch_id}", response_model=BranchOut)
async def update_branch(branch_id: str, payload: BranchUpdate, user: User = Depends(_write)) -> BranchOut:
    return await branch_controller.update(branch_id, payload)


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_branch(branch_id: str, user: User = Depends(_write)) -> Response:
    await branch_controller.delete(branch_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
