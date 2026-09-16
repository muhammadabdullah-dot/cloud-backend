from fastapi import APIRouter, Depends

from app.controllers import warehouse_controller
from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.services.rbac_service import can_see_costs
from app.schemas.warehouse import (
    BalanceListOut,
    BalanceOut,
    BatchListOut,
    BinOut,
    CountListOut,
    CountOut,
    CountSubmitRequest,
    DispatchRequest,
    GRNCreateRequest,
    GRNListOut,
    GRNOut,
    ProductListOut,
    ReceiveTransferRequest,
    RequisitionCreateRequest,
    RequisitionListOut,
    RequisitionOut,
    ResolveDisputeRequest,
    StockMovementListOut,
    SupplierOut,
    TransferCreateRequest,
    TransferReasonRequest,
    TransferListOut,
    TransferOut,
)

router = APIRouter(prefix="/warehouse", tags=["warehouse"])

_dashboard = require_permission("warehouse.dashboard", "R")
_bins_read = require_permission("warehouse.bins", "R")
_receiving_read = require_permission("warehouse.receiving", "R")
_receiving_write = require_permission("warehouse.receiving", "W")
_requisitions_read = require_permission("warehouse.requisitions", "R")
_requisitions_approve = require_permission("warehouse.requisitions.approve", "X")
_transfers_read = require_permission("warehouse.transfers", "R")
_transfers_dispatch = require_permission("warehouse.transfers.dispatch", "X")
_transfers_manage = require_permission("warehouse.transfers.manage", "X")
_requisitions_write = require_permission("warehouse.requisitions", "W")
_counts_read = require_permission("warehouse.counts", "R")
_counts_write = require_permission("warehouse.counts", "W")
_counts_approve = require_permission("warehouse.counts.approve", "X")

# Master data and the ledger are read from nearly every warehouse screen and from Executive's
# stock rollup, so gating them on one screen's resource would break the others. Writes stay on
# the owning resource.
_masters_read = require_any_permission(
    ("warehouse.dashboard", "R"), ("warehouse.bins", "R"), ("warehouse.receiving", "R"),
    ("warehouse.picking", "R"), ("warehouse.counts", "R"), ("warehouse.transfers", "R"),
    ("executive.stock", "R"), ("executive.dashboard", "R"),
)
_picking_read = require_any_permission(("warehouse.picking", "R"), ("warehouse.transfers", "R"))
_decisions_read = require_any_permission(
    ("warehouse.decisions", "R"), ("warehouse.requisitions", "R"), ("warehouse.transfers", "R"), ("warehouse.picking", "R"),
)


# ── masters ─────────────────────────────────────────────────────────────────
@router.get("/products", response_model=ProductListOut)
async def products(q: str | None = None, limit: int = 100, offset: int = 0, user: User = Depends(_masters_read)) -> ProductListOut:
    return await warehouse_controller.list_products(q, limit, offset)


@router.get("/suppliers", response_model=list[SupplierOut])
async def suppliers(user: User = Depends(_masters_read)) -> list[SupplierOut]:
    return await warehouse_controller.list_suppliers()


@router.get("/bins", response_model=list[BinOut])
async def bins(includeInactive: bool = False, user: User = Depends(_masters_read)) -> list[BinOut]:
    """Bins stock can go into — switched-off bins only when asked."""
    return await warehouse_controller.list_bins(includeInactive)


# ── balances & ledger ───────────────────────────────────────────────────────
@router.get("/balance", response_model=BalanceOut)
async def get_balance(productId: str, binId: str | None = None, user: User = Depends(_masters_read)) -> BalanceOut:
    return await warehouse_controller.get_balance(productId, binId)


@router.get("/balances", response_model=BalanceListOut)
async def balances(user: User = Depends(_masters_read)) -> BalanceListOut:
    """Every non-zero (product, bin) balance, folded in one grouped query. What the dashboard,
    Racks & Bins and Executive's stock value all need. Average cost only for people who see cost."""
    out = await warehouse_controller.list_balances()
    if not await can_see_costs(user):
        for row in out.items:
            row.avgCost = None
    return out


@router.get("/movements", response_model=StockMovementListOut)
async def movements(
    productId: str | None = None, binId: str | None = None, limit: int = 500, offset: int = 0,
    user: User = Depends(_masters_read),
) -> StockMovementListOut:
    return await warehouse_controller.list_movements(productId, binId, limit, offset)


@router.get("/batches", response_model=BatchListOut)
async def batches(productId: str | None = None, limit: int = 500, offset: int = 0, user: User = Depends(_picking_read)) -> BatchListOut:
    return await warehouse_controller.list_batches(productId, limit, offset)


# ── receiving ───────────────────────────────────────────────────────────────
@router.post("/grn", response_model=GRNOut)
async def receive_grn(payload: GRNCreateRequest, user: User = Depends(_receiving_write)) -> GRNOut:
    return await warehouse_controller.receive_grn(user, payload)


