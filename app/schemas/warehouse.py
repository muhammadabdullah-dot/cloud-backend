"""Warehouse wire shapes.

Every row that refers to a product carries that product's name and sku, resolved server-side.
This is deliberate and was learned the hard way on the Branch Server: leaving the client to turn
a productId into a name means it can only name the products it happens to have cached, so tables
render bare codes and searches miss. A row that refers to something carries that thing's name.
The same applies to branch and bin references.
"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.types import Money, Qty


class ProductOut(BaseModel):
    id: str
    sku: str
    name: str
    price: Money
    taxRate: Decimal
    isWeighed: bool
    unit: str
    packUnit: str | None = None
    packSize: int | None = None


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int


class SupplierOut(BaseModel):
    id: str
    code: str
    name: str
    contactPerson: str | None = None
    phone: str | None = None


class BinOut(BaseModel):
    id: str
    rack: str
    bin: str
    label: str
    priority: int
    capacityUnits: int


class BalanceOut(BaseModel):
    productId: str
    productName: str | None = None
    productSku: str | None = None
    binId: str | None = None
    binLabel: str | None = None
    balance: Qty


class BalanceListOut(BaseModel):
    items: list[BalanceOut]
    total: int


class StockMovementOut(BaseModel):
    id: str
    productId: str
    productName: str | None = None
    productSku: str | None = None
    binId: str
    binLabel: str | None = None
    kind: str
    qty: Qty
    reason: str | None = None
    originUserId: str | None = None
    originUserName: str | None = None
    at: datetime


class StockMovementListOut(BaseModel):
    items: list[StockMovementOut]
    total: int


class BatchOut(BaseModel):
    id: str
    productId: str
    productName: str | None = None
    productSku: str | None = None
    lotNumber: str | None = None
    expiry: datetime | None = None
    receivedQty: Qty


class BatchListOut(BaseModel):
    items: list[BatchOut]
    total: int


# ── receiving ───────────────────────────────────────────────────────────────
class GRNLineIn(BaseModel):
    productId: str
    qty: Decimal
    bonusQty: Decimal = Decimal("0")
    unitPrice: Decimal
    discPercent: Decimal = Decimal("0")
    expiry: datetime | None = None
    taxRate: Decimal = Decimal("0")


class GRNCreateRequest(BaseModel):
    supplierId: str
    partyInvNo: str | None = None
    binId: str
    gstMode: str = "normal"
    advanceTax: Decimal = Decimal("0")
    approved: bool = False
    lines: list[GRNLineIn]


class GRNLineOut(BaseModel):
    productId: str
    productName: str | None = None
    productSku: str | None = None
    qty: Qty
    bonusQty: Qty
    unitPrice: Money
    discPercent: Decimal
    expiry: datetime | None = None
    taxRate: Decimal


class GRNOut(BaseModel):
    id: str
    grnNumber: str
    supplierId: str
    supplierName: str | None = None
    partyInvNo: str | None = None
    binId: str
    binLabel: str | None = None
    gstMode: str
    advanceTax: Money
    approved: bool
    receivedByUserId: str | None = None
    receivedByName: str | None = None
    at: datetime
    lines: list[GRNLineOut]


class GRNListOut(BaseModel):
    items: list[GRNOut]
    total: int


# ── requisitions ────────────────────────────────────────────────────────────
class RequisitionCreateRequest(BaseModel):
    branchId: str
    productId: str
    qtyRequested: Decimal


class RequisitionOut(BaseModel):
    id: str
    requisitionNumber: str
    branchId: str
    branchName: str | None = None
    branchCode: str | None = None
    productId: str
    productName: str | None = None
    productSku: str | None = None
    qtyRequested: Qty
    status: str
    requestedAt: datetime
    decidedByUserId: str | None = None
    decidedByName: str | None = None
    decidedAt: datetime | None = None


class RequisitionListOut(BaseModel):
    items: list[RequisitionOut]
    total: int


# ── transfers ───────────────────────────────────────────────────────────────
class TransferLineOut(BaseModel):
    productId: str
    productName: str | None = None
    productSku: str | None = None
    qtySent: Qty
    qtyReceived: Qty | None = None


class TransferOut(BaseModel):
    id: str
    transferNumber: str
    branchId: str
    branchName: str | None = None
    branchCode: str | None = None
    requisitionId: str | None = None
    requisitionNumber: str | None = None
    status: str
    vehicle: str | None = None
    driver: str | None = None
    requestedAt: datetime
    approvedAt: datetime | None = None
    dispatchedAt: datetime | None = None
    receivedAt: datetime | None = None
    disputeOpen: bool
    disputeNote: str | None = None
    lines: list[TransferLineOut]


class TransferListOut(BaseModel):
    items: list[TransferOut]
    total: int


class DispatchRequest(BaseModel):
    vehicle: str
    driver: str


class ReceiveLineIn(BaseModel):
    productId: str
    qtyReceived: Decimal


class ReceiveTransferRequest(BaseModel):
    lines: list[ReceiveLineIn]
    note: str | None = None


class ResolveDisputeRequest(BaseModel):
    note: str | None = None


# ── cycle counts ────────────────────────────────────────────────────────────
class CountSubmitRequest(BaseModel):
    productId: str
    binId: str
    countedQty: Decimal


class CountOut(BaseModel):
    id: str
    productId: str
    productName: str | None = None
    productSku: str | None = None
    binId: str
    binLabel: str | None = None
    systemQty: Qty
    countedQty: Qty
    status: str
    countedByUserId: str | None = None
    countedByName: str | None = None
    approvedByUserId: str | None = None
    at: datetime


class CountListOut(BaseModel):
    items: list[CountOut]
    total: int
