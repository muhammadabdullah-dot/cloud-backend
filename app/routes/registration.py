"""The two doors a branch server knocks on.

Neither is behind the user JWT, and that is deliberate rather than an oversight. A branch that has
not been claimed yet has no credential of any kind, so requiring one would make claiming
impossible; a branch that *has* been claimed authenticates as itself, not as a person, because no
human is present at 2am when the scheduler fires. The credential in the headers is the whole
authentication here, and every handler re-checks it — there is no "trusted network" assumption.

`/branches/{id}/pairing` and `/branches/{id}/pairing/revoke` are the other side of the same
workflow and *are* behind System Admin, because issuing and revoking credentials is an office act.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.controllers import registration_controller
from app.middlewares.auth import require_permission
from app.models import Branch, User
from app.schemas.registration import (
    ClaimIn,
    ClaimOut,
    PairingOut,
    SnapshotIn,
    SnapshotOut,
    StockChunkIn,
    StockChunkOut,
    StockCompleteIn,
    StockCompleteOut,
    SyncHelloOut,
    SyncPushIn,
    SyncPushOut,
)
from app.services import registration_service, snapshot_service
from app.services.registration_service import RegistrationError

router = APIRouter(prefix="/registration", tags=["registration"])
sync_router = APIRouter(prefix="/sync", tags=["sync"])
admin_router = APIRouter(prefix="/branches", tags=["branches"])


async def branch_credential(
    x_branch_code: str | None = Header(default=None, alias="X-Branch-Code"),
    x_branch_secret: str | None = Header(default=None, alias="X-Branch-Secret"),
) -> Branch:
    """Who is calling. Separate headers rather than `Authorization: Bearer` so a branch credential
    can never be mistaken for a user's JWT by a handler that takes either."""
    try:
        return await registration_service.authenticate(x_branch_code, x_branch_secret)
    except RegistrationError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.post("/claim", response_model=ClaimOut)
async def claim(payload: ClaimIn) -> ClaimOut:
    """Spend a pairing key and become a verified branch. Succeeds exactly once per branch."""
    return await registration_controller.claim(payload)


@sync_router.get("/hello", response_model=SyncHelloOut)
async def hello(branch: Branch = Depends(branch_credential)) -> SyncHelloOut:
    """Is the Cloud reachable and is this credential still good? The branch's own status screen
    calls this, and so does the scheduler before it bothers building a batch."""
    return await registration_controller.hello(branch)


@sync_router.post("/push", response_model=SyncPushOut)
async def push(payload: SyncPushIn, branch: Branch = Depends(branch_credential)) -> SyncPushOut:
    """Receive a batch of branch events. Idempotent — re-sending a batch stores nothing twice."""
    return await registration_controller.push(branch, payload)


@sync_router.post("/snapshot", response_model=SnapshotOut)
async def push_snapshot(payload: SnapshotIn, branch: Branch = Depends(branch_credential)) -> SnapshotOut:
    """Receive a branch's aggregate picture of itself — trading days, product-days, cashiers,
    hours, till closes, tenders, overrides, returns, credit customers, stock alerts.

    This is what actually feeds the Executive dashboard. Replaces everything previously held for
    this branch: a snapshot is a fresh picture, not an increment."""
    result = await snapshot_service.apply_aggregates(branch, payload.model_dump(), source="sync")
    return SnapshotOut(**result, serverTime=datetime.now(timezone.utc))


@sync_router.post("/stock", response_model=StockChunkOut)
async def push_stock_chunk(payload: StockChunkIn, branch: Branch = Depends(branch_credential)) -> StockChunkOut:
    """One chunk of the item-level stock list. Invisible to readers until `/sync/stock/complete`."""
    await snapshot_service.begin_stock(branch, payload.snapshotId)
    received = await snapshot_service.add_stock_chunk(branch, payload.snapshotId, payload.rows)
    return StockChunkOut(snapshotId=payload.snapshotId, received=received)


@sync_router.post("/stock/complete", response_model=StockCompleteOut)
async def complete_stock(payload: StockCompleteIn, branch: Branch = Depends(branch_credential)) -> StockCompleteOut:
    """The branch says its stock picture is whole. Promote it; drop the previous generation."""
    result = await snapshot_service.complete_stock(branch, payload.snapshotId)
    return StockCompleteOut(snapshotId=payload.snapshotId, **result, serverTime=datetime.now(timezone.utc))


# --- Admin side of the same workflow ----------------------------------------------------------

_write = require_permission("admin.sync-endpoints", "W")


@admin_router.post("/{branch_id}/pairing", response_model=PairingOut, status_code=status.HTTP_201_CREATED)
async def issue_pairing(branch_id: str, user: User = Depends(_write)) -> PairingOut:
    """Mint this branch's verification key. Shown to the admin, handed to the branch."""
    return await registration_controller.issue_pairing(branch_id)


@admin_router.post("/{branch_id}/pairing/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_pairing(branch_id: str, user: User = Depends(_write)) -> Response:
    """Un-pair a branch so it can be set up again. Clears the credential only — the branch's
    history stays."""
    await registration_controller.revoke_pairing(branch_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
