"""The godown's Item master: add and edit one Item, bulk import products and alternate barcodes, export
the whole master, pictures and attachments.

The same rules as the branch's catalog, so an Item behaves the same on both sides: the code, barcode and
every alternate barcode are one namespace (a code that rang up two Items would ring up whichever was
found first), a new code is the next short number, an import only writes the cells a file filled, and
every sale or retail price change is logged.
"""
import asyncio
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from tortoise.expressions import Q
from tortoise.transactions import atomic

from app.core.pk_time import now_pk
from app.models import (
    GRNLine, Product, ProductAlias, ProductAttachment, ProductPriceChange, ProductSupplier, StockMovement, Supplier, User,
)
from app.schemas.import_result import ImportRowError, ImportSummary
from app.schemas.items import ItemAliasIn, ItemCreate, ItemSupplierIn, ItemUpdate
from app.services import media_service, price_history_service
from app.services.import_service import cell_bool, cell_str_any, parse_rows, row_error


class ItemError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


_FIELD_MAP = {
    "taxRate": "tax_rate", "isWeighed": "is_weighed", "packUnit": "pack_unit",
    "packSize": "pack_size", "avgCost": "avg_cost", "itemClass": "item_class",
    "discPercent": "disc_percent", "discFlat": "disc_flat", "lockDisc": "lock_disc",
    "parentId": "parent_id", "parentQty": "parent_qty", "reorderLevel": "reorder_level", "homeBinId": "home_bin_id",
}
_TEXT_FIELDS = {
    "name", "unit", "barcode", "packUnit", "department", "category", "itemClass", "subclass",
    "manufacturer", "brand", "variant", "remarks",
}
_REQUIRED_COLUMNS = {"name", "price", "tax_rate", "is_weighed", "unit", "active", "disc_percent", "disc_flat", "lock_disc"}

SORTS = {
    "sku": "sku", "name": "name", "brand": "brand", "category": "category",
    "price": "price", "taxRate": "tax_rate", "unit": "unit", "avgCost": "avg_cost",
}


