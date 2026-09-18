from datetime import datetime

from fastapi import HTTPException

from app.models import Product
from app.schemas.price_history import PriceChangeChoicesOut, PriceChangesReportOut, PriceHistoryEntryOut
from app.services import price_history_service


async def for_item(product_id: str, include_costs: bool) -> list[PriceHistoryEntryOut]:
    if not await Product.exists(id=product_id):
        raise HTTPException(404, "Item not found")
    return [PriceHistoryEntryOut(**e) for e in await price_history_service.for_item(product_id, include_costs=include_costs)]


async def report(
    from_at: datetime | None, to_at: datetime | None, department: str | None, user_id: str | None, source: str | None,
    field: str | None, q: str | None, limit: int, offset: int, include_costs: bool,
) -> PriceChangesReportOut:
    if field in price_history_service.COST_FIELDS and not include_costs:
        return PriceChangesReportOut(rows=[], total=0)
    found = await price_history_service.search(
        from_at, to_at, department, user_id, source, field, q, limit, offset, include_costs=include_costs,
    )
    return PriceChangesReportOut(rows=[PriceHistoryEntryOut(**e) for e in found["rows"]], total=found["total"])


async def choices(include_costs: bool) -> PriceChangeChoicesOut:
    return PriceChangeChoicesOut(**await price_history_service.choices(include_costs))
