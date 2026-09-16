"""The godown's Item master — the same shape as the branch's Item form."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.types import Money, Qty

Origin = Literal["local", "imported"]


class ItemAliasOut(BaseModel):
    code: str
    remarks: str | None = None
    qty: Qty = Decimal("1")
    discPercent: Decimal = Decimal("0")
    discFlat: Money = Decimal("0")


class ItemOut(BaseModel):
    id: str
    sku: str
    name: str
    price: Money
    taxRate: Decimal
    isWeighed: bool
    unit: str
    barcode: str | None = None
    packUnit: str | None = None
    packSize: int | None = None
    # Left out for people who don't see cost (see rbac_service.can_see_costs).
    avgCost: Money | None = None
    rpp: Money | None = None
    department: str | None = None
    category: str | None = None
    itemClass: str | None = None
    subclass: str | None = None
    manufacturer: str | None = None
    brand: str | None = None
    active: bool
    aliases: list[ItemAliasOut] = []
    discPercent: Decimal = Decimal("0")
    discFlat: Money = Decimal("0")
    lockDisc: bool = False
    reorderLevel: Decimal | None = None
    variant: str | None = None
    origin: str | None = None
    remarks: str | None = None
    hasPicture: bool = False
    attachmentCount: int = 0
    parentId: str | None = None
    parentQty: Qty | None = None
    homeBinId: str | None = None
    # Set only by the code lookup, when the code was an alternate barcode.
    matchedAlias: ItemAliasOut | None = None


class ItemListOut(BaseModel):
    items: list[ItemOut]
    total: int


class ItemRef(BaseModel):
    id: str
    sku: str
    name: str
    parentQty: Qty | None = None


class ItemSupplierOut(BaseModel):
    supplierId: str
    supplierCode: str
    supplierName: str
    priority: int


class LastPurchaseOut(BaseModel):
    unitPrice: Money
    discPercent: Decimal
    at: datetime
    grnNumber: str
    supplierName: str


class PriceChangeOut(BaseModel):
    oldPrice: Money
    newPrice: Money
    oldRpp: Money | None = None
    newRpp: Money | None = None
    source: str
    changedBy: str | None = None
    at: datetime


class AttachmentOut(BaseModel):
    id: str
    fileName: str
    contentType: str
    sizeBytes: int
    note: str | None = None
    uploadedBy: str | None = None
    uploadedAt: datetime


class ItemDetailOut(ItemOut):
    suppliers: list[ItemSupplierOut] = []
    parent: ItemRef | None = None
    children: list[ItemRef] = []
    lastPurchase: LastPurchaseOut | None = None
    stockOnHand: Qty = Decimal("0")
    priceHistory: list[PriceChangeOut] = []


class ItemCreate(BaseModel):
    # Blank on the form means "number it for me". Imports always carry one.
    sku: str | None = Field(default=None, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    price: Decimal = Field(ge=0)
    taxRate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    isWeighed: bool = False
    unit: str = Field(default="pc", min_length=1, max_length=30)
    barcode: str | None = Field(default=None, max_length=60)
    packUnit: str | None = Field(default=None, max_length=30)
    packSize: int | None = Field(default=None, ge=1)
    avgCost: Decimal | None = None
    rpp: Decimal | None = Field(default=None, ge=0)
    department: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    itemClass: str | None = Field(default=None, max_length=80)
    subclass: str | None = Field(default=None, max_length=80)
    manufacturer: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    active: bool = True
    discPercent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discFlat: Decimal = Field(default=Decimal("0"), ge=0)
    lockDisc: bool = False
    reorderLevel: Decimal | None = Field(default=None, ge=0)
    variant: str | None = Field(default=None, max_length=60)
    origin: Origin | None = None
    remarks: str | None = Field(default=None, max_length=255)
    parentId: str | None = None
    parentQty: Decimal | None = Field(default=None, gt=0)
    # Where the Item lives in the godown.
    homeBinId: str | None = None


class ItemUpdate(BaseModel):
    """Only fields sent change. The code is the Item's identity on every GRN, count and transfer, so it
    stays; average cost comes from receiving, never from a form."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    price: Decimal | None = Field(default=None, ge=0)
    taxRate: Decimal | None = Field(default=None, ge=0, le=100)
    isWeighed: bool | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=30)
    barcode: str | None = Field(default=None, max_length=60)
    packUnit: str | None = Field(default=None, max_length=30)
    packSize: int | None = Field(default=None, ge=1)
    rpp: Decimal | None = Field(default=None, ge=0)
    department: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    itemClass: str | None = Field(default=None, max_length=80)
    subclass: str | None = Field(default=None, max_length=80)
    manufacturer: str | None = Field(default=None, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    active: bool | None = None
    discPercent: Decimal | None = Field(default=None, ge=0, le=100)
    discFlat: Decimal | None = Field(default=None, ge=0)
    lockDisc: bool | None = None
    reorderLevel: Decimal | None = Field(default=None, ge=0)
    variant: str | None = Field(default=None, max_length=60)
    origin: Origin | None = None
    remarks: str | None = Field(default=None, max_length=255)
    parentId: str | None = None
    parentQty: Decimal | None = Field(default=None, gt=0)
    # Where the Item lives in the godown.
    homeBinId: str | None = None


class ItemAliasIn(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    remarks: str | None = Field(default=None, max_length=255)
    qty: Decimal = Field(default=Decimal("1"), gt=0)
    discPercent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discFlat: Decimal = Field(default=Decimal("0"), ge=0)


class ItemSupplierIn(BaseModel):
    supplierId: str
    priority: int = Field(default=1, ge=1, le=99)


class ItemFacetsOut(BaseModel):
    departments: list[str]
    categories: list[str]
    classes: list[str]
    subclasses: list[str]
    manufacturers: list[str]
    brands: list[str]
    units: list[str]
    packUnits: list[str]
    variants: list[str]
