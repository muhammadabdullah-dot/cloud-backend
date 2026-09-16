"""Put-away over HTTP: where an Item sits, moving stock between bins, home bins, and a transfer's pick list."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.middlewares.auth import require_any_permission, require_permission
from app.services.rbac_service import has_permission
from app.models import Bin, Transfer, User
from app.schemas.types import Qty
from app.services import putaway_service

router = APIRouter(prefix="/warehouse", tags=["warehouse-putaway"])

_read = require_any_permission(("warehouse.bins", "R"), ("warehouse.picking", "R"), ("warehouse.counts", "R"), ("warehouse.receiving", "R"))
_move = require_permission("warehouse.bins.move", "W")
_home = require_any_permission(("warehouse.bins.manage", "W"), ("warehouse.items.manage", "W"))


def _fail(exc: putaway_service.PutAwayError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


class ItemBinOut(BaseModel):
    binId: str
    label: str
    qty: Qty
    isHome: bool
    active: bool


class ItemBinsOut(BaseModel):
    productId: str
    sku: str | None = None
    name: str
    unit: str | None = None
    homeBinId: str | None = None
    homeBinLabel: str | None = None
    total: Qty
    bins: list[ItemBinOut]


class MoveIn(BaseModel):
    productId: str
    fromBinId: str
    toBinId: str
    qty: Decimal = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)
    # Make the destination the Item's home bin too.
    makeHome: bool = False


class MoveOut(BaseModel):
    id: str
    number: str
    productId: str
    productName: str
    sku: str | None = None
    fromBinId: str
    fromBinLabel: str
    toBinId: str
    toBinLabel: str
    qty: Qty
    note: str | None = None
    movedBy: str | None = None
    at: datetime


class MoveResultOut(BaseModel):
    move: MoveOut
    warning: str | None = None


class MoveListOut(BaseModel):
    items: list[MoveOut]
    total: int


class HomeBinIn(BaseModel):
    binId: str | None = None


class WhereOut(BaseModel):
    binId: str
    label: str
    qty: Qty


class AwayRowOut(BaseModel):
    productId: str
    sku: str | None = None
    name: str
    unit: str | None = None
    homeBinId: str
    homeBinLabel: str
    homeActive: bool
    inHome: Qty
    strayQty: Qty
    elsewhere: list[WhereOut]


class HomelessRowOut(BaseModel):
    productId: str
    sku: str | None = None
    name: str
    unit: str | None = None
    total: Qty
    bins: list[WhereOut]


class PutAwayListOut(BaseModel):
    awayFromHome: list[AwayRowOut]
    noHomeBin: list[HomelessRowOut]


class PickOut(BaseModel):
    binId: str
    label: str
    qty: Qty
    isHome: bool


class PickLineOut(BaseModel):
    productId: str
    productName: str
    sku: str | None = None
    qty: Qty
    picks: list[PickOut]
    short: Qty


class PickPlanOut(BaseModel):
    transferId: str
    lines: list[PickLineOut]


async def _move_out(move) -> MoveOut:
    await move.fetch_related("product", "from_bin", "to_bin")
    return MoveOut(id=str(move.id), number=move.number, productId=move.product_id, productName=move.product.name, sku=move.product.sku,
                   fromBinId=move.from_bin_id, fromBinLabel=move.from_bin.label, toBinId=move.to_bin_id, toBinLabel=move.to_bin.label,
                   qty=move.qty, note=move.note, movedBy=move.moved_by_name, at=move.at)


@router.get("/putaway", response_model=PutAwayListOut)
async def putaway_list(user: User = Depends(_read)) -> PutAwayListOut:
    """Items with stock outside their home bin, and Items holding stock that have no home bin yet."""
    return PutAwayListOut(**await putaway_service.putaway_list())


@router.get("/items/{product_id}/bins", response_model=ItemBinsOut)
async def item_bins(product_id: str, user: User = Depends(_read)) -> ItemBinsOut:
    try:
        return ItemBinsOut(**await putaway_service.item_bins(product_id))
    except putaway_service.PutAwayError as exc:
        raise _fail(exc)


@router.put("/items/{product_id}/home-bin", response_model=ItemBinsOut)
async def set_home_bin(product_id: str, payload: HomeBinIn, user: User = Depends(_home)) -> ItemBinsOut:
    try:
        await putaway_service.set_home_bin(product_id, payload.binId)
        return ItemBinsOut(**await putaway_service.item_bins(product_id))
    except putaway_service.PutAwayError as exc:
        raise _fail(exc)


@router.post("/moves", response_model=MoveResultOut)
async def move_stock(payload: MoveIn, user: User = Depends(_move)) -> MoveResultOut:
    """Move stock from one bin to another.

    Moving stock is floor work; deciding where an Item *lives* is not. `makeHome` therefore needs the same
    access as setting a home bin directly — otherwise the tick box on the move window would be a way around
    that guard."""
    if payload.makeHome and not (await has_permission(user, "warehouse.bins.manage", "W")
                                 or await has_permission(user, "warehouse.items.manage", "W")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Moving stock is one thing; changing where an Item lives is another. Ask a Warehouse Manager.")
    try:
        move, warning = await putaway_service.move(user, payload.productId, payload.fromBinId, payload.toBinId, payload.qty, payload.note, payload.makeHome)
    except putaway_service.PutAwayError as exc:
        raise _fail(exc)
    return MoveResultOut(move=await _move_out(move), warning=warning)


@router.post("/items/{product_id}/put-home", response_model=MoveListOut)
async def put_home(product_id: str, user: User = Depends(_move)) -> MoveListOut:
    """Everything of an Item outside its home bin, moved home."""
    try:
        moves = await putaway_service.put_home(user, product_id)
    except putaway_service.PutAwayError as exc:
        raise _fail(exc)
    return MoveListOut(items=[await _move_out(m) for m in moves], total=len(moves))


@router.get("/moves", response_model=MoveListOut)
async def list_moves(productId: str | None = None, binId: str | None = None, limit: int = 50, offset: int = 0, user: User = Depends(_read)) -> MoveListOut:
    rows, total = await putaway_service.list_moves(productId, binId, min(max(limit, 1), 500), max(offset, 0))
    return MoveListOut(items=[MoveOut(**r) for r in rows], total=total)


@router.get("/transfers/{transfer_id}/pick-plan", response_model=PickPlanOut)
async def pick_plan(transfer_id: str, user: User = Depends(_read)) -> PickPlanOut:
    """Which bins each line of a transfer is picked from — the same choice dispatch makes."""
    transfer = await Transfer.get_or_none(id=transfer_id).prefetch_related("lines__product")
    if not transfer:
        raise HTTPException(404, "Transfer not found")
    held_all = await putaway_service.holdings()
    bins = {b.id: b for b in await Bin.all()}
    lines = []
    for line in transfer.lines:
        held = held_all.get(line.product_id, {})
        plan, short = await putaway_service.pick_plan(line.product_id, line.qty_sent, held, bins)
        lines.append(PickLineOut(
            productId=line.product_id, productName=line.product.name, sku=line.product.sku, qty=line.qty_sent, short=short,
            picks=[PickOut(binId=b, label=bins[b].label if b in bins else b, qty=q, isHome=b == line.product.home_bin_id) for b, q in plan],
        ))
    return PickPlanOut(transferId=str(transfer.id), lines=lines)
