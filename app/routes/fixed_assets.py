"""Head office's fixed asset register over HTTP (book HO): the register and its check against the books, each asset's schedule, adding,
changing and disposing of assets, and the monthly depreciation run. See services/fixed_assets_service.py for the rules."""
from datetime import date
from decimal import Decimal
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.middlewares.auth import get_current_user
from app.models import HEAD_OFFICE_BOOK, User
from app.services import accounts_areas, accounts_chart_service, fixed_assets_service, vouchers_service
from app.services.accounts_areas import access_of
from app.services.rbac_service import has_permission

Day = date

router = APIRouter(prefix="/accounts/fixed-assets", tags=["fixed assets"])


def _need(resource: str, action: str, sentence: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if not await has_permission(user, resource, action):
            raise HTTPException(status.HTTP_403_FORBIDDEN, sentence)
        return user

    return checker


_read = _need("accounts.fixed-assets", "R", "Seeing the fixed asset register needs its own access. Ask your manager for it.")
_write = _need("accounts.fixed-assets", "W", "Adding, changing and disposing of fixed assets needs its own access. Ask your manager for it.")
_run = _need("accounts.fixed-assets", "X", "Preparing the month's depreciation needs its own access. Ask your manager for it.")

ERRORS = (fixed_assets_service.AssetError, vouchers_service.VoucherError, accounts_chart_service.ChartError)


async def _guard(call: Callable, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except accounts_areas.AreaRefused as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.message)
    except ERRORS as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)


class AssetIn(BaseModel):
    name: str | None = None
    categoryAccountId: str | None = None
    accumulatedAccountId: str | None = None
    expenseAccountId: str | None = None
    purchaseDate: Day | None = None
    cost: Decimal | None = None
    salvageValue: Decimal | None = None
    usefulLifeMonths: int | None = None
    method: str | None = None
    ratePercent: Decimal | None = None
    # "2026-09"
    depreciationStart: str | None = None
    openingAccumulated: Decimal | None = None
    location: str | None = None
    supplierRef: str | None = None
    notes: str | None = None


class DisposeIn(BaseModel):
    date: Day | None = None
    proceeds: Decimal = Decimal("0")
    accountId: str | None = None


class RunIn(BaseModel):
    month: str


def _payload(body: BaseModel) -> dict:
    return body.model_dump(mode="json", exclude_unset=True)


@router.get("")
async def register(book: str | None = None, user: User = Depends(_read)) -> dict:
    """The register as far as the person's areas go, with category totals and the check against the books."""
    if book and book.strip().upper() != HEAD_OFFICE_BOOK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This is head office's fixed asset register. Each branch keeps its own register at the branch.")
    return await _guard(fixed_assets_service.register, await access_of(user))


@router.get("/runs")
async def runs(user: User = Depends(_read)) -> list[dict]:
    await fixed_assets_service.refresh()
    return await fixed_assets_service.runs_out(await access_of(user))


@router.get("/runs/preview")
async def run_preview(month: str, user: User = Depends(_read)) -> dict:
    """What the month's depreciation voucher would hold. Anyone who sees the register can look; preparing it is separate."""
    return await _guard(fixed_assets_service.run_preview, month, await access_of(user))


@router.post("/runs")
async def prepare_run(payload: RunIn, user: User = Depends(_run)) -> dict:
    run = await _guard(fixed_assets_service.prepare_run, user, payload.month, await access_of(user))
    voucher = await vouchers_service.voucher_out(await vouchers_service.get(str(run.voucher_id)))
    return {"id": str(run.id), "month": f"{run.month:%Y-%m}", "status": run.status, "total": format(run.total, "f"), "voucher": voucher}


@router.post("/schedule-preview")
async def schedule_preview(payload: AssetIn, user: User = Depends(_read)) -> list[dict]:
    """The schedule an asset would have, while it is being entered."""
    try:
        return fixed_assets_service.preview_schedule(_payload(payload))
    except fixed_assets_service.AssetError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)


@router.post("")
async def create_asset(payload: AssetIn, user: User = Depends(_write)) -> dict:
    asset = await _guard(fixed_assets_service.create, user, _payload(payload), await access_of(user))
    return await fixed_assets_service.detail(str(asset.id), await access_of(user))


@router.get("/{asset_id}")
async def asset_detail(asset_id: str, user: User = Depends(_read)) -> dict:
    return await _guard(fixed_assets_service.detail, asset_id, await access_of(user))


@router.put("/{asset_id}")
async def update_asset(asset_id: str, payload: AssetIn, user: User = Depends(_write)) -> dict:
    asset = await _guard(fixed_assets_service.update, user, asset_id, _payload(payload), await access_of(user))
    return await fixed_assets_service.detail(str(asset.id), await access_of(user))


@router.delete("/{asset_id}")
async def delete_asset(asset_id: str, user: User = Depends(_write)) -> dict:
    await _guard(fixed_assets_service.delete, asset_id, await access_of(user))
    return {"deleted": True}


@router.post("/{asset_id}/dispose/preview")
async def disposal_preview(asset_id: str, payload: DisposeIn, user: User = Depends(_write)) -> dict:
    plan = await _guard(fixed_assets_service.disposal_plan, asset_id, _payload(payload), await access_of(user))
    return fixed_assets_service.plan_out(plan)


@router.post("/{asset_id}/dispose")
async def dispose(asset_id: str, payload: DisposeIn, user: User = Depends(_write)) -> dict:
    """Prepares the disposal as a draft journal. The asset counts as disposed of once that voucher is posted."""
    asset = await _guard(fixed_assets_service.dispose, user, asset_id, _payload(payload), await access_of(user))
    return await fixed_assets_service.detail(str(asset.id), await access_of(user))
