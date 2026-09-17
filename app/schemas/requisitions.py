from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.types import Qty


class BranchRequestLineOut(BaseModel):
    productId: str
    productName: str | None = None
    productSku: str | None = None
    unit: str | None = None
    qtyRequested: Qty
    qtyApproved: Qty | None = None
    # What the asking branch held and sold a day when it sent the request.
    branchOnHand: Qty | None = None
    branchDailySales: Qty | None = None
    godownOnHand: Qty
    # What the branch asked to send it last reported holding (pending requests from another branch only).
    sourceOnHand: Qty | None = None


class BranchRequestOut(BaseModel):
    id: str
    requisitionNumber: str
    branchId: str
    branchName: str
    branchCode: str
    branchHasServer: bool
    # pending · approved · rejected (declined) · cancelled (withdrawn by the branch)
    status: str
    origin: str
    branchNumber: str | None = None
    sourceBranchId: str | None = None
    sourceBranchName: str | None = None
    sourceBranchCode: str | None = None
    reason: str | None = None
    neededBy: date | None = None
    requestedBy: str | None = None
    requestedAt: datetime
    decidedAt: datetime | None = None
    decidedBy: str | None = None
    decisionNote: str | None = None
    transferId: str | None = None
    transferNumber: str | None = None
    transferStatus: str | None = None
    lines: list[BranchRequestLineOut]


class BranchRequestListOut(BaseModel):
    items: list[BranchRequestOut]
    total: int


class ApproveLineIn(BaseModel):
    productId: str
    qty: Decimal = Field(ge=0)


class ApproveIn(BaseModel):
    # Left out: every Item as asked. 0 leaves an Item out.
    lines: list[ApproveLineIn] | None = None
    note: str | None = Field(default=None, max_length=200)


class DeclineIn(BaseModel):
    reason: str = Field(max_length=255)


class RecordLineIn(BaseModel):
    productId: str
    qty: Decimal = Field(gt=0)


class RecordIn(BaseModel):
    branchId: str
    # Empty: the central godown sends it.
    sourceBranchId: str | None = None
    reason: str = Field(max_length=255)
    neededBy: date | None = None
    lines: list[RecordLineIn] = Field(min_length=1, max_length=300)
