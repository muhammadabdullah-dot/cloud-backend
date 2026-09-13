"""Warehouse controller.

`_Names` is the shape of this file: one bulk lookup per response turns every product, bin, branch
and user id into a readable label. It exists because the alternative — letting the client resolve
ids against whatever it cached — produces tables full of bare codes as soon as the catalog is
bigger than one page. Resolve once here, and every screen reads properly for free.
"""
from dataclasses import dataclass, field

from fastapi import HTTPException, status

from app.models import (
    GRN,
    Batch,
    Bin,
    Branch,
    CycleCount,
    Product,
    Requisition,
    StockMovement,
    Supplier,
    Transfer,
    User,
)
from app.schemas.warehouse import (
    BalanceListOut,
    BalanceOut,
    BatchListOut,
    BatchOut,
    BinOut,
    CountListOut,
    CountOut,
    CountSubmitRequest,
    DispatchRequest,
    GRNCreateRequest,
    GRNLineOut,
    GRNListOut,
    GRNOut,
    ProductListOut,
    ProductOut,
    ReceiveTransferRequest,
    RequisitionCreateRequest,
    RequisitionListOut,
    RequisitionOut,
    StockMovementListOut,
    StockMovementOut,
    SupplierOut,
    TransferLineOut,
    TransferListOut,
    TransferOut,
)
from app.services import warehouse_service

# Chunked well under SQLite's parameter cap so a full page of movements still resolves in a
# handful of queries rather than blowing the IN(...) limit.
_CHUNK = 400


@dataclass
class _Names:
    products: dict[str, Product] = field(default_factory=dict)
    bins: dict[str, Bin] = field(default_factory=dict)
    branches: dict[str, Branch] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    suppliers: dict[str, str] = field(default_factory=dict)

    def product(self, pid: str) -> tuple[str | None, str | None]:
        p = self.products.get(pid)
        return (p.name, p.sku) if p else (None, None)

    def bin_label(self, bid: str | None) -> str | None:
        b = self.bins.get(bid) if bid else None
        return b.label if b else None

    def branch(self, bid: str | None) -> tuple[str | None, str | None]:
        b = self.branches.get(bid) if bid else None
        return (b.name, b.code) if b else (None, None)

    def user(self, uid: str | None) -> str | None:
        return self.users.get(uid) if uid else None


async def _resolve(
    *, product_ids: set[str] = frozenset(), bin_ids: set[str] = frozenset(),
    branch_ids: set[str] = frozenset(), user_ids: set[str] = frozenset(),
    supplier_ids: set[str] = frozenset(),
) -> _Names:
    names = _Names()
    pids = [i for i in product_ids if i]
    for start in range(0, len(pids), _CHUNK):
        for p in await Product.filter(id__in=pids[start:start + _CHUNK]):
            names.products[str(p.id)] = p
    if bin_ids:
        for b in await Bin.filter(id__in=[i for i in bin_ids if i]):
            names.bins[str(b.id)] = b
    if branch_ids:
        for b in await Branch.filter(id__in=[i for i in branch_ids if i]):
            names.branches[str(b.id)] = b
    if user_ids:
        for u in await User.filter(id__in=[i for i in user_ids if i]):
            names.users[str(u.id)] = u.name
    if supplier_ids:
        for s in await Supplier.filter(id__in=[i for i in supplier_ids if i]):
            names.suppliers[str(s.id)] = s.name
    return names


def _fail(exc: warehouse_service.WarehouseError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)


# ── masters ─────────────────────────────────────────────────────────────────
def _product_out(p: Product) -> ProductOut:
    return ProductOut(
        id=str(p.id), sku=p.sku, name=p.name, price=p.price, taxRate=p.tax_rate,
        isWeighed=p.is_weighed, unit=p.unit, packUnit=p.pack_unit, packSize=p.pack_size,
    )


async def list_products(q: str | None, limit: int, offset: int) -> ProductListOut:
    limit = min(max(limit, 1), 200)
    items, total = await warehouse_service.list_products(q, limit, max(offset, 0))
    return ProductListOut(items=[_product_out(p) for p in items], total=total)


