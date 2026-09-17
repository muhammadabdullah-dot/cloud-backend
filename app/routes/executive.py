from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.controllers import branch_page_controller, executive_controller
from app.controllers.executive_controller import KpiDetailOut, KpiListOut
from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.services import analytics_service, branch_comparison_service
from app.services import executive_service as ex

router = APIRouter(prefix="/executive", tags=["executive"])

# The dashboard is one screen made of many KPIs, each belonging to a different executive concern
# — sales, cash, stock, sync. Gating the grid on every one of those would mean an executive with
# five of six grants sees nothing, so any executive read opens it and each tile carries its own
# source. The underlying domain endpoints stay gated on their own resources.
_read = require_any_permission(
    ("executive.dashboard", "R"), ("executive.branches", "R"), ("executive.stock", "R"),
    ("executive.cash", "R"), ("executive.staffing", "R"), ("executive.sync-health", "R"),
)


@router.get("/kpis", response_model=KpiListOut)
async def kpis(
    period: str = "30d",
    # Your own dates, with period=custom. `from` is a Python keyword, hence the aliases.
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    # One branch by code, or every branch when left out.
    branch: str | None = None,
    user: User = Depends(_read),
) -> KpiListOut:
    """The control-room grid. Every tile carries its own `source` and `asOf` — a live godown
    figure and a branch's last-reported figure are different claims."""
    return await executive_controller.list_kpis(period, start=start, end=end, branch=branch)


@router.get("/kpis/{kpi_id}", response_model=KpiDetailOut)
async def kpi_detail(
    kpi_id: str,
    period: str = "30d",
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    branch: str | None = None,
    # What ABC ranks Items by: sales (value), profit or units.
    basis: str | None = None,
    # Focus params for second-level views. A drill-down row links to another KPI and passes one of
    # these — a category name, a product sku, a cashier, a day, a branch code, a supplier id — so
    # every breakdown can be gone behind rather than being the end of the road.
    name: str | None = None,
    sku: str | None = None,
    day: str | None = None,
    code: str | None = None,
    id: str | None = None,
    user: User = Depends(_read),
) -> KpiDetailOut:
    """The drill-down behind a tile: the headline, its trend, its breakdown, and a plain statement
    of how it was computed and what it deliberately excludes. Rows link onward where there is
    something real to go to."""
    focus = {"name": name, "sku": sku, "day": day, "code": code, "id": id}
    return await executive_controller.kpi_detail(kpi_id, period, focus, start=start, end=end, branch=branch, basis=basis)


# ── one branch, thoroughly ──────────────────────────────────────────────────────────────────────
# The Executive's page for each branch. Sales and Products on that page are the KPI views above, limited to the branch;
# these are the tabs that have no KPI view of their own. Every tab takes the same dates as the rest of the module.
_branch_read = require_permission("executive.branches", "R")


@router.get("/branch-options")
async def branch_options(user: User = Depends(_branch_read)) -> list[dict]:
    """Every registered branch by code and name, for a branch picker."""
    return await analytics_service.branch_options()


@router.get("/branches/{code}")
async def branch_head(code: str, period: str = "30d", start: str | None = Query(None, alias="from"),
                      end: str | None = Query(None, alias="to"), user: User = Depends(_branch_read)) -> dict:
    """Who the branch is, how fresh its figures are, and the choices the page offers."""
    return await branch_page_controller.head(await branch_page_controller.scope_for(code, period, start, end))


@router.get("/branches/{code}/{tab}")
async def branch_tab(code: str, tab: str, period: str = "30d", start: str | None = Query(None, alias="from"),
                     end: str | None = Query(None, alias="to"), user: User = Depends(_branch_read)) -> dict:
    scope = await branch_page_controller.scope_for(code, period, start, end)
    if tab == "overview":
        return await branch_page_controller.overview(scope)
    if tab == "people":
        return await branch_page_controller.people(scope)
    if tab == "stock":
        return await branch_page_controller.stock(scope)
    if tab == "accounts":
        # The branch's books also need the ability to see branch books; without it the tab says so.
        return await branch_page_controller.accounts(scope, user)
    if tab == "transfers":
        return await branch_page_controller.transfers(scope)
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"A branch page has no tab called {tab}.")


# ── the same Item across branches ───────────────────────────────────────────────────────────────
@router.get("/branch-comparison")
async def branch_comparison(period: str = "30d", start: str | None = Query(None, alias="from"),
                            end: str | None = Query(None, alias="to"), branch: str | None = None, q: str | None = None,
                            receiveDays: int = branch_comparison_service.RECEIVE_DAYS, keepDays: int = branch_comparison_service.KEEP_DAYS,
                            user: User = Depends(_branch_read)) -> dict:
    """Units per day, stock and days of cover for each Item at every reporting branch, and what could be moved.
    `branch` keeps the suggestions and Items to the ones that branch is part of (its own page's tab)."""
    scope = await executive_controller.resolve_scope(period, start, end, None)
    out = await branch_comparison_service.compare(scope.period, focus=branch, q=q, receive_days=receiveDays, keep_days=keepDays)
    return {**out, "period": executive_controller._period_out(scope.period).model_dump(mode="json"),
            "periods": [executive_controller._period_out(ex.resolve_period(p)).model_dump(mode="json") for p in ex.PERIOD_IDS]}


class TransferRequestLine(BaseModel):
    sku: str
    qty: Decimal


class TransferRequestIn(BaseModel):
    fromCode: str
    toCode: str
    lines: list[TransferRequestLine]
    note: str | None = None


# Asking a branch to send stock is deciding to send stock, the same ability the godown's transfers use.
@router.get("/branch-comparison/pair")
async def branch_pair(a: str | None = None, b: str | None = None, view: str = "good-a", q: str | None = None,
                      period: str = "30d", start: str | None = Query(None, alias="from"), end: str | None = Query(None, alias="to"),
                      user: User = Depends(_branch_read)) -> dict:
    """Two branches side by side: their figures, then each Item at both, sorted into what sells well at one and poorly or
    not at all at the other."""
    scope = await executive_controller.resolve_scope(period, start, end, None)
    out = await branch_comparison_service.pair(scope.period, a, b, view=view, q=q)
    return {**out, "period": executive_controller._period_out(scope.period).model_dump(mode="json"),
            "periods": [executive_controller._period_out(ex.resolve_period(p)).model_dump(mode="json") for p in ex.PERIOD_IDS]}


@router.post("/branch-comparison/transfer-requests")
async def create_transfer_request(payload: TransferRequestIn,
                                  user: User = Depends(require_permission("warehouse.transfers.manage", "X"))) -> dict:
    try:
        transfer = await branch_comparison_service.create_transfer_request(
            user, payload.fromCode, payload.toCode, [(line.sku, line.qty) for line in payload.lines], payload.note)
    except branch_comparison_service.ComparisonError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    await transfer.fetch_related("branch", "source_branch")
    return {"id": str(transfer.id), "number": transfer.transfer_number, "status": transfer.status, "ackStatus": transfer.ack_status,
            "from": transfer.source_branch.name, "to": transfer.branch.name}
