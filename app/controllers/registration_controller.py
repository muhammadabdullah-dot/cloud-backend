from datetime import datetime, timezone

from fastapi import HTTPException

from app.models import Branch
from app.schemas.registration import (
    ClaimIn,
    ClaimOut,
    PairingOut,
    SyncHelloOut,
    SyncPushIn,
    SyncPushOut,
)
from app.services import branch_service, registration_service
from app.services.registration_service import RegistrationError


def _http(exc: RegistrationError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def claim(payload: ClaimIn) -> ClaimOut:
    try:
        branch, secret = await registration_service.claim(
            payload.code, payload.pairingKey, payload.syncUrl, payload.claimedFrom,
        )
    except RegistrationError as exc:
        raise _http(exc) from exc
    return ClaimOut(
        branchId=str(branch.id), code=branch.code, name=branch.name, address=branch.address,
        city=branch.city, phone=branch.phone, timezone=branch.timezone,
        verifiedAt=branch.verified_at, syncSecret=secret,
    )


async def issue_pairing(branch_id: str) -> PairingOut:
    branch = await branch_service.get(branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    try:
        code = await registration_service.issue_pairing(branch)
    except RegistrationError as exc:
        raise _http(exc) from exc
    return PairingOut(
        branchId=str(branch.id), code=branch.code, name=branch.name,
        pairingKey=code, expiresAt=branch.pairing_expires_at,
    )


async def revoke_pairing(branch_id: str) -> None:
    branch = await branch_service.get(branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    await registration_service.revoke_pairing(branch)


async def hello(branch: Branch) -> SyncHelloOut:
    await registration_service.touch(branch)
    return SyncHelloOut(
        branchId=str(branch.id), code=branch.code, name=branch.name, status=branch.status,
        serverTime=datetime.now(timezone.utc),
    )


async def push(branch: Branch, payload: SyncPushIn) -> SyncPushOut:
    events = [e.model_dump() for e in payload.events]
    run, acknowledged = await registration_service.ingest(branch, events)
    return SyncPushOut(
        received=run.event_count, accepted=run.accepted_count, duplicates=run.duplicate_count,
        acknowledgedIds=acknowledged, serverTime=datetime.now(timezone.utc),
    )
