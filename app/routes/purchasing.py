"""Head office purchase orders: raise, submit, approve within limits, reject, cancel, close, and what's running low."""
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
    # Added while buying and still waiting for someone to complete its details on the Item form.
    needsDetails: bool = False


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


class SuggestionLineOut(BaseModel):
    productId: str
    sku: str
    name: str
    unit: str | None = None
    packSize: int | None = None
    ratePerDay: str
    held: str
    onOrder: str
    reorderLevel: str | None = None
    branchesHold: str
    suggestedQty: str
    unitCost: str
    # The working in words: "branches sell 4 a day, godown holds 12, 30 days of cover needs 120, so order 108".
    reason: str
    # Why it's on the list: "received from them", "ordered from them", "on the Item's supplier list", "running low".
    why: str
    needsDetails: bool = False


class SuggestionsOut(BaseModel):
    supplierId: str | None = None
    supplierName: str | None = None
    coverDays: int
    salesDays: int
    # How many Items the rules found; at most 300 are sent.
    count: int
    rule: str
    lines: list[SuggestionLineOut]


class BranchHintOut(BaseModel):
    branchCode: str
    branchName: str
    qty: str
    cost: str | None = None
    price: str


class BuyItemOut(BaseModel):
    """An Item found for buying: one of the godown's own (`source` godown, with an `id`) or one only a branch carries
    (`source` branch, no `id` until it is added to the godown's Items)."""
    key: str
    source: str
    id: str | None = None
    sku: str
    name: str
    unit: str | None = None
    department: str | None = None
    category: str | None = None
    brand: str | None = None
    price: str
    cost: str | None = None
    taxRate: str | None = None
    packSize: int | None = None
    active: bool = True
    needsDetails: bool = False
    detailsNote: str | None = None
    godownQty: str | None = None
    branches: list[BranchHintOut] = []
    # Set when this request added it to the godown's Items.
    added: bool = False


class FromBranchesIn(BaseModel):
    skus: list[str] = Field(min_length=1, max_length=200)


class ByHandIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit: str | None = Field(default=None, max_length=30)
    # What the supplier charges for one.
    cost: Decimal = Field(ge=0)
    # What it sells for, when known.
    price: Decimal | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=60)
    # Where it is being written in, for the note on the Item: "order" or "grn".
    where: str | None = Field(default=None, max_length=20)


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
                needsDetails=bool(products[l.product_id].needs_details) if l.product_id in products else False,
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


@router.get("/purchase-orders/suggestions", response_model=SuggestionsOut)
async def suggestions(supplierId: str | None = None, coverDays: int = 30, excludeOrderId: str | None = None, user: User = Depends(_write)) -> SuggestionsOut:
    """With `supplierId`: the Items bought from that supplier before, each with a suggested quantity and the working in
    words. Without it: the godown Items running low. `excludeOrderId` leaves the order being changed out of "on order"."""
    from app.services.rbac_service import can_see_costs

    try:
        found = await purchasing_service.suggestions(supplierId, coverDays, excludeOrderId)
    except purchasing_service.PurchasingError as exc:
        raise _fail(exc) from exc
    costs = await can_see_costs(user)
    return SuggestionsOut(
        supplierId=found["supplierId"], supplierName=found["supplierName"], coverDays=found["coverDays"], salesDays=found["salesDays"],
        count=found["count"], rule=found["rule"],
        lines=[
            SuggestionLineOut(
                productId=l["productId"], sku=l["sku"], name=l["name"], unit=l["unit"], packSize=l["packSize"],
                ratePerDay=_q(Decimal(l["ratePerDay"]).quantize(Decimal("0.001"))), held=_q(l["held"]), onOrder=_q(l["onOrder"]),
                reorderLevel=_q(l["reorderLevel"]) if l["reorderLevel"] is not None else None, branchesHold=_q(l["branchesHold"]),
                suggestedQty=_q(l["suggestedQty"]), unitCost=purchasing_service.money(l["unitCost"]) if costs else "0.00",
                reason=l["reason"], why=l["why"], needsDetails=l["needsDetails"],
            )
            for l in found["lines"]
        ],
    )


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