async def list_suppliers() -> list[SupplierOut]:
    return [
        SupplierOut(id=str(s.id), code=s.code, name=s.name, contactPerson=s.contact_person, phone=s.phone)
        for s in await warehouse_service.list_suppliers()
    ]


async def list_bins() -> list[BinOut]:
    return [
        BinOut(id=str(b.id), rack=b.rack, bin=b.bin, label=b.label, priority=b.priority, capacityUnits=b.capacity_units)
        for b in await warehouse_service.list_bins()
    ]


# ── balances ────────────────────────────────────────────────────────────────
async def get_balance(product_id: str, bin_id: str | None) -> BalanceOut:
    bal = await warehouse_service.balance(product_id, bin_id)
    names = await _resolve(product_ids={product_id}, bin_ids={bin_id} if bin_id else set())
    name, sku = names.product(product_id)
    return BalanceOut(
        productId=product_id, productName=name, productSku=sku,
        binId=bin_id, binLabel=names.bin_label(bin_id), balance=bal,
    )


async def list_balances() -> BalanceListOut:
    rows = await warehouse_service.all_balances()
    names = await _resolve(
        product_ids={r["product_id"] for r in rows}, bin_ids={r["bin_id"] for r in rows},
    )
    items = []
    for r in rows:
        name, sku = names.product(r["product_id"])
        items.append(BalanceOut(
            productId=r["product_id"], productName=name, productSku=sku,
            binId=r["bin_id"], binLabel=names.bin_label(r["bin_id"]), balance=r["total"],
        ))
    items.sort(key=lambda b: (b.productName or "", b.binLabel or ""))
    return BalanceListOut(items=items, total=len(items))


# ── ledger ──────────────────────────────────────────────────────────────────
def _movement_out(m: StockMovement, names: _Names) -> StockMovementOut:
    pid = str(m.product_id)
    name, sku = names.product(pid)
    uid = str(m.origin_user_id) if m.origin_user_id else None
    return StockMovementOut(
        id=str(m.id), productId=pid, productName=name, productSku=sku,
        binId=str(m.bin_id), binLabel=names.bin_label(str(m.bin_id)), kind=m.kind, qty=m.qty,
        reason=m.reason, originUserId=uid, originUserName=names.user(uid), at=m.at,
    )


async def list_movements(product_id: str | None, bin_id: str | None, limit: int, offset: int) -> StockMovementListOut:
    limit = min(max(limit, 1), 2000)
    items, total = await warehouse_service.list_movements(product_id, bin_id, limit, max(offset, 0))
    names = await _resolve(
        product_ids={str(m.product_id) for m in items},
        bin_ids={str(m.bin_id) for m in items},
        user_ids={str(m.origin_user_id) for m in items if m.origin_user_id},
    )
    return StockMovementListOut(items=[_movement_out(m, names) for m in items], total=total)


def _batch_out(b: Batch, names: _Names) -> BatchOut:
    pid = str(b.product_id)
    name, sku = names.product(pid)
    return BatchOut(
        id=str(b.id), productId=pid, productName=name, productSku=sku,
        lotNumber=b.lot_number, expiry=b.expiry, receivedQty=b.received_qty,
    )


async def list_batches(product_id: str | None, limit: int, offset: int) -> BatchListOut:
    limit = min(max(limit, 1), 2000)
    items, total = await warehouse_service.list_batches(product_id, limit, max(offset, 0))
    names = await _resolve(product_ids={str(b.product_id) for b in items})
    return BatchListOut(items=[_batch_out(b, names) for b in items], total=total)


