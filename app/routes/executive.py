from fastapi import APIRouter, Depends

from app.controllers import executive_controller
from app.middlewares.auth import require_any_permission
from app.models import User
from app.schemas.executive import KpiDetailOut, KpiListOut

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
async def kpis(period: str = "30d", user: User = Depends(_read)) -> KpiListOut:
    """The control-room grid. Every tile carries its own `source` and `asOf` — a live godown
    figure and a branch's last-reported figure are different claims."""
    return await executive_controller.list_kpis(period)


@router.get("/kpis/{kpi_id}", response_model=KpiDetailOut)
async def kpi_detail(
    kpi_id: str,
    period: str = "30d",
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
    return await executive_controller.kpi_detail(kpi_id, period, focus)
