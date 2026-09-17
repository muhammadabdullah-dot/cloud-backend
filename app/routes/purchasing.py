"""Head office purchase orders: raise, submit, approve within limits, reject, cancel, close — and what's running low."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.middlewares.auth import require_any_permission, require_permission
from app.models import PurchaseOrder, User
from app.services import purchase_report_service, purchasing_service

router = APIRouter(prefix="/warehouse", tags=["purchase-orders"])
_read = require_any_permission(("warehouse.purchase-orders", "R"), ("warehouse.receiving", "R"), ("executive.dashboard", "R"))
_write = require_permission("warehouse.purchase-orders", "W")
_approve = require_permission("warehouse.purchase-orders.approve", "X")


class OrderLineIn(BaseModel):
    productId: str
    qty: Decimal = Field(gt=0)
    unitCost: Decimal = Field(ge=0)


class OrderIn(BaseModel):
    supplierId: str
    lines: list[OrderLineIn] = Field(min_length=1)
    expectedAt: datetime | None = None
    notes: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=120)


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=255)


class OrderLineOut(BaseModel):
    productId: str
    productName: str | None = None
    productSku: str | None = None
    unit: str | None = None
    qty: str
    unitCost: str
    receivedQty: str


class OrderOut(BaseModel):
    id: str
    number: str
    supplierId: str
    supplierName: str
    status: str
    reason: str | None = None
    expectedAt: datetime | None = None
    notes: str | None = None
    total: str
    raisedBy: str
    raisedById: str
    raisedAt: datetime
    submittedAt: datetime | None = None
    approvedBy: str | None = None
    approvedAt: datetime | None = None
    autoApproved: bool
    decisionNote: str | None = None
    closedAt: datetime | None = None
    lines: list[OrderLineOut]


class OrderListOut(BaseModel):
    items: list[OrderOut]
    total: int


class LowStockOut(BaseModel):
    productId: str
    sku: str
    name: str
    onHand: str
    reorderLevel: str
    avgCost: str


class MyLimitOut(BaseModel):
    # None = no limit.
    limit: str | None = None
    canApprove: bool


def _q(v: Decimal) -> str:
    text = format(v.normalize(), "f")
    return "0" if text in ("-0", "") else text


async def _out(po: PurchaseOrder) -> OrderOut:
    from app.models import Product

    products = {p.id: p for p in await Product.filter(id__in=[l.product_id for l in po.lines])}
    return OrderOut(
        id=str(po.id), number=po.po_number, supplierId=str(po.supplier_id), supplierName=po.supplier.name, status=po.status,
        reason=po.reason, expectedAt=po.expected_at, notes=po.notes, total=purchasing_service.money(po.total), raisedBy=po.raised_by.name,
        raisedById=str(po.raised_by_id), raisedAt=po.raised_at, submittedAt=po.submitted_at,
        approvedBy=po.approved_by.name if po.approved_by else None, approvedAt=po.approved_at, autoApproved=po.auto_approved,
        decisionNote=po.decision_note, closedAt=po.closed_at,
        lines=[
            OrderLineOut(
                productId=str(l.product_id), productName=products[l.product_id].name if l.product_id in products else None,
                productSku=products[l.product_id].sku if l.product_id in products else None,
                unit=products[l.product_id].unit if l.product_id in products else None,
                qty=_q(l.qty), unitCost=purchasing_service.money(l.unit_cost), receivedQty=_q(l.received_qty),
            )
            for l in sorted(po.lines, key=lambda x: x.position)
        ],
    )


def _fail(exc: purchasing_service.PurchasingError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/purchase-orders", response_model=OrderListOut)
async def list_orders(status: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0, user: User = Depends(_read)) -> OrderListOut:
    rows, total = await purchasing_service.list_orders(status, q, limit, offset)
    return OrderListOut(items=[await _out(po) for po in rows], total=total)


@router.get("/purchase-orders/my-limit", response_model=MyLimitOut)
async def my_limit(user: User = Depends(_read)) -> MyLimitOut:
    from app.services.rbac_service import has_permission

    await purchasing_service.use_saved_limits()
    limit = purchasing_service.po_limit_of(user)
    return MyLimitOut(limit=purchasing_service.money(limit), canApprove=await has_permission(user, "warehouse.purchase-orders.approve", "X"))


@router.get("/low-stock", response_model=list[LowStockOut])
async def low_stock(user: User = Depends(_read)) -> list[LowStockOut]:
    return [
        LowStockOut(productId=r["productId"], sku=r["sku"], name=r["name"], onHand=_q(r["onHand"]), reorderLevel=_q(r["reorderLevel"]), avgCost=purchasing_service.money(r["avgCost"]))
        for r in await purchasing_service.low_stock()
    ]


@router.get("/purchase-orders/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, user: User = Depends(_read)) -> OrderOut:
    try:
        return await _out(await purchasing_service.get_order(order_id))
    except purchasing_service.PurchasingError as exc:
        raise _fail(exc) from exc


@router.post("/purchase-orders", response_model=OrderOut)
async def create_order(payload: OrderIn, user: User = Depends(_write)) -> OrderOut:
    try:
        po = await purchasing_service.create_order(
            user, payload.supplierId, [(l.productId, l.qty, l.unitCost) for l in payload.lines], payload.expectedAt, payload.notes, payload.reason,
        )
    except purchasing_service.PurchasingError as exc:
        raise _fail(exc) from exc
    return await _out(po)


@router.put("/purchase-orders/{order_id}", response_model=OrderOut)
async def update_order(order_id: str, payload: OrderIn, user: User = Depends(_write)) -> OrderOut:
    try:
        po = await purchasing_service.update_order(
            user, order_id, payload.supplierId, [(l.productId, l.qty, l.unitCost) for l in payload.lines], payload.expectedAt, payload.notes, payload.reason,
        )
    except purchasing_service.PurchasingError as exc:
        raise _fail(exc) from exc
    return await _out(po)


async def _step(fn, *args) -> OrderOut:
    try:
        return await _out(await fn(*args))
    except purchasing_service.PurchasingError as exc:
        raise _fail(exc) from exc


@router.post("/purchase-orders/{order_id}/submit", response_model=OrderOut)
async def submit_order(order_id: str, user: User = Depends(_write)) -> OrderOut:
    """Within your own approval limit it's approved at once (the Executive is told); above it, it waits."""
    return await _step(purchasing_service.submit_order, user, order_id)


