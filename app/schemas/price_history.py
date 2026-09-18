"""Price history of godown Items: one Item's changes, and the Price Changes report."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.types import Money


class PriceHistoryEntryOut(BaseModel):
    id: str
    at: datetime
    productId: str
    sku: str
    name: str
    department: str | None = None
    # A Product column (price, rpp, avg_cost, disc_percent...) and what a person calls it.
    field: str
    fieldLabel: str
    # "percent" for the Item discount %, where the change is in points, not rupees.
    unit: Literal["rupees", "percent"] = "rupees"
    # Blank before on a new Item, or when a blank price was filled in; blank after when a price was cleared.
    oldValue: Money | None = None
    newValue: Money | None = None
    change: Money | None = None
    changePercent: Money | None = None
    source: str
    sourceLabel: str
    reference: str | None = None
    changedById: str | None = None
    changedBy: str | None = None


class PriceChangesReportOut(BaseModel):
    rows: list[PriceHistoryEntryOut]
    # Changes in the window before splitting older rows, for paging.
    total: int


class ChoiceOut(BaseModel):
    value: str
    label: str


class PersonOut(BaseModel):
    id: str
    name: str


class PriceChangeChoicesOut(BaseModel):
    people: list[PersonOut]
    sources: list[ChoiceOut]
    fields: list[ChoiceOut]
