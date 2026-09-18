"""The godown's Item master — add, edit, import, export, pictures and attachments."""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from app.controllers import items_controller, price_history_controller
from app.middlewares.auth import require_any_permission, require_permission
from app.schemas.price_history import PriceChangeChoicesOut, PriceChangesReportOut, PriceHistoryEntryOut
from app.models import User
from app.services.rbac_service import can_see_costs
from app.schemas.import_result import ImportSummary
from app.schemas.items import (
    AttachmentOut, ItemAliasIn, ItemAliasOut, ItemCreate, ItemDetailOut, ItemFacetsOut, ItemListOut, ItemOut,
    ItemSupplierIn, ItemSupplierOut, ItemUpdate,
)

router = APIRouter(prefix="/warehouse/items", tags=["warehouse-items"])

# Every warehouse screen that picks an Item reads the master; only the Item screen's manage grant writes it.
_read = require_any_permission(
    ("warehouse.items", "R"), ("warehouse.dashboard", "R"), ("warehouse.bins", "R"), ("warehouse.receiving", "R"),
    ("warehouse.picking", "R"), ("warehouse.counts", "R"), ("warehouse.transfers", "R"), ("warehouse.requisitions", "R"),
    ("executive.stock", "R"),
)
_manage = require_permission("warehouse.items.manage", "W")


@router.get("", response_model=ItemListOut)
async def list_items(
    q: str | None = None, ids: str | None = None, limit: int = 50, offset: int = 0, sort: str | None = None,
    order: str | None = None, supplierId: str | None = None, includeInactive: bool = True, needsDetails: bool | None = None,
    user: User = Depends(_read),
) -> ItemListOut:
    """Paged and searched on the server (name, code or barcode). `sort`: sku, name, brand, category, price,
    taxRate, unit, avgCost; `order`: asc or desc. `ids` resolves a known set of Items (at most 200)."""
    id_list = [i for i in (ids.split(",") if ids else []) if i][:200]
    costs = await can_see_costs(user)
    if not costs and sort == "avgCost":
        sort = None
    out = await items_controller.list_all(
        q, min(max(limit, 1), 200), max(offset, 0), id_list or None, sort, order, supplierId, includeInactive, needsDetails,
    )
    if not costs:
        for item in out.items:
            item.avgCost = None
    return out


@router.post("", response_model=ItemOut)
async def create_item(payload: ItemCreate, user: User = Depends(_manage)) -> ItemOut:
    """Add one Item. Leave `sku` out to get the next code. Refuses a code or barcode another Item already uses."""
    return await items_controller.create(payload, user)


@router.post("/import", response_model=ImportSummary)
async def import_items(file: UploadFile = File(...), user: User = Depends(_manage)) -> ImportSummary:
    """CSV / Excel of Items. New codes are added; existing codes update only the cells the file filled."""
    return await items_controller.import_file(file.filename, await file.read(), user)


@router.post("/import-aliases", response_model=ImportSummary)
async def import_aliases(file: UploadFile = File(...), user: User = Depends(_manage)) -> ImportSummary:
    """Alternate barcodes: AliasName plus the Item's Name (or its code), optional qty and Remarks."""
    return await items_controller.import_aliases_file(file.filename, await file.read())


@router.get("/export")
async def export_items(user: User = Depends(_read)) -> Response:
    """The whole Item master as an Excel workbook, with column titles import reads back."""
    return await items_controller.export(await can_see_costs(user))


@router.get("/lookup", response_model=ItemOut | None)
async def lookup(code: str, user: User = Depends(_read)) -> ItemOut | None:
    out = await items_controller.lookup(code)
    if out and not await can_see_costs(user):
        out.avgCost = None
    return out


@router.get("/facets", response_model=ItemFacetsOut)
async def facets(user: User = Depends(_read)) -> ItemFacetsOut:
    return await items_controller.facets()


# Price history: the Godown's Price Changes report (its own tick), and one Item's history on the Item form. Cost rows
# only reach people who see cost.
_price_report = require_permission("warehouse.price-changes", "R")
_item_history = require_any_permission(("warehouse.items", "R"), ("warehouse.price-changes", "R"))