# ── receiving ───────────────────────────────────────────────────────────────
def _grn_out(g: GRN, names: _Names) -> GRNOut:
    lines = []
    for l in g.lines:
        pid = str(l.product_id)
        name, sku = names.product(pid)
        lines.append(GRNLineOut(
            productId=pid, productName=name, productSku=sku, qty=l.qty, bonusQty=l.bonus_qty,
            unitPrice=l.unit_price, discPercent=l.disc_percent, expiry=l.expiry, taxRate=l.tax_rate,
        ))
    uid = str(g.received_by_id) if g.received_by_id else None
    return GRNOut(
        id=str(g.id), grnNumber=g.grn_number, supplierId=str(g.supplier_id),
        supplierName=names.suppliers.get(str(g.supplier_id)), partyInvNo=g.party_inv_no,
        binId=str(g.bin_id), binLabel=names.bin_label(str(g.bin_id)), gstMode=g.gst_mode,
        advanceTax=g.advance_tax, approved=g.approved, receivedByUserId=uid,
        receivedByName=names.user(uid), at=g.at, lines=lines,
    )


async def _grn_names(grns: list[GRN]) -> _Names:
    return await _resolve(
        product_ids={str(l.product_id) for g in grns for l in g.lines},
        bin_ids={str(g.bin_id) for g in grns},
        supplier_ids={str(g.supplier_id) for g in grns},
        user_ids={str(g.received_by_id) for g in grns if g.received_by_id},
    )


async def receive_grn(user: User, payload: GRNCreateRequest) -> GRNOut:
    try:
        grn = await warehouse_service.receive_grn(user, payload)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _grn_out(grn, await _grn_names([grn]))


async def list_grns(limit: int, offset: int) -> GRNListOut:
    limit = min(max(limit, 1), 500)
    items, total = await warehouse_service.list_grns(limit, max(offset, 0))
    names = await _grn_names(items)
    return GRNListOut(items=[_grn_out(g, names) for g in items], total=total)


# ── requisitions ────────────────────────────────────────────────────────────
def _requisition_out(r: Requisition, names: _Names) -> RequisitionOut:
    pid = str(r.product_id)
    pname, sku = names.product(pid)
    bname, bcode = names.branch(str(r.branch_id))
    uid = str(r.decided_by_id) if r.decided_by_id else None
    return RequisitionOut(
        id=str(r.id), requisitionNumber=r.requisition_number, branchId=str(r.branch_id),
        branchName=bname, branchCode=bcode, productId=pid, productName=pname, productSku=sku,
        qtyRequested=r.qty_requested, status=r.status, requestedAt=r.requested_at,
        decidedByUserId=uid, decidedByName=names.user(uid), decidedAt=r.decided_at,
    )


async def _requisition_names(rows: list[Requisition]) -> _Names:
    return await _resolve(
        product_ids={str(r.product_id) for r in rows},
        branch_ids={str(r.branch_id) for r in rows},
        user_ids={str(r.decided_by_id) for r in rows if r.decided_by_id},
    )


async def list_requisitions(status_filter: str | None, limit: int, offset: int) -> RequisitionListOut:
    limit = min(max(limit, 1), 500)
    items, total = await warehouse_service.list_requisitions(status_filter, limit, max(offset, 0))
    names = await _requisition_names(items)
    return RequisitionListOut(items=[_requisition_out(r, names) for r in items], total=total)


async def create_requisition(payload: RequisitionCreateRequest) -> RequisitionOut:
    try:
        req = await warehouse_service.create_requisition(payload.branchId, payload.productId, payload.qtyRequested)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _requisition_out(req, await _requisition_names([req]))


async def approve_requisition(user: User, requisition_id: str) -> RequisitionOut:
    try:
        req, _transfer = await warehouse_service.approve_requisition(user, requisition_id)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _requisition_out(req, await _requisition_names([req]))


async def reject_requisition(user: User, requisition_id: str) -> RequisitionOut:
    try:
        req = await warehouse_service.reject_requisition(user, requisition_id)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _requisition_out(req, await _requisition_names([req]))