# ── finding Items to buy: the godown's own and every Item a branch carries ──────────────────────

_find = require_any_permission(
    ("warehouse.purchase-orders", "R"), ("warehouse.receiving", "R"), ("warehouse.items", "R"), ("executive.dashboard", "R"),
)
_add_items = require_any_permission(("warehouse.purchase-orders", "W"), ("warehouse.receiving", "W"), ("warehouse.items.manage", "W"))


def _buy_item(d: dict, costs: bool) -> BuyItemOut:
    return BuyItemOut(
        key=d["key"], source=d["source"], id=d["id"], sku=d["sku"], name=d["name"], unit=d["unit"], department=d["department"],
        category=d["category"], brand=d["brand"], price=purchasing_service.money(d["price"]) or "0.00",
        cost=purchasing_service.money(d["cost"]) if costs and d["cost"] is not None else None,
        taxRate=_q(Decimal(d["taxRate"])) if d["taxRate"] is not None else None, packSize=d["packSize"], active=d["active"],
        needsDetails=d["needsDetails"], detailsNote=d.get("detailsNote"),
        godownQty=_q(d["godownQty"]) if d["godownQty"] is not None else None,
        branches=[
            BranchHintOut(branchCode=b["branchCode"], branchName=b["branchName"], qty=_q(b["qty"]),
                          cost=purchasing_service.money(b["cost"]) if costs else None, price=purchasing_service.money(b["price"]) or "0.00")
            for b in d["branches"]
        ],
        added=d.get("added", False),
    )


def _product_as_buy_item(p, added: bool) -> dict:
    return {
        "key": f"g:{p.id}", "source": "godown", "id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "department": p.department,
        "category": p.category, "brand": p.brand, "price": p.price, "cost": p.avg_cost, "taxRate": p.tax_rate, "packSize": p.pack_size,
        "active": p.active, "needsDetails": p.needs_details, "detailsNote": p.details_note, "godownQty": None, "branches": [], "added": added,
    }


@router.get("/buying/items", response_model=list[BuyItemOut])
async def find_items(q: str, limit: int = 30, user: User = Depends(_find)) -> list[BuyItemOut]:
    """Every Item the company carries matching all the words typed (name, code or barcode): the godown's own Items first,
    then Items only a branch carries, from each branch's latest complete stock list, with its quantity, cost and price."""
    from app.services import items_service
    from app.services.rbac_service import can_see_costs

    costs = await can_see_costs(user)
    return [_buy_item(d, costs) for d in await items_service.company_search(q, limit)]


@router.post("/buying/items/from-branches", response_model=list[BuyItemOut])
async def add_from_branches(payload: FromBranchesIn, user: User = Depends(_add_items)) -> list[BuyItemOut]:
    """Adds Items only a branch carries to the godown's Items, from the branch's figures, so they can be ordered, received
    and stocked. Each is marked "details to complete" (the stock list doesn't carry the unit or GST)."""
    from app.services import items_service
    from app.services.rbac_service import can_see_costs

    try:
        made = await items_service.add_from_branches(payload.skus, user)
    except items_service.ItemError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    costs = await can_see_costs(user)
    return [_buy_item(_product_as_buy_item(p, added), costs) for p, added in made]


@router.post("/buying/items/by-hand", response_model=BuyItemOut)
async def add_by_hand(payload: ByHandIn, user: User = Depends(_add_items)) -> BuyItemOut:
    """An Item nobody carries yet, written in while buying. It can be ordered and received at once, and shows on the
    Items screen as "details to complete" until someone saves its Item form."""
    from app.services import items_service
    from app.services.rbac_service import can_see_costs

    where = "a GRN" if (payload.where or "").strip().lower() == "grn" else "a purchase order"
    try:
        p = await items_service.add_by_hand(payload.name, payload.unit, payload.cost, payload.price, payload.sku, user, where)
    except items_service.ItemError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return _buy_item(_product_as_buy_item(p, True), await can_see_costs(user))