@router.get("/price-history", response_model=PriceChangesReportOut)
async def price_history_report(
    from_: datetime | None = Query(None, alias="from"), to: datetime | None = None, department: str | None = None,
    userId: str | None = None, source: str | None = None, field: str | None = None, q: str | None = None,
    limit: int = 100, offset: int = 0, user: User = Depends(_price_report),
) -> PriceChangesReportOut:
    """Every change to a godown Item's sale or retail price, cost or discount in the window, newest first.
    `userId=none` is changes nobody made by hand. `limit` up to 20000 for an export."""
    return await price_history_controller.report(
        from_, to, department, userId, source, field, q, min(max(limit, 1), 20000), max(offset, 0), await can_see_costs(user),
    )


@router.get("/price-history/choices", response_model=PriceChangeChoicesOut)
async def price_history_choices(user: User = Depends(_price_report)) -> PriceChangeChoicesOut:
    return await price_history_controller.choices(await can_see_costs(user))


@router.get("/{product_id}/price-history", response_model=list[PriceHistoryEntryOut])
async def item_price_history(product_id: str, user: User = Depends(_item_history)) -> list[PriceHistoryEntryOut]:
    """One Item's price history, newest first."""
    return await price_history_controller.for_item(product_id, await can_see_costs(user))


# Paths with an id come last, so /import, /export, /lookup and /facets are never read as an id.
@router.get("/{product_id}", response_model=ItemDetailOut)
async def detail(product_id: str, user: User = Depends(_read)) -> ItemDetailOut:
    out = await items_controller.detail(product_id)
    if not await can_see_costs(user):
        out.avgCost = None
        out.lastPurchase = None
    return out


@router.patch("/{product_id}", response_model=ItemOut)
async def update_item(product_id: str, payload: ItemUpdate, user: User = Depends(_manage)) -> ItemOut:
    return await items_controller.update(product_id, payload, user)


@router.put("/{product_id}/aliases", response_model=list[ItemAliasOut])
async def replace_aliases(product_id: str, payload: list[ItemAliasIn], user: User = Depends(_manage)) -> list[ItemAliasOut]:
    return await items_controller.replace_aliases(product_id, payload)


@router.put("/{product_id}/suppliers", response_model=list[ItemSupplierOut])
async def replace_suppliers(product_id: str, payload: list[ItemSupplierIn], user: User = Depends(_manage)) -> list[ItemSupplierOut]:
    return await items_controller.replace_suppliers(product_id, payload)


@router.get("/{product_id}/picture")
async def picture(product_id: str, user: User = Depends(_read)):
    return await items_controller.picture(product_id)


@router.put("/{product_id}/picture", response_model=ItemOut)
async def upload_picture(product_id: str, file: UploadFile = File(...), user: User = Depends(_manage)) -> ItemOut:
    return await items_controller.set_picture(product_id, await file.read())


@router.delete("/{product_id}/picture", response_model=ItemOut)
async def delete_picture(product_id: str, user: User = Depends(_manage)) -> ItemOut:
    return await items_controller.remove_picture(product_id)


@router.get("/{product_id}/attachments", response_model=list[AttachmentOut])
async def attachments(product_id: str, user: User = Depends(_read)) -> list[AttachmentOut]:
    return await items_controller.list_attachments(product_id)


@router.post("/{product_id}/attachments", response_model=AttachmentOut)
async def add_attachment(
    product_id: str, file: UploadFile = File(...), note: str | None = Form(default=None), user: User = Depends(_manage),
) -> AttachmentOut:
    """PDF, image, Word, Excel, CSV or text, under 10 MB. Checked by content, not just the name."""
    return await items_controller.add_attachment(product_id, file.filename or "file", await file.read(), note, user)


@router.get("/{product_id}/attachments/{attachment_id}")
async def download_attachment(product_id: str, attachment_id: str, user: User = Depends(_read)):
    return await items_controller.download_attachment(product_id, attachment_id)


@router.delete("/{product_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(product_id: str, attachment_id: str, user: User = Depends(_manage)) -> Response:
    await items_controller.remove_attachment(product_id, attachment_id)
    return Response(status_code=204)