def _to_model_fields(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if key == "sku":
            continue
        if key in _TEXT_FIELDS and isinstance(value, str):
            value = value.strip() or None
        out[_FIELD_MAP.get(key, key)] = value
    return out


async def list_all(
    q: str | None, limit: int, offset: int, ids: list[str] | None = None,
    sort: str | None = None, order: str | None = None, supplier_id: str | None = None,
    include_inactive: bool = True, needs_details: bool | None = None,
) -> tuple[list[Product], int]:
    qs = Product.all()
    if ids:
        qs = qs.filter(id__in=ids)
    if needs_details is not None:
        qs = qs.filter(needs_details=needs_details)
    if supplier_id:
        qs = qs.filter(supplier_links__supplier_id=supplier_id).distinct()
    if not include_inactive:
        qs = qs.filter(active=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
    total = await qs.count()
    field = SORTS.get(sort or "name", "name")
    direction = "-" if order == "desc" else ""
    items = await qs.order_by(f"{direction}{field}", "id").offset(offset).limit(limit).prefetch_related("aliases")
    return items, total


async def attachment_counts(product_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pid in await ProductAttachment.filter(product_id__in=product_ids).values_list("product_id", flat=True):
        counts[pid] = counts.get(pid, 0) + 1
    return counts


# ── codes ──────────────────────────────────────────────────────────────────────────────────────

async def code_owner(code: str, except_product_id: str | None = None) -> Product | None:
    code = code.strip()
    for qs in (Product.filter(sku=code), Product.filter(barcode=code)):
        if except_product_id:
            qs = qs.exclude(id=except_product_id)
        hit = await qs.first()
        if hit:
            return hit
    alias_qs = ProductAlias.filter(code=code)
    if except_product_id:
        alias_qs = alias_qs.exclude(product_id=except_product_id)
    alias = await alias_qs.prefetch_related("product").first()
    return alias.product if alias else None


async def _refuse_taken(code: str, what: str, except_product_id: str | None = None) -> None:
    owner = await code_owner(code, except_product_id)
    if owner:
        raise ItemError(f"{what} {code} already belongs to {owner.name} ({owner.sku}).")


async def _next_sku() -> str:
    """One past the highest short numeric code in use; codes of more than seven digits are barcodes."""
    highest = 0
    for sku in await Product.all().values_list("sku", flat=True):
        if sku.isdigit() and len(sku) <= 7:
            highest = max(highest, int(sku))
    candidate = highest + 1
    while await code_owner(str(candidate)):
        candidate += 1
    return str(candidate)


async def _check_parent(product_id: str | None, parent_id: str | None, parent_qty: Decimal | None) -> None:
    if not parent_id:
        return
    if parent_id == product_id:
        raise ItemError("An Item can't be its own parent pack.")
    parent = await Product.get_or_none(id=parent_id)
    if not parent:
        raise ItemError("That parent Item doesn't exist.")
    if not parent_qty:
        raise ItemError(f"Say how many of this Item make one {parent.name}.")
    seen = {parent_id}
    current = parent
    while current.parent_id:
        if current.parent_id == product_id or current.parent_id in seen:
            raise ItemError(f"{parent.name} is already packed inside this Item, which would make a loop.")
        seen.add(current.parent_id)
        current = await Product.get(id=current.parent_id)


# Every price, cost and discount change is logged by price_history_service: a snapshot before, a row per value
# that moved after.


# ── form ───────────────────────────────────────────────────────────────────────────────────────

async def _check_home_bin(bin_id: str | None) -> None:
    if not bin_id:
        return
    from app.models import Bin

    found = await Bin.get_or_none(id=bin_id)
    if not found:
        raise ItemError("That home bin doesn't exist.")
    if not found.active:
        raise ItemError(f"{found.label} is switched off. Pick a bin that's in use as the home bin.")


@atomic()
async def create(data: ItemCreate, user: User | None = None) -> Product:
    sku = (data.sku or "").strip() or await _next_sku()
    await _refuse_taken(sku, "Code")
    await _refuse_switched_off(data.model_dump())
    fields = _to_model_fields(data.model_dump())
    if fields.get("barcode"):
        if fields["barcode"] == sku:
            fields["barcode"] = None
        else:
            await _refuse_taken(fields["barcode"], "Barcode")
    await _check_parent(None, fields.get("parent_id"), fields.get("parent_qty"))
    await _check_home_bin(fields.get("home_bin_id"))
    if not fields.get("parent_id"):
        fields["parent_qty"] = None
    fields["avg_cost"] = fields.get("avg_cost") or Decimal("0")
    product = await Product.create(id=sku, sku=sku, **fields)
    # A new Item's first price starts its price history.
    await price_history_service.save_rows([price_history_service.first_price(product, "new-item", None, user)])
    return product


@atomic()
async def update(product_id: str, data: ItemUpdate, user: User | None = None) -> Product:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise ItemError("Item not found", status=404)
    before = price_history_service.snapshot(product)
    await _refuse_switched_off(data.model_dump(exclude_unset=True), {
        "department": product.department, "category": product.category, "itemClass": product.item_class,
        "subclass": product.subclass, "manufacturer": product.manufacturer, "brand": product.brand,
        "unit": product.unit, "packUnit": product.pack_unit, "taxRate": product.tax_rate,
    })
    changes = _to_model_fields(data.model_dump(exclude_unset=True))
    if changes.get("barcode"):
        if changes["barcode"] == product.sku:
            changes["barcode"] = None
        else:
            await _refuse_taken(changes["barcode"], "Barcode", except_product_id=product.id)
    if changes.get("home_bin_id"):
        await _check_home_bin(changes["home_bin_id"])
    if "parent_id" in changes or "parent_qty" in changes:
        parent_id = changes.get("parent_id", product.parent_id)
        parent_qty = changes.get("parent_qty", product.parent_qty)
        await _check_parent(product.id, parent_id, parent_qty)
        if not parent_id:
            changes["parent_qty"] = None
    for column, value in changes.items():
        if column in _REQUIRED_COLUMNS and value is None:
            continue
        setattr(product, column, value)
    # The whole Item form was saved (it always sends the name), so the details are no longer waiting to be completed.
    if "name" in data.model_dump(exclude_unset=True):
        product.needs_details = False
    await product.save()
    await price_history_service.record(product, before, "form", None, user)
    return product


async def detail(product_id: str) -> dict[str, Any]:
    product = await Product.get_or_none(id=product_id).prefetch_related("aliases")
    if not product:
        raise ItemError("Item not found", status=404)
    rows = await StockMovement.filter(product_id=product.id).values_list("qty", flat=True)
    return {
        "product": product,
        "suppliers": await ProductSupplier.filter(product_id=product.id).prefetch_related("supplier").order_by("priority"),
        "parent": await Product.get_or_none(id=product.parent_id) if product.parent_id else None,
        "children": await Product.filter(parent_id=product.id).order_by("name"),
        "last": await GRNLine.filter(product_id=product.id).order_by("-grn__at").prefetch_related("grn__supplier").first(),
        "stock": sum((Decimal(str(q)) for q in rows), Decimal("0")),
        "prices": await ProductPriceChange.filter(product_id=product.id).order_by("-at").limit(50).prefetch_related("changed_by"),
        "attachments": await ProductAttachment.filter(product_id=product.id).count(),
    }


@atomic()
async def replace_aliases(product_id: str, aliases: list[ItemAliasIn]) -> list[ProductAlias]:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise ItemError("Item not found", status=404)
    seen: set[str] = set()
    for alias in aliases:
        code = alias.code.strip()
        if code in seen:
            raise ItemError(f"Barcode {code} is listed twice.")
        if code in (product.sku, product.barcode):
            raise ItemError(f"{code} is already this Item's own code or barcode.")
        seen.add(code)
        await _refuse_taken(code, "Barcode", except_product_id=product.id)
    await ProductAlias.filter(product_id=product.id).delete()
    return [
        await ProductAlias.create(
            product=product, code=a.code.strip(), remarks=(a.remarks or "").strip() or None,
            qty=a.qty, disc_percent=a.discPercent, disc_flat=a.discFlat,
        )
        for a in aliases
    ]


@atomic()
async def replace_suppliers(product_id: str, links: list[ItemSupplierIn]) -> list[ProductSupplier]:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise ItemError("Item not found", status=404)
    ids = [link.supplierId for link in links]
    if len(set(ids)) != len(ids):
        raise ItemError("A supplier is listed twice.")
    found = {s.id: s for s in await Supplier.filter(id__in=ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ItemError(f"Unknown supplier {missing[0]}")
    await ProductSupplier.filter(product_id=product.id).delete()
    for link in links:
        await ProductSupplier.create(product=product, supplier=found[link.supplierId], priority=link.priority)
    return await ProductSupplier.filter(product_id=product.id).prefetch_related("supplier").order_by("priority")


async def facets() -> dict[str, list[str]]:
    """What the Item form offers. The lists come from Godown > Item Lists (switched-on values only, so a value switched
    off there stops being offered); variants aren't a list, so they are every variant already typed."""
    from app.services import item_lists_service

    values = await Product.filter(variant__isnull=False).distinct().order_by("variant").limit(2000).values_list("variant", flat=True)
    lists = await item_lists_service.choices()
    return {
        "departments": lists["department"], "categories": lists["category"],
        "classes": lists["class"], "subclasses": lists["subclass"],
        "manufacturers": lists["manufacturer"], "brands": lists["brand"],
        "units": lists["unit"], "packUnits": lists["pack-unit"], "variants": [v for v in values if v and v.strip()],
        "taxRates": lists["gst-rate"],
    }


async def _refuse_switched_off(values: dict, before: dict | None = None) -> None:
    from app.services import item_lists_service

    try:
        await item_lists_service.refuse_switched_off(values, before)
    except item_lists_service.ItemListError as exc:
        raise ItemError(exc.message)


async def lookup_by_code(code: str) -> tuple[Product, ProductAlias | None] | None:
    normalized = code.strip()
    product = await Product.get_or_none(sku=normalized) or await Product.get_or_none(barcode=normalized)
    if product:
        return product, None
    alias = await ProductAlias.get_or_none(code=normalized).prefetch_related("product")
    return (alias.product, alias) if alias else None


# ── picture and attachments ────────────────────────────────────────────────────────────────────

async def _get(product_id: str) -> Product:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise ItemError("Item not found", status=404)
    return product


async def set_picture(product_id: str, content: bytes) -> Product:
    product = await _get(product_id)
    try:
        product.picture = media_service.save_picture("items", product.id, content, replacing=product.picture)
    except media_service.MediaError as exc:
        raise ItemError(exc.message)
    await product.save(update_fields=["picture"])
    return product


async def remove_picture(product_id: str) -> Product:
    product = await _get(product_id)
    media_service.remove_file(product.picture)
    product.picture = None
    await product.save(update_fields=["picture"])
    return product


MAX_ATTACHMENTS = 50


async def list_attachments(product_id: str) -> list[ProductAttachment]:
    await _get(product_id)
    return await ProductAttachment.filter(product_id=product_id).prefetch_related("uploaded_by").order_by("-uploaded_at")


async def add_attachment(product_id: str, file_name: str, content: bytes, note: str | None, user: User) -> ProductAttachment:
    product = await _get(product_id)
    if await ProductAttachment.filter(product_id=product.id).count() >= MAX_ATTACHMENTS:
        raise ItemError(f"{product.name} already has {MAX_ATTACHMENTS} attachments. Remove one before adding another.")
    name = (file_name or "file").replace("\\", "/").split("/")[-1].strip()[:200] or "file"
    try:
        stored, content_type = media_service.save_attachment(product.id, name, content)
    except media_service.MediaError as exc:
        raise ItemError(exc.message)
    attachment = await ProductAttachment.create(
        product=product, file_name=name, stored_path=stored, content_type=content_type, size_bytes=len(content),
        note=(note or "").strip()[:255] or None, uploaded_by=user,
    )
    await attachment.fetch_related("uploaded_by")
    return attachment


async def get_attachment(product_id: str, attachment_id: str) -> ProductAttachment:
    attachment = await ProductAttachment.get_or_none(id=attachment_id, product_id=product_id)
    if not attachment:
        raise ItemError("That attachment doesn't exist.", status=404)
    return attachment


async def remove_attachment(product_id: str, attachment_id: str) -> None:
    attachment = await get_attachment(product_id, attachment_id)
    media_service.remove_file(attachment.stored_path)
    await attachment.delete()


# ── import ─────────────────────────────────────────────────────────────────────────────────────

def _num(raw: str | None, label: str) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, but this row has {raw!r}") from None


def _row_to_create(row: dict) -> ItemCreate:
    barcode = cell_str_any(row, "barcode", "BARCODE")
    sku = cell_str_any(row, "sku", "code", "ITEM CODE") or barcode
    name = cell_str_any(row, "name", "ITEM NAME", "Item name")
    price = cell_str_any(row, "price", "SALES PRICE", "SALE PRICE")
    if not sku or not name or price is None:
        raise ValueError("Need at minimum a code or barcode, a name, and a sale price")
    pack_size_raw = cell_str_any(row, "packSize", "PACK SIZE", "Units per pack")
    rpp_raw = cell_str_any(row, "rpp", "RPP", "Retail price")
    avg_cost_raw = cell_str_any(row, "avgCost", "PURCHASE PRICE", "Average cost")
    status = cell_str_any(row, "STATUS", "Active")
    tax_raw = cell_str_any(row, "taxRate", "TAX RATE", "GST %")
    pack_size = _num(pack_size_raw, "Units per pack")
    present = {
        "taxRate": _num(tax_raw, "GST %"),
        "isWeighed": cell_bool(row, "isWeighed") if cell_str_any(row, "isWeighed") else None,
        "unit": cell_str_any(row, "unit"),
        "barcode": barcode,
        "packUnit": cell_str_any(row, "packUnit", "PACK UNIT"),
        "packSize": (int(pack_size) or None) if pack_size is not None else None,
        "avgCost": _num(avg_cost_raw, "Average cost"),
        "rpp": _num(rpp_raw, "Retail price"),
        "department": cell_str_any(row, "department", "DEPARTMENT"),
        "category": cell_str_any(row, "category", "CATEGORY"),
        "itemClass": cell_str_any(row, "itemClass", "CLASS"),
        "subclass": cell_str_any(row, "subclass", "SUBCLASS", "SUB CLASS"),
        "manufacturer": cell_str_any(row, "manufacturer", "MANUFACTURER"),
        "brand": cell_str_any(row, "brand", "BRAND"),
        "variant": cell_str_any(row, "variant"),
        "active": (status.strip().upper() not in ("N", "NO", "FALSE", "0")) if status else None,
    }
    return ItemCreate(sku=sku, name=name, price=_num(price, "Sale price"), **{k: v for k, v in present.items() if v is not None})


async def import_items(filename: str, content: bytes, user: User | None = None) -> ImportSummary:
    rows = parse_rows(filename, content)
    existing_skus = set(await Product.all().values_list("sku", flat=True))
    to_create: list[Product] = []
    to_update: list[tuple[str, dict]] = []
    seen_in_file: set[str] = set()
    errors: list[ImportRowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            data = _row_to_create(row)
            if data.sku in seen_in_file:
                raise ValueError(f"Code {data.sku} is in this file twice")
            seen_in_file.add(data.sku)
            if data.sku in existing_skus:
                fields = _to_model_fields(data.model_dump(exclude_unset=True))
                # A file's cost only seeds a new Item. For one already in the godown, cost comes from what
                # receiving actually paid — an old export imported back must not wind it back.
                fields.pop("avg_cost", None)
                to_update.append((data.sku, fields))
            else:
                fields = _to_model_fields(data.model_dump())
                fields["avg_cost"] = fields.get("avg_cost") or Decimal("0")
                to_create.append(Product(id=data.sku[:60], sku=data.sku, **fields))
        except (ValueError, InvalidOperation, KeyError) as exc:
            errors.append(ImportRowError(row=i, message=row_error(exc)))

    # Price history is collected across the whole file and written in one bulk insert at the end.
    file_name = (filename or "").replace("\\", "/").split("/")[-1] or None
    history = []
    if to_create:
        await Product.bulk_create(to_create, batch_size=500)
        history += [price_history_service.first_price(p, "import-new", file_name, user) for p in to_create]
    for sku, fields in to_update:
        product = await Product.get(sku=sku)
        before = price_history_service.snapshot(product)
        for key, value in fields.items():
            setattr(product, key, value)
        await product.save()
        history += price_history_service.rows_for(product, before, "import", file_name, user)
    await price_history_service.save_rows(history)
    return ImportSummary(created=len(to_create), updated=len(to_update), errors=errors)


async def import_aliases(filename: str, content: bytes) -> ImportSummary:
    """The legacy AliasName / Name / Remarks file. Rows match Items by exact name (or by code when the
    file has one), so import the Items first."""
    rows = parse_rows(filename, content)
    products = await Product.all().values("id", "name", "sku")
    name_to_id = {p["name"].strip().upper(): p["id"] for p in products}
    sku_to_id = {p["sku"]: p["id"] for p in products}
    taken = set(await ProductAlias.all().values_list("code", flat=True)) | set(sku_to_id) | {
        b for b in await Product.filter(barcode__isnull=False).values_list("barcode", flat=True) if b
    }
    to_create: list[ProductAlias] = []
    seen: set[str] = set()
    errors: list[ImportRowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            code = cell_str_any(row, "AliasName", "alias", "alternateBarcode")
            item_code = cell_str_any(row, "itemCode", "sku", "code")
            name = cell_str_any(row, "Name", "name", "Item name")
            if not code or not (name or item_code):
                raise ValueError("Need the alternate barcode (AliasName) and the Item's Name or code")
            if code in seen or code in taken:
                raise ValueError(f"Barcode {code} already rings up an Item")
            product_id = sku_to_id.get(item_code) if item_code else name_to_id.get(name.strip().upper())
            if not product_id:
                raise ValueError(f"No Item {'with code ' + item_code if item_code else 'named ' + repr(name)}. Import the Items first")
            qty_raw = cell_str_any(row, "qty", "Units per scan")
            seen.add(code)
            to_create.append(ProductAlias(
                product_id=product_id, code=code, remarks=cell_str_any(row, "Remarks", "remarks"),
                qty=_num(qty_raw, "qty") or Decimal("1"),
            ))
        except (ValueError, InvalidOperation, KeyError) as exc:
            errors.append(ImportRowError(row=i, message=row_error(exc)))
    if to_create:
        await ProductAlias.bulk_create(to_create, batch_size=500)
    return ImportSummary(created=len(to_create), updated=0, errors=errors)


# ── export ─────────────────────────────────────────────────────────────────────────────────────

Column = tuple[str, Callable[[dict], Any], int]
_EXPORT_FIELDS = (
    "id", "sku", "name", "barcode", "brand", "category", "item_class", "subclass", "department", "manufacturer",
    "unit", "pack_unit", "pack_size", "price", "rpp", "tax_rate", "avg_cost", "disc_percent", "disc_flat",
    "lock_disc", "variant", "origin", "active",
)
_yes = lambda v: "Yes" if v else "No"  # noqa: E731
# Titles match what import reads back, so an exported file can be edited and imported again.
_EXPORT_COLUMNS: list[Column] = [
    ("Code", lambda r: r["sku"], 14), ("Item name", lambda r: r["name"], 42), ("Barcode", lambda r: r["barcode"], 16),
    ("Brand", lambda r: r["brand"], 18), ("Category", lambda r: r["category"], 18), ("Class", lambda r: r["item_class"], 16),
    ("Sub class", lambda r: r["subclass"], 18), ("Department", lambda r: r["department"], 14),
    ("Manufacturer", lambda r: r["manufacturer"], 22), ("Unit", lambda r: r["unit"], 8), ("Pack unit", lambda r: r["pack_unit"], 10),
    ("Units per pack", lambda r: r["pack_size"], 10), ("Sale price", lambda r: r["price"], 11), ("Retail price", lambda r: r["rpp"], 11),
    ("GST %", lambda r: r["tax_rate"], 7), ("Average cost", lambda r: r["avg_cost"], 12), ("Item disc %", lambda r: r["disc_percent"], 9),
    ("Item flat disc", lambda r: r["disc_flat"], 10), ("Lock discount", lambda r: _yes(r["lock_disc"]), 9),
    ("Variant", lambda r: r["variant"], 10), ("Imported / local", lambda r: (r["origin"] or "").title() or None, 10),
    ("Active", lambda r: _yes(r["active"]), 7), ("Alternate barcodes", lambda r: r.get("alias_codes"), 30),
]


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _write_xlsx(rows: list[dict], columns: list[Column]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Items")
    for index, (_, _, width) in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    header = []
    for name, _, _ in columns:
        cell = WriteOnlyCell(sheet, value=name)
        cell.font = Font(bold=True)
        header.append(cell)
    sheet.append(header)
    for row in rows:
        sheet.append([_plain(getter(row)) for _, getter, _ in columns])
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


async def export_xlsx(include_costs: bool = True) -> tuple[bytes, str]:
    """The whole Item master as a workbook, read in chunks and written on a worker thread. Without
    `include_costs` the Average cost column is left out."""
    columns = [c for c in _EXPORT_COLUMNS if include_costs or c[0] != "Average cost"]
    aliases: dict[str, list[str]] = {}
    for product_id, code in await ProductAlias.all().values_list("product_id", "code"):
        aliases.setdefault(product_id, []).append(code)
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = await Product.all().order_by("name", "id").offset(offset).limit(2000).values(*_EXPORT_FIELDS)
        for row in chunk:
            row["alias_codes"] = ", ".join(aliases.get(row["id"], [])) or None
        rows.extend(chunk)
        if len(chunk) < 2000:
            break
        offset += 2000
        await asyncio.sleep(0)
    content = await asyncio.to_thread(_write_xlsx, rows, columns)
    return content, f"godown-items-{now_pk():%Y%m%d-%H%M}.xlsx"


# ── finding any Item the company carries, for buying ──────────────────────────────────────────
#
# The godown's own Item master is short; the branches carry tens of thousands of Items, and head office already holds
# each branch's latest complete stock list. Buying searches both, so an order can be raised for anything the company
# sells. Picking an Item only a branch carries adds it to the godown's Items from that branch's figures; an Item nobody
# carries yet can be written in by hand. Both are marked "details to complete" until someone saves the Item form.

BRANCH_ROWS_PER_SEARCH = 400
_ESC = "\\"


def _search_terms(q: str | None) -> list[str]:
    return [t for t in (q or "").strip().split() if t][:6]


def _escaped(term: str) -> str:
    return term.replace(_ESC, _ESC + _ESC).replace("%", _ESC + "%").replace("_", _ESC + "_")


async def latest_branch_lists() -> list[dict]:
    """Each active branch's latest complete stock list: {branchId, snapshotId, code, name}."""
    from tortoise import Tortoise

    from app.models import Branch

    rows = await Tortoise.get_connection("default").execute_query_dict(
        "SELECT branch_id, snapshot_id, MAX(completed_at) AS done FROM branch_snapshot_runs WHERE status = 'complete' GROUP BY branch_id", [],
    )
    ids = [str(r["branch_id"]) for r in rows]
    branches = {str(b.id): b for b in await Branch.filter(id__in=ids, status="active")} if ids else {}
    return [
        {"branchId": str(r["branch_id"]), "snapshotId": r["snapshot_id"], "code": branches[str(r["branch_id"])].code,
         "name": branches[str(r["branch_id"])].name}
        for r in rows if str(r["branch_id"]) in branches
    ]


async def branch_rows(lists: list[dict], terms: list[str] | None = None, skus: list[str] | None = None, limit: int = BRANCH_ROWS_PER_SEARCH) -> list[dict]:
    """Rows of the branches' latest stock lists matching every search word (in the name or code), or these codes."""
    from tortoise import Tortoise

    if not lists or (not terms and not skus):
        return []
    where = ["(" + " OR ".join("(s.branch_id = ? AND s.snapshot_id = ?)" for _ in lists) + ")"]
    params: list = [v for entry in lists for v in (entry["branchId"], entry["snapshotId"])]
    order = "s.product_name COLLATE NOCASE"
    if skus:
        where.append(f"s.product_sku IN ({','.join('?' * len(skus))})")
        params += skus
    for term in terms or []:
        where.append(f"(s.product_name LIKE ? ESCAPE '{_ESC}' OR s.product_sku LIKE ? ESCAPE '{_ESC}')")
        params += [f"%{_escaped(term)}%", f"%{_escaped(term)}%"]
    tail: list = []
    if terms:
        whole = " ".join(terms)
        order = f"(s.product_sku = ?) DESC, (s.product_name LIKE ? ESCAPE '{_ESC}') DESC, {order}"
        tail = [whole, f"{_escaped(whole)}%"]
    sql = (
        "SELECT s.branch_id, s.product_sku, s.product_name, s.department, s.category, s.brand, s.qty, s.avg_cost, s.price "
        f"FROM branch_product_stock s WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}"
    )
    return await Tortoise.get_connection("default").execute_query_dict(sql, params + tail)


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


async def _codes_in_godown(codes: list[str]) -> dict[str, str]:
    """Which of these codes already ring up a godown Item (as its code, barcode or an alternate barcode): code to Item id."""
    if not codes:
        return {}
    out: dict[str, str] = {}
    for sku, pid in await Product.filter(sku__in=codes).values_list("sku", "id"):
        out[sku] = pid
    for code, pid in await Product.filter(barcode__in=codes).values_list("barcode", "id"):
        out.setdefault(code, pid)
    for code, pid in await ProductAlias.filter(code__in=codes).values_list("code", "product_id"):
        out.setdefault(code, pid)
    return out


def _hint(entry: dict, row: dict) -> dict:
    return {
        "branchCode": entry["code"], "branchName": entry["name"], "qty": _dec(row["qty"]),
        "cost": _dec(row["avg_cost"]), "price": _dec(row["price"]),
    }


async def company_search(q: str | None, limit: int = 30) -> list[dict]:
    """Godown Items and Items only a branch carries, matching every word typed (name, code or barcode).

    Godown Items come first, each with what the branches hold of it; then Items that only branches carry, one per code,
    with each branch's quantity, cost and price as hints. An exact code match leads the list."""
    from tortoise.functions import Sum

    terms = _search_terms(q)
    if not terms:
        return []
    limit = min(max(limit, 1), 60)
    qs = Product.all()
    for term in terms:
        qs = qs.filter(Q(name__icontains=term) | Q(sku__icontains=term) | Q(barcode__icontains=term))
    godown = list(await qs.order_by("name", "id").limit(limit))
    exact = await lookup_by_code(" ".join(terms))
    if exact and exact[0].id not in {p.id for p in godown}:
        godown = [exact[0], *godown][:limit]

    lists = await latest_branch_lists()
    by_branch = {entry["branchId"]: entry for entry in lists}
    rows = await branch_rows(lists, terms=terms)
    taken = await _codes_in_godown(list({r["product_sku"] for r in rows}))

    # What the branches hold of the godown Items found, by their code.
    hints: dict[str, list[dict]] = {}
    godown_codes = [p.sku for p in godown]
    for r in (await branch_rows(lists, skus=godown_codes)) if godown_codes else []:
        entry = by_branch.get(str(r["branch_id"]))
        if entry:
            hints.setdefault(r["product_sku"], []).append(_hint(entry, r))

    on_hand: dict[str, Decimal] = {}
    if godown:
        for row in await StockMovement.filter(product_id__in=[p.id for p in godown]).annotate(total=Sum("qty")).group_by("product_id").values("product_id", "total"):
            on_hand[str(row["product_id"])] = _dec(row["total"])

    out: list[dict] = []
    for p in godown:
        out.append({
            "key": f"g:{p.id}", "source": "godown", "id": p.id, "sku": p.sku, "name": p.name, "unit": p.unit,
            "department": p.department, "category": p.category, "brand": p.brand, "price": p.price, "cost": p.avg_cost,
            "taxRate": p.tax_rate, "packSize": p.pack_size, "active": p.active, "needsDetails": p.needs_details,
            "godownQty": on_hand.get(p.id, Decimal("0")), "branches": hints.get(p.sku, []),
        })

    branch_only: dict[str, dict] = {}
    for r in rows:
        sku = r["product_sku"]
        entry = by_branch.get(str(r["branch_id"]))
        if sku in taken or not entry:
            continue
        item = branch_only.get(sku)
        if item is None:
            if len(branch_only) >= limit:
                continue
            item = branch_only[sku] = {
                "key": f"b:{sku}", "source": "branch", "id": None, "sku": sku, "name": r["product_name"], "unit": None,
                "department": r["department"], "category": r["category"], "brand": r["brand"], "price": _dec(r["price"]),
                "cost": _dec(r["avg_cost"]), "taxRate": None, "packSize": None, "active": True, "needsDetails": False,
                "godownQty": None, "branches": [],
            }
        item["branches"].append(_hint(entry, r))
        if item["cost"] <= 0 < _dec(r["avg_cost"]):
            item["cost"] = _dec(r["avg_cost"])
    out.extend(branch_only.values())
    whole = " ".join(terms)
    out.sort(key=lambda i: 0 if i["sku"] == whole else 1)
    return out


async def _next_company_sku() -> str:
    """The next short number past every one the godown and the branches' stock lists use, so an Item written in at head
    office never takes a code a branch already rings up for something else."""
    from tortoise import Tortoise

    highest = 0
    for sku in await Product.all().values_list("sku", flat=True):
        if sku.isdigit() and len(sku) <= 7:
            highest = max(highest, int(sku))
    lists = await latest_branch_lists()
    if lists:
        pairs = " OR ".join("(branch_id = ? AND snapshot_id = ?)" for _ in lists)
        rows = await Tortoise.get_connection("default").execute_query_dict(
            f"SELECT MAX(CAST(product_sku AS INTEGER)) AS top FROM branch_product_stock WHERE ({pairs}) "
            "AND length(product_sku) BETWEEN 1 AND 7 AND product_sku NOT GLOB '*[^0-9]*'",
            [v for entry in lists for v in (entry["branchId"], entry["snapshotId"])],
        )
        highest = max(highest, int((rows[0]["top"] if rows else 0) or 0))
    candidate = highest + 1
    while await code_owner(str(candidate)):
        candidate += 1
    return str(candidate)


def _best_row(rows: list[tuple[dict, dict]]) -> tuple[dict, dict]:
    """The branch figures to start a godown Item from: one with a cost and a price, then the one holding the most."""
    return max(rows, key=lambda er: (_dec(er[1]["avg_cost"]) > 0, _dec(er[1]["price"]) > 0, _dec(er[1]["qty"])))


def _cut(value: str | None, size: int) -> str | None:
    text = (value or "").strip()
    return text[:size] if text else None


@atomic()
async def add_from_branches(skus: list[str], user: User | None) -> list[tuple[Product, bool]]:
    """Make godown Items of Items only a branch carries, from the branch's stock list figures: code, name, department,
    category, brand, cost and sale price. A stock list doesn't carry the unit or GST, so each is marked "details to
    complete". A code the godown already has is simply returned. Gives (Item, newly added) in the order asked."""
    wanted = list(dict.fromkeys(s.strip() for s in skus if s and s.strip()))
    if not wanted:
        raise ItemError("Pick at least one Item.")
    if len(wanted) > 200:
        raise ItemError("Add at most 200 Items at a time.")
    lists = await latest_branch_lists()
    by_branch = {entry["branchId"]: entry for entry in lists}
    found: dict[str, list[tuple[dict, dict]]] = {}
    for r in await branch_rows(lists, skus=wanted, limit=len(wanted) * max(len(lists), 1) + 10):
        entry = by_branch.get(str(r["branch_id"]))
        if entry:
            found.setdefault(r["product_sku"], []).append((entry, r))
    who = f" by {user.name}" if user else ""
    out: list[tuple[Product, bool]] = []
    for sku in wanted:
        owner = await code_owner(sku)
        if owner:
            out.append((owner, False))
            continue
        if sku not in found:
            raise ItemError(f"No branch's stock list has an Item with code {sku} now. Search again, or write it in by hand.")
        entry, row = _best_row(found[sku])
        product = await Product.create(
            id=sku, sku=sku, name=(row["product_name"] or sku)[:200], price=_dec(row["price"]).quantize(Decimal("0.01")),
            avg_cost=_dec(row["avg_cost"]), unit="pc", tax_rate=Decimal("0"),
            department=_cut(row["department"], 80), category=_cut(row["category"], 80), brand=_cut(row["brand"], 120),
            needs_details=True,
            details_note=f"Taken from {entry['name']}'s stock list{who}. Check the unit and GST, which a stock list doesn't carry."[:200],
        )
        await price_history_service.save_rows([price_history_service.first_price(product, "from-branch", entry["name"], user)])
        out.append((product, True))
    return out


@atomic()
async def add_by_hand(
    name: str, unit: str | None, cost: Decimal, price: Decimal | None, sku: str | None, user: User | None, where: str = "a purchase order",
) -> Product:
    """An Item nobody carries yet, written in while buying: a name, unit, cost and, if known, a sale price and code.
    It can be ordered and received at once and is marked "details to complete" for the Item form."""
    name = " ".join((name or "").split())
    if not name:
        raise ItemError("Write the Item's name.")
    if len(name) > 200:
        raise ItemError("Keep the name under 200 letters.")
    if cost < 0 or (price is not None and price < 0):
        raise ItemError("A price can't be below zero.")
    same = await Product.filter(name__iexact=name).first()
    if same:
        raise ItemError(f"The godown already has {same.name} ({same.sku}). Search for it and pick it instead.")
    code = (sku or "").strip()
    if code:
        if len(code) > 60:
            raise ItemError("Keep the code under 60 characters.")
        await _refuse_taken(code, "Code")
        lists = await latest_branch_lists()
        if await branch_rows(lists, skus=[code], limit=1):
            raise ItemError(f"A branch already uses code {code} for an Item. Search for it, or leave the code blank for a new number.")
    else:
        code = await _next_company_sku()
    missing = "sale price, " if price is None else ""
    product = await Product.create(
        id=code, sku=code, name=name, unit=((unit or "").strip() or "pc")[:30],
        price=(price if price is not None else cost).quantize(Decimal("0.01")), avg_cost=Decimal("0"), tax_rate=Decimal("0"),
        needs_details=True,
        details_note=f"Written in by hand on {where}{f' by {user.name}' if user else ''}. Check the {missing}GST, department and barcode."[:200],
    )
    await price_history_service.save_rows([price_history_service.first_price(product, "by-hand", where, user)])
    return product
