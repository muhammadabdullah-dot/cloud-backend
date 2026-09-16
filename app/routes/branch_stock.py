"""Each branch's stock at head office: how much, what it's made of, and where it came from."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.middlewares.auth import require_any_permission
from app.models import User
from app.services import branch_stock_service

router = APIRouter(prefix="/branch-stock", tags=["branch-stock"])
# Executives watching stock, the Warehouse Manager deciding what to send where, and admins.
_read = require_any_permission(("executive.stock", "R"), ("warehouse.transfers.manage", "R"), ("admin.sync-endpoints", "R"))


class Origin(BaseModel):
    warehouse: str
    branches: str
    within: str


class Totals(BaseModel):
    items: int
    inStock: int
    negative: int
    units: str
    valueAtCost: str
    origin: Origin
    receivedFromWarehouse: str
    receivedFromBranches: str
    sold: str


class BranchStockLine(BaseModel):
    branchId: str
    code: str
    name: str
    snapshotAt: datetime | None = None
    lastChangeAt: datetime | None = None
    totals: Totals | None = None


class StockItem(BaseModel):
    sku: str
    name: str
    category: str | None = None
    brand: str | None = None
    department: str | None = None
    qty: str
    avgCost: str
    price: str
    valueAtCost: str
    origin: Origin
    # fromWarehouse, fromBranches, fromSuppliers, opening, found, customerReturns, sold, toBranches, toSuppliers, writtenOff
    flows: dict[str, str]
    locations: list[dict]
    lastIn: dict | None = None
    lastMovedAt: datetime | None = None
    lastSoldAt: datetime | None = None
    changedAt: datetime | None = None


class StockPage(BaseModel):
    branchId: str
    code: str
    name: str
    snapshotAt: datetime | None = None
    lastChangeAt: datetime | None = None
    items: list[StockItem]
    total: int
    totals: Totals


class Elsewhere(BaseModel):
    branchId: str
    code: str
    name: str
    qty: str
    origin: Origin
    lastMovedAt: datetime | None = None


class StockItemDetail(StockItem):
    elsewhere: list[Elsewhere]


@router.get("/overview", response_model=list[BranchStockLine])
async def overview(user: User = Depends(_read)) -> list[BranchStockLine]:
    return [BranchStockLine(**b) for b in await branch_stock_service.overview()]


@router.get("/{branch_id}", response_model=StockPage)
async def branch_items(
    branch_id: str,
    q: str | None = None,
    state: str = Query(default="in-stock", pattern="^(in-stock|out|negative|all)$"),
    origin: str | None = Query(default=None, pattern="^(warehouse|branches|within|ever-warehouse|ever-branches)$"),
    sort: str = Query(default="moved", pattern="^(moved|qty|value|sold|name)$"),
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(_read),
) -> StockPage:
    page = await branch_stock_service.items(branch_id, q=q, state=state, origin=origin, sort=sort, limit=limit, offset=offset)
    if page is None:
        raise HTTPException(404, "No such branch")
    return StockPage(**page)


@router.get("/{branch_id}/items/{sku}", response_model=StockItemDetail)
async def branch_item(branch_id: str, sku: str, user: User = Depends(_read)) -> StockItemDetail:
    found = await branch_stock_service.item(branch_id, sku)
    if found is None:
        raise HTTPException(404, "That Item isn't in this branch's stock list")
    return StockItemDetail(**found)
