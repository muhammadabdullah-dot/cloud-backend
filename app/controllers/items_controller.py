from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from app.models import Product, ProductAlias, ProductAttachment, ProductSupplier, User
from app.schemas.import_result import ImportSummary
from app.schemas.items import (
    AttachmentOut, ItemAliasIn, ItemAliasOut, ItemCreate, ItemDetailOut, ItemFacetsOut, ItemListOut, ItemOut, ItemRef,
    ItemSupplierIn, ItemSupplierOut, ItemUpdate, LastPurchaseOut, PriceChangeOut,
)
from app.services import items_service, media_service


def _alias_out(a: ProductAlias) -> ItemAliasOut:
    return ItemAliasOut(code=a.code, remarks=a.remarks, qty=a.qty, discPercent=a.disc_percent, discFlat=a.disc_flat)


def _fields(p: Product, include_aliases: bool = False, attachments: int = 0) -> dict:
    return dict(
        id=p.id, sku=p.sku, name=p.name, price=p.price, taxRate=p.tax_rate, isWeighed=p.is_weighed,
        unit=p.unit, barcode=p.barcode, packUnit=p.pack_unit, packSize=p.pack_size, avgCost=p.avg_cost,
        rpp=p.rpp, department=p.department, category=p.category, itemClass=p.item_class,
        subclass=p.subclass, manufacturer=p.manufacturer, brand=p.brand, active=p.active,
        aliases=[_alias_out(a) for a in p.aliases] if include_aliases else [],
        discPercent=p.disc_percent, discFlat=p.disc_flat, lockDisc=p.lock_disc, variant=p.variant,
        origin=p.origin, remarks=p.remarks, hasPicture=bool(p.picture), attachmentCount=attachments,
        parentId=p.parent_id, parentQty=p.parent_qty, reorderLevel=p.reorder_level, homeBinId=p.home_bin_id,
        needsDetails=p.needs_details, detailsNote=p.details_note if p.needs_details else None,
    )


def _out(p: Product, include_aliases: bool = False, attachments: int = 0) -> ItemOut:
    return ItemOut(**_fields(p, include_aliases, attachments))


def _supplier_out(link: ProductSupplier) -> ItemSupplierOut:
    return ItemSupplierOut(
        supplierId=link.supplier.id, supplierCode=link.supplier.code, supplierName=link.supplier.name, priority=link.priority,
    )


def _attachment_out(a: ProductAttachment) -> AttachmentOut:
    return AttachmentOut(
        id=str(a.id), fileName=a.file_name, contentType=a.content_type, sizeBytes=a.size_bytes, note=a.note,
        uploadedBy=a.uploaded_by.name if a.uploaded_by else None, uploadedAt=a.uploaded_at,
    )


