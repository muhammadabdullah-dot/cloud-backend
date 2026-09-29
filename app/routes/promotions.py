"""Campaigns: head office writes one, somebody else approves it, and every branch gets it.

Two permissions on purpose. Writing a campaign and approving one are separate rights, because the approval is a
second person's act and a right one person holds both halves of is not an approval at all. Their own software has the
same step and used it: 10,893 of their 11,364 campaigns are approved and 441 are not.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from app.middlewares.auth import require_permission
from app.models import User
from app.services import promotions_service as svc

router = APIRouter(prefix="/promotions", tags=["promotions"])

_read = require_permission("warehouse.promotions", "R")
_write = require_permission("warehouse.promotions", "W")
_approve = require_permission("warehouse.promotions.approve", "W")


async def _guard(call, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except svc.PromotionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)


@router.get("")
async def list_promotions(state: str | None = None, user: User = Depends(_read)) -> list[dict]:
    return await svc.listing(state)


@router.post("")
async def create_promotion(payload: dict, user: User = Depends(_write)) -> dict:
    """Write one. It reaches nobody until it is approved and sent."""
    return svc.out(await _guard(svc.create, payload, user.name))


@router.get("/{promo_id}")
async def get_promotion(promo_id: str, user: User = Depends(_read)) -> dict:
    promo = await _guard(svc._get, promo_id)
    return {**svc.out(promo), "givenAway": await svc.given_away(promo)}


@router.patch("/{promo_id}")
async def update_promotion(promo_id: str, payload: dict, user: User = Depends(_write)) -> dict:
    """Only while it is a draft or approved. A live campaign is stopped and replaced, never edited."""
    return svc.out(await _guard(svc.update, promo_id, payload, user.name))


@router.post("/{promo_id}/approve")
async def approve_promotion(promo_id: str, user: User = Depends(_approve)) -> dict:
    return svc.out(await _guard(svc.approve, promo_id, user.name))


@router.post("/{promo_id}/publish")
async def publish_promotion(promo_id: str, user: User = Depends(_approve)) -> dict:
    promo, sent = await _guard(svc.publish, promo_id, user.name)
    return {**svc.out(promo), "sentToBranches": sent}


@router.post("/{promo_id}/stop")
async def stop_promotion(promo_id: str, user: User = Depends(_approve)) -> dict:
    """Withdraw it. It goes down again switched off, so a till stops selling under it and can still say what it gave."""
    promo, sent = await _guard(svc.stop, promo_id, user.name)
    return {**svc.out(promo), "sentToBranches": sent}