# ── transfers ───────────────────────────────────────────────────────────────
def _transfer_out(t: Transfer, names: _Names, requisition_numbers: dict[str, str]) -> TransferOut:
    lines = []
    for l in t.lines:
        pid = str(l.product_id)
        name, sku = names.product(pid)
        lines.append(TransferLineOut(
            productId=pid, productName=name, productSku=sku, qtySent=l.qty_sent, qtyReceived=l.qty_received,
        ))
    bname, bcode = names.branch(str(t.branch_id))
    rid = str(t.requisition_id) if t.requisition_id else None
    return TransferOut(
        id=str(t.id), transferNumber=t.transfer_number, branchId=str(t.branch_id),
        branchName=bname, branchCode=bcode,
        requisitionId=rid, requisitionNumber=requisition_numbers.get(rid) if rid else None,
        status=t.status, vehicle=t.vehicle, driver=t.driver, requestedAt=t.requested_at,
        approvedAt=t.approved_at, dispatchedAt=t.dispatched_at, receivedAt=t.received_at,
        disputeOpen=t.dispute_open, disputeNote=t.dispute_note, lines=lines,
    )


async def _transfer_context(rows: list[Transfer]) -> tuple[_Names, dict[str, str]]:
    names = await _resolve(
        product_ids={str(l.product_id) for t in rows for l in t.lines},
        branch_ids={str(t.branch_id) for t in rows},
    )
    req_ids = [str(t.requisition_id) for t in rows if t.requisition_id]
    numbers = {}
    if req_ids:
        numbers = {
            str(r.id): r.requisition_number for r in await Requisition.filter(id__in=req_ids)
        }
    return names, numbers


async def list_transfers(status_filter: str | None, limit: int, offset: int) -> TransferListOut:
    limit = min(max(limit, 1), 500)
    items, total = await warehouse_service.list_transfers(status_filter, limit, max(offset, 0))
    names, numbers = await _transfer_context(items)
    return TransferListOut(items=[_transfer_out(t, names, numbers) for t in items], total=total)


async def _one_transfer(t: Transfer) -> TransferOut:
    names, numbers = await _transfer_context([t])
    return _transfer_out(t, names, numbers)


async def dispatch_transfer(user: User, transfer_id: str, payload: DispatchRequest) -> TransferOut:
    try:
        transfer = await warehouse_service.dispatch_transfer(user, transfer_id, payload)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return await _one_transfer(transfer)


async def receive_transfer(transfer_id: str, payload: ReceiveTransferRequest) -> TransferOut:
    try:
        transfer = await warehouse_service.receive_transfer(transfer_id, payload)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return await _one_transfer(transfer)


async def resolve_dispute(transfer_id: str, note: str | None) -> TransferOut:
    try:
        transfer = await warehouse_service.resolve_dispute(transfer_id, note)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return await _one_transfer(transfer)


# ── cycle counts ────────────────────────────────────────────────────────────
def _count_out(c: CycleCount, names: _Names) -> CountOut:
    pid = str(c.product_id)
    name, sku = names.product(pid)
    uid = str(c.counted_by_id) if c.counted_by_id else None
    return CountOut(
        id=str(c.id), productId=pid, productName=name, productSku=sku,
        binId=str(c.bin_id), binLabel=names.bin_label(str(c.bin_id)),
        systemQty=c.system_qty, countedQty=c.counted_qty, status=c.status,
        countedByUserId=uid, countedByName=names.user(uid),
        approvedByUserId=str(c.approved_by_id) if c.approved_by_id else None, at=c.at,
    )


async def _count_names(rows: list[CycleCount]) -> _Names:
    return await _resolve(
        product_ids={str(c.product_id) for c in rows},
        bin_ids={str(c.bin_id) for c in rows},
        user_ids={str(c.counted_by_id) for c in rows if c.counted_by_id},
    )


async def list_counts(limit: int, offset: int) -> CountListOut:
    limit = min(max(limit, 1), 500)
    items, total = await warehouse_service.list_counts(limit, max(offset, 0))
    names = await _count_names(items)
    return CountListOut(items=[_count_out(c, names) for c in items], total=total)


async def submit_count(user: User, payload: CountSubmitRequest) -> CountOut:
    try:
        count = await warehouse_service.submit_count(user, payload)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _count_out(count, await _count_names([count]))


async def approve_count(user: User, count_id: str) -> CountOut:
    try:
        count = await warehouse_service.approve_count(user, count_id)
    except warehouse_service.WarehouseError as exc:
        raise _fail(exc)
    return _count_out(count, await _count_names([count]))
