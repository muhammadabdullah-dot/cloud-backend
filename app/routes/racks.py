"""Racks & Bins — the godown's floor plan: the layout with how full each bin is, adding and sizing racks,
changing and switching off bins, and what a bin holds."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.schemas.types import Qty
from app.services import racks_service

router = APIRouter(prefix="/warehouse", tags=["warehouse-racks"])

_read = require_any_permission(("warehouse.bins", "R"), ("warehouse.picking", "R"), ("warehouse.counts", "R"))
_manage = require_permission("warehouse.bins.manage", "W")


class LayoutBinOut(BaseModel):
    id: str
    code: str
    label: str
    level: int | None = None
    position: int | None = None
    capacityUnits: int
    priority: int
    active: bool
    usedUnits: Qty
    itemCount: int
    # How much of the searched Item this bin holds, when an Item was searched.
    itemQty: Qty | None = None
    # How many Items call this bin home, whether or not stock is there yet.
    homeCount: int = 0
    # The searched Item lives here.
    isHome: bool = False


class LayoutRackOut(BaseModel):
    id: str
    code: str
    name: str | None = None
    zone: str | None = None
    levels: int
    positions: int
    active: bool
    bins: list[LayoutBinOut]


class LayoutOut(BaseModel):
    racks: list[LayoutRackOut]
    zones: list[str]


class RackCreate(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    name: str | None = Field(default=None, max_length=80)
    zone: str | None = Field(default=None, max_length=60)
    levels: int = Field(ge=1, le=racks_service.MAX_LEVELS)
    positions: int = Field(ge=1, le=racks_service.MAX_POSITIONS)
    capacityUnits: int = Field(default=0, ge=0)
    priority: int = Field(default=1, ge=1, le=99)


class RackUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    zone: str | None = Field(default=None, max_length=60)
    levels: int | None = Field(default=None, ge=1, le=racks_service.MAX_LEVELS)
    positions: int | None = Field(default=None, ge=1, le=racks_service.MAX_POSITIONS)
    # Capacity for bins added by growing the rack; existing bins keep theirs.
    capacityUnits: int | None = Field(default=None, ge=0)
    active: bool | None = None


class RackSavedOut(BaseModel):
    rack: LayoutRackOut
    binsAdded: int


class BinUpdate(BaseModel):
    capacityUnits: int | None = Field(default=None, ge=0)
    priority: int | None = Field(default=None, ge=1, le=99)
    active: bool | None = None


class BinContentOut(BaseModel):
    productId: str
    name: str
    sku: str | None = None
    unit: str | None = None
    qty: Qty
    isHome: bool = False
    homeBinId: str | None = None
    homeBinLabel: str | None = None


class BinResidentOut(BaseModel):
    productId: str
    name: str
    sku: str | None = None
    unit: str | None = None
    category: str | None = None
    brand: str | None = None
    # Stock of it in this bin right now; zero until it's received.
    qty: Qty


class BinContentsOut(BaseModel):
    bin: LayoutBinOut
    items: list[BinContentOut]
    homeItems: list[BinResidentOut]


class BinSuggestionOut(BaseModel):
    binId: str
    label: str
    rack: str
    level: int | None = None
    position: int | None = None
    reason: str


def _fail(exc: racks_service.RackError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _bin_out(b, per_bin: dict, product_qty: dict | None = None, home_bin_id: str | None = None) -> LayoutBinOut:
    occ = per_bin.get(b.id, {"used": Decimal("0"), "items": 0})
    return LayoutBinOut(
        id=b.id, code=b.bin, label=b.label, level=b.level, position=b.position, capacityUnits=b.capacity_units,
        priority=b.priority, active=b.active, usedUnits=occ["used"], itemCount=occ["items"],
        itemQty=(product_qty or {}).get(b.id) if product_qty is not None else None,
        homeCount=occ.get("homes", 0), isHome=bool(home_bin_id and home_bin_id == b.id),
    )


async def _rack_out(rack, product_id: str | None = None) -> LayoutRackOut:
    racks, bins, per_bin, product_qty = await racks_service.layout(product_id)
    return LayoutRackOut(
        id=str(rack.id), code=rack.code, name=rack.name, zone=rack.zone, levels=rack.levels, positions=rack.positions,
        active=rack.active, bins=[_bin_out(b, per_bin) for b in bins.get(rack.code, [])],
    )


@router.get("/racks/layout", response_model=LayoutOut)
async def layout(productId: str | None = None, user: User = Depends(_read)) -> LayoutOut:
    """Every rack with its bins and how full each is. With `productId`, each bin also says how much of that Item it holds."""
    racks, bins, per_bin, product_qty = await racks_service.layout(productId)
    home_bin_id = None
    if productId:
        from app.models import Product

        home_bin_id = (await Product.filter(id=productId).values_list("home_bin_id", flat=True) or [None])[0]
    return LayoutOut(
        racks=[
            LayoutRackOut(
                id=str(r.id), code=r.code, name=r.name, zone=r.zone, levels=r.levels, positions=r.positions, active=r.active,
                bins=[_bin_out(b, per_bin, product_qty if productId else None, home_bin_id) for b in bins.get(r.code, [])],
            )
            for r in racks
        ],
        zones=await racks_service.zones(),
    )


@router.post("/racks", response_model=RackSavedOut)
async def create_rack(payload: RackCreate, user: User = Depends(_manage)) -> RackSavedOut:
    """Add a rack `levels` high and `positions` wide; its bins are created with it."""
    try:
        rack, made = await racks_service.create_rack(payload.model_dump())
    except racks_service.RackError as exc:
        raise _fail(exc)
    return RackSavedOut(rack=await _rack_out(rack), binsAdded=made)


@router.patch("/racks/{rack_id}", response_model=RackSavedOut)
async def update_rack(rack_id: str, payload: RackUpdate, user: User = Depends(_manage)) -> RackSavedOut:
    """Rename, re-zone, grow (never shrink), or switch a whole rack off or on."""
    try:
        rack, made = await racks_service.update_rack(rack_id, payload.model_dump(exclude_unset=True))
    except racks_service.RackError as exc:
        raise _fail(exc)
    return RackSavedOut(rack=await _rack_out(rack), binsAdded=made)


@router.patch("/bins/{bin_id}", response_model=LayoutBinOut)
async def update_bin(bin_id: str, payload: BinUpdate, user: User = Depends(_manage)) -> LayoutBinOut:
    try:
        found = await racks_service.update_bin(bin_id, payload.model_dump(exclude_unset=True))
    except racks_service.RackError as exc:
        raise _fail(exc)
    _, _, per_bin, _ = await racks_service.layout()
    return _bin_out(found, per_bin)


@router.get("/bins/{bin_id}/contents", response_model=BinContentsOut)
async def bin_contents(bin_id: str, user: User = Depends(_read)) -> BinContentsOut:
    """What the bin holds, most first, and the Items that live in it."""
    try:
        found, items, homes = await racks_service.contents(bin_id)
    except racks_service.RackError as exc:
        raise _fail(exc)
    _, _, per_bin, _ = await racks_service.layout()
    return BinContentsOut(bin=_bin_out(found, per_bin), items=[BinContentOut(**i) for i in items], homeItems=[BinResidentOut(**h) for h in homes])


@router.get("/bins/suggest", response_model=BinSuggestionOut | None)
async def suggest_bin(productId: str | None = None, category: str | None = None, brand: str | None = None, user: User = Depends(_read)) -> BinSuggestionOut | None:
    """A free bin for an Item, next to Items like it."""
    found = await racks_service.suggest_bin(productId, category, brand)
    return BinSuggestionOut(**found) if found else None