@router.post("/purchase-orders/{order_id}/approve", response_model=OrderOut)
async def approve_order(order_id: str, user: User = Depends(_approve)) -> OrderOut:
    return await _step(purchasing_service.approve_order, user, order_id)


@router.post("/purchase-orders/{order_id}/reject", response_model=OrderOut)
async def reject_order(order_id: str, payload: NoteIn, user: User = Depends(_approve)) -> OrderOut:
    return await _step(purchasing_service.reject_order, user, order_id, payload.note)


@router.post("/purchase-orders/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: str, payload: NoteIn, user: User = Depends(_write)) -> OrderOut:
    return await _step(purchasing_service.cancel_order, user, order_id, payload.note)


@router.post("/purchase-orders/{order_id}/close", response_model=OrderOut)
async def close_order(order_id: str, payload: NoteIn, user: User = Depends(_write)) -> OrderOut:
    return await _step(purchasing_service.close_order, user, order_id, payload.note)


@router.get("/purchase-summary")
async def purchase_summary(
    groupBy: str = "brand", view: str = "summary", from_: datetime | None = Query(None, alias="from"), to: datetime | None = None,
    approved: str = Query("all", pattern="^(all|yes|no)$"), supplierId: str | None = None, user: User = Depends(_read),
) -> dict:
    """The legacy Purchase Summary Group Wise for the godown. Same layout as the branch report."""
    return await purchase_report_service.summary(groupBy, view, from_, to, approved, supplierId)
