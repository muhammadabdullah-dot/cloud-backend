from datetime import datetime

from pydantic import BaseModel, Field


class ClaimIn(BaseModel):
    """What a branch server presents to be verified. No bearer token — this call happens before
    the branch has any credential at all; the pairing key *is* the authentication."""

    code: str
    pairingKey: str
    # Where this branch can be reached back on, and what machine is claiming. Both optional and
    # both purely informational — a branch behind a home router has no reachable URL to give, and
    # refusing to verify it for that reason would be refusing the common case.
    syncUrl: str | None = None
    claimedFrom: str | None = Field(default=None, max_length=120)


class ClaimOut(BaseModel):
    """Everything the branch needs to become itself — and the secret it will present from now on.
    This is the only response that ever carries `syncSecret`."""

    branchId: str
    code: str
    name: str
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    timezone: str
    verifiedAt: datetime
    syncSecret: str


class PairingOut(BaseModel):
    """A freshly-minted key, shown once in the admin UI. `pairingKey` is present only on the
    response that mints it and on a branch that has not been claimed yet — never in a list."""

    branchId: str
    code: str
    name: str
    pairingKey: str
    expiresAt: datetime | None = None


class SyncEventIn(BaseModel):
    """One branch outbox event, in the branch's own shape. Cloud stores it verbatim."""

    id: str
    aggregateType: str
    aggregateId: str
    payload: dict
    originUserId: str | None = None
    originDeviceId: str | None = None
    createdAt: datetime | None = None


class SyncPushIn(BaseModel):
    events: list[SyncEventIn] = Field(default_factory=list)


class SyncPushOut(BaseModel):
    """What the branch needs to decide what to do next: how many we took, how many we already had,
    and the ids it may now mark as sent. Returning the ids rather than assuming "all of them"
    means a partially-accepted batch can't be mistaken for a fully-accepted one."""

    received: int
    accepted: int
    duplicates: int
    acknowledgedIds: list[str]
    serverTime: datetime


class SnapshotIn(BaseModel):
    """A branch's whole aggregate picture of itself.

    Rows are `dict`, not per-row models, deliberately: this carries hundreds of product-days and
    validating each one field-by-field would cost more than it protects. The apply layer coerces
    every value and tolerates missing keys, so a malformed row lands as zeroes rather than as a
    500 that rejects an entire branch's day."""

    daily: list[dict] = Field(default_factory=list)
    cashiers: list[dict] = Field(default_factory=list)
    products: list[dict] = Field(default_factory=list)
    hourly: list[dict] = Field(default_factory=list)
    tillCloses: list[dict] = Field(default_factory=list)
    tenders: list[dict] = Field(default_factory=list)
    overrides: list[dict] = Field(default_factory=list)
    returns: list[dict] = Field(default_factory=list)
    creditCustomers: list[dict] = Field(default_factory=list)
    alerts: list[dict] = Field(default_factory=list)
    stockValue: str | None = None


class SnapshotOut(BaseModel):
    tradingDays: int
    productDays: int
    cashierDays: int
    alerts: int
    stockValue: str
    serverTime: datetime


class StockChunkIn(BaseModel):
    """One slice of the item-level stock list. `snapshotId` ties the slices of one push together so
    a half-delivered picture never becomes visible."""

    snapshotId: str = Field(min_length=1, max_length=60)
    rows: list[dict] = Field(default_factory=list)


class StockChunkOut(BaseModel):
    snapshotId: str
    received: int


class StockCompleteIn(BaseModel):
    snapshotId: str = Field(min_length=1, max_length=60)


class StockCompleteOut(BaseModel):
    snapshotId: str
    stockRows: int
    serverTime: datetime


class SyncHelloOut(BaseModel):
    """A cheap "are we still who we think we are" — the branch calls this to confirm the Cloud is
    reachable and its credential still works, without pushing anything."""

    branchId: str
    code: str
    name: str
    status: str
    serverTime: datetime