@router.get("/grns", response_model=GRNListOut)
async def grns(limit: int = 50, offset: int = 0, user: User = Depends(_receiving_read)) -> GRNListOut:
    return await warehouse_controller.list_grns(limit, offset)


# ── requisitions ────────────────────────────────────────────────────────────
@router.get("/requisitions", response_model=RequisitionListOut)
async def requisitions(status: str | None = None, limit: int = 100, offset: int = 0, user: User = Depends(_decisions_read)) -> RequisitionListOut:
    return await warehouse_controller.list_requisitions(status, limit, offset)


@router.post("/requisitions", response_model=RequisitionOut)
async def create_requisition(payload: RequisitionCreateRequest, user: User = Depends(_requisitions_write)) -> RequisitionOut:
    """A branch asking for stock. Nothing on either frontend authors one yet — branch-app has no
    requisition screen — so this is here to be the endpoint a branch calls, and to let the Cloud
    approval flow be exercised with data that wasn't seeded."""
    return await warehouse_controller.create_requisition(payload)


@router.post("/requisitions/{requisition_id}/approve", response_model=RequisitionOut)
async def approve_requisition(requisition_id: str, user: User = Depends(_requisitions_approve)) -> RequisitionOut:
    return await warehouse_controller.approve_requisition(user, requisition_id)


@router.post("/requisitions/{requisition_id}/reject", response_model=RequisitionOut)
async def reject_requisition(requisition_id: str, user: User = Depends(_requisitions_approve)) -> RequisitionOut:
    return await warehouse_controller.reject_requisition(user, requisition_id)


# ── transfers ───────────────────────────────────────────────────────────────
@router.get("/transfers", response_model=TransferListOut)
async def transfers(status: str | None = None, limit: int = 100, offset: int = 0, user: User = Depends(_decisions_read)) -> TransferListOut:
    return await warehouse_controller.list_transfers(status, limit, offset)


@router.post("/transfers", response_model=TransferOut)
async def create_transfer(payload: TransferCreateRequest, user: User = Depends(_transfers_manage)) -> TransferOut:
    """Send stock to a branch without waiting for a requisition. Starts approved; dispatch it next."""
    return await warehouse_controller.create_transfer(user, payload)


@router.post("/transfers/{transfer_id}/dispatch", response_model=TransferOut)
async def dispatch_transfer(transfer_id: str, payload: DispatchRequest, user: User = Depends(_transfers_dispatch)) -> TransferOut:
    return await warehouse_controller.dispatch_transfer(user, transfer_id, payload)


@router.post("/transfers/{transfer_id}/receive", response_model=TransferOut)
async def receive_transfer(transfer_id: str, payload: ReceiveTransferRequest, user: User = Depends(_transfers_manage)) -> TransferOut:
    """Confirming what actually arrived. A branch server calls this once sync exists; until then
    it is how the received / short-received half of the lifecycle can be reached at all."""
    return await warehouse_controller.receive_transfer(transfer_id, payload)


@router.post("/transfers/{transfer_id}/send-without-answer", response_model=TransferOut)
async def send_without_answer(transfer_id: str, payload: TransferReasonRequest, user: User = Depends(_transfers_manage)) -> TransferOut:
    """The branch has been offline a long time: let the transfer go without its answer, with a written reason."""
    return await warehouse_controller.transfer_step("send-without-answer", user, transfer_id, payload.reason)


@router.post("/transfers/{transfer_id}/ask-again", response_model=TransferOut)
async def ask_again(transfer_id: str, user: User = Depends(_transfers_manage)) -> TransferOut:
    """The branch declined: ask it again (after talking to them)."""
    return await warehouse_controller.transfer_step("ask-again", user, transfer_id)


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferOut)
async def cancel_transfer(transfer_id: str, payload: TransferReasonRequest, user: User = Depends(_transfers_manage)) -> TransferOut:
    """Cancel a transfer that hasn't left yet. The branches involved are told."""
    return await warehouse_controller.transfer_step("cancel", user, transfer_id, payload.reason)


@router.post("/transfers/{transfer_id}/resolve-dispute", response_model=TransferOut)
async def resolve_dispute(transfer_id: str, payload: ResolveDisputeRequest, user: User = Depends(_transfers_manage)) -> TransferOut:
    return await warehouse_controller.resolve_dispute(transfer_id, payload.note)


# ── cycle counts ────────────────────────────────────────────────────────────
@router.get("/counts", response_model=CountListOut)
async def counts(limit: int = 100, offset: int = 0, user: User = Depends(_counts_read)) -> CountListOut:
    return await warehouse_controller.list_counts(limit, offset)


@router.post("/counts", response_model=CountOut)
async def submit_count(payload: CountSubmitRequest, user: User = Depends(_counts_write)) -> CountOut:
    return await warehouse_controller.submit_count(user, payload)


@router.post("/counts/{count_id}/approve", response_model=CountOut)
async def approve_count(count_id: str, user: User = Depends(_counts_approve)) -> CountOut:
    return await warehouse_controller.approve_count(user, count_id)