def _fail(exc: items_service.ItemError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def list_all(q, limit, offset, ids, sort, order, supplier_id, include_inactive, needs_details=None) -> ItemListOut:
    items, total = await items_service.list_all(q, limit, offset, ids, sort, order, supplier_id, include_inactive, needs_details)
    counts = await items_service.attachment_counts([p.id for p in items])
    return ItemListOut(items=[_out(p, True, counts.get(p.id, 0)) for p in items], total=total)


async def detail(product_id: str) -> ItemDetailOut:
    try:
        d = await items_service.detail(product_id)
    except items_service.ItemError as exc:
        raise _fail(exc)
    p: Product = d["product"]
    last = d["last"]
    return ItemDetailOut(
        **_fields(p, True, d["attachments"]),
        suppliers=[_supplier_out(s) for s in d["suppliers"]],
        parent=ItemRef(id=d["parent"].id, sku=d["parent"].sku, name=d["parent"].name) if d["parent"] else None,
        children=[ItemRef(id=c.id, sku=c.sku, name=c.name, parentQty=c.parent_qty) for c in d["children"]],
        lastPurchase=LastPurchaseOut(
            unitPrice=last.unit_price, discPercent=last.disc_percent, at=last.grn.at,
            grnNumber=last.grn.grn_number, supplierName=last.grn.supplier.name,
        ) if last else None,
        stockOnHand=d["stock"],
        priceHistory=[
            PriceChangeOut(
                oldPrice=c.old_price, newPrice=c.new_price, oldRpp=c.old_rpp, newRpp=c.new_rpp, source=c.source,
                changedBy=c.changed_by.name if c.changed_by else None, at=c.at,
            )
            for c in d["prices"]
        ],
    )


async def create(payload: ItemCreate, user: User) -> ItemOut:
    try:
        product = await items_service.create(payload, user)
    except items_service.ItemError as exc:
        raise _fail(exc)
    await product.fetch_related("aliases")
    return _out(product, True)


async def update(product_id: str, payload: ItemUpdate, user: User) -> ItemOut:
    try:
        product = await items_service.update(product_id, payload, user)
    except items_service.ItemError as exc:
        raise _fail(exc)
    await product.fetch_related("aliases")
    return _out(product, True)


async def replace_aliases(product_id: str, payload: list[ItemAliasIn]) -> list[ItemAliasOut]:
    try:
        return [_alias_out(a) for a in await items_service.replace_aliases(product_id, payload)]
    except items_service.ItemError as exc:
        raise _fail(exc)


async def replace_suppliers(product_id: str, payload: list[ItemSupplierIn]) -> list[ItemSupplierOut]:
    try:
        return [_supplier_out(s) for s in await items_service.replace_suppliers(product_id, payload)]
    except items_service.ItemError as exc:
        raise _fail(exc)


async def facets() -> ItemFacetsOut:
    return ItemFacetsOut(**await items_service.facets())


async def lookup(code: str) -> ItemOut | None:
    found = await items_service.lookup_by_code(code)
    if not found:
        return None
    product, alias = found
    await product.fetch_related("aliases")
    out = _out(product, True)
    if alias:
        out.matchedAlias = _alias_out(alias)
    return out


async def picture(product_id: str) -> FileResponse:
    product = await Product.get_or_none(id=product_id)
    found = media_service.picture_file(product.picture if product else None)
    if not found:
        raise HTTPException(404, "No picture for this Item")
    path, content_type = found
    return FileResponse(path, media_type=content_type)


async def set_picture(product_id: str, content: bytes) -> ItemOut:
    try:
        product = await items_service.set_picture(product_id, content)
    except items_service.ItemError as exc:
        raise _fail(exc)
    await product.fetch_related("aliases")
    return _out(product, True)


async def remove_picture(product_id: str) -> ItemOut:
    try:
        product = await items_service.remove_picture(product_id)
    except items_service.ItemError as exc:
        raise _fail(exc)
    await product.fetch_related("aliases")
    return _out(product, True)


async def list_attachments(product_id: str) -> list[AttachmentOut]:
    try:
        return [_attachment_out(a) for a in await items_service.list_attachments(product_id)]
    except items_service.ItemError as exc:
        raise _fail(exc)


async def add_attachment(product_id: str, file_name: str, content: bytes, note: str | None, user: User) -> AttachmentOut:
    try:
        return _attachment_out(await items_service.add_attachment(product_id, file_name, content, note, user))
    except items_service.ItemError as exc:
        raise _fail(exc)


async def download_attachment(product_id: str, attachment_id: str) -> FileResponse:
    try:
        attachment = await items_service.get_attachment(product_id, attachment_id)
    except items_service.ItemError as exc:
        raise _fail(exc)
    path = media_service.stored_file(attachment.stored_path)
    if not path:
        raise HTTPException(404, "That file is missing from the server's storage.")
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.file_name)


async def remove_attachment(product_id: str, attachment_id: str) -> None:
    try:
        await items_service.remove_attachment(product_id, attachment_id)
    except items_service.ItemError as exc:
        raise _fail(exc)


async def import_file(filename: str, content: bytes, user: User) -> ImportSummary:
    return await items_service.import_items(filename, content, user)


async def import_aliases_file(filename: str, content: bytes) -> ImportSummary:
    return await items_service.import_aliases(filename, content)


async def export(include_costs: bool = True) -> Response:
    content, name = await items_service.export_xlsx(include_costs)
    return Response(
        content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
