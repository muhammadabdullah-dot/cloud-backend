"""Price history of godown Items: every change to what an Item sells or costs for, one row per value that moved.

Every path that changes an Item's money values writes here the same way: take `snapshot(product)` before
changing it, then `record(product, before, source, reference, user)` after saving. Only values that really
moved are written (compared to the paisa, so an average cost moving by a fraction of a paisa is not a
change). Imports collect `rows_for(...)` across the whole file and save them with `save_rows` in one bulk
insert, so a file of thousands of rows stays fast.

The Item form's Price history tab and the Godown's Price Changes report read these rows. Cost rows are left out
for people who don't see cost (rbac_service.can_see_costs). Rows written before 18 Sep 2026 hold the sale and
retail price together with no `field`; `entries` reads them as the sale price change they were, plus the retail
price when that moved too.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tortoise.expressions import Q

from app.models import Product, ProductPriceChange, User

# The Item's money values that keep a history: column -> what a person calls it. A column the Item doesn't have
# is skipped, so a new price on the Item only needs a line here.
FIELDS: dict[str, str] = {
    "price": "Sale price",
    "rpp": "Retail price (RPP)",
    "wholesale_price": "Wholesale price",
    "pack_price": "Pack price",
    "box_price": "Box price",
    "piece_price": "Piece price",
    "avg_cost": "Average cost",
    "disc_percent": "Item discount %",
    "disc_flat": "Item flat discount",
}
PERCENT_FIELDS = frozenset({"disc_percent"})
COST_FIELDS = frozenset({"avg_cost"})
# What a shelf tag shows: a change to one of these means reprinting it.
SHELF_FIELDS = ("price", "rpp")

# Where a change came from, in the words the screens show. `reference` fills in the paper it came on.
SOURCES: dict[str, str] = {
    "new-item": "New Item",
    "form": "Edited on the Item",
    "import": "Imported from a file",
    "import-new": "Added from a file",
    "receiving": "Received on a GRN",
    "from-branch": "Added from a branch's stock list",
    "by-hand": "Written in by hand while buying",
    "transfer-new": "Added from a branch",
}

_PAISA = Decimal("0.01")
REFERENCE_MAX = 120


def _tracked(product: Product) -> list[str]:
    columns = product._meta.fields_map
    return [f for f in FIELDS if f in columns]


def snapshot(product: Product) -> dict[str, Any]:
    """The Item's money values now, to compare with after a change."""
    return {f: getattr(product, f, None) for f in _tracked(product)}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _paisa(value: Any) -> Decimal | None:
    d = _dec(value)
    return None if d is None else d.quantize(_PAISA, rounding=ROUND_HALF_UP)


def _row(
    product: Product, field: str, old: Any, new: Any, source: str, reference: str | None, user: User | None,
    old_price: Any = None, old_rpp: Any = None,
) -> ProductPriceChange:
    price_now = _dec(product.price) or Decimal("0")
    rpp_now = _dec(getattr(product, "rpp", None))
    return ProductPriceChange(
        product_id=product.id, field=field, old_value=_dec(old), new_value=_dec(new),
        old_price=_dec(old_price) if old_price is not None else price_now, new_price=price_now,
        old_rpp=_dec(old_rpp) if field == "rpp" else rpp_now, new_rpp=rpp_now,
        source=source[:20], reference=(reference or "").strip()[:REFERENCE_MAX] or None,
        changed_by_id=user.id if user else None,
    )


def rows_for(
    product: Product, before: dict[str, Any], source: str, reference: str | None = None, user: User | None = None,
) -> list[ProductPriceChange]:
    """Unsaved rows for every value that moved since `before`. Nothing when nothing moved."""
    out: list[ProductPriceChange] = []
    for field in _tracked(product):
        if field not in before:
            continue
        old, new = before[field], getattr(product, field, None)
        if _paisa(old) == _paisa(new):
            continue
        out.append(_row(
            product, field, old, new, source, reference, user,
            old_price=old if field == "price" else None, old_rpp=old if field == "rpp" else None,
        ))
    return out


def first_price(product: Product, source: str, reference: str | None = None, user: User | None = None) -> ProductPriceChange:
    """A new Item's starting sale price: the first line of its history."""
    return _row(product, "price", None, product.price, source, reference, user, old_price=product.price)


async def save_rows(rows: list[ProductPriceChange]) -> int:
    if rows:
        await ProductPriceChange.bulk_create(rows, batch_size=500)
    return len(rows)


async def record(
    product: Product, before: dict[str, Any], source: str, reference: str | None = None, user: User | None = None,
) -> int:
    """Writes a row for every value that moved since `before`."""
    return await save_rows(rows_for(product, before, source, reference, user))


# ── reading ───────────────────────────────────────────────────────────────────────────────────────


def source_label(source: str, reference: str | None) -> str:
    ref = (reference or "").strip()
    if not ref:
        return SOURCES.get(source, source.replace("-", " ").capitalize())
    return {
        "import": f"Imported from {ref}",
        "import-new": f"Added from {ref}",
        "receiving": f"Received on {ref}",
        "from-branch": f"Added from {ref}'s stock list",
        "by-hand": f"Written in by hand on {ref}",
        "transfer-new": f"Added from {ref}",
    }.get(source, f"{SOURCES.get(source, source)} ({ref})")


def _entry(row: ProductPriceChange, field: str, old: Any, new: Any) -> dict[str, Any]:
    # Shown to the paisa: an average cost is kept to four places, but nobody reads a price that way.
    old_d, new_d = _paisa(old), _paisa(new)
    change = percent = None
    if old_d is not None and new_d is not None:
        change = new_d - old_d
        if field not in PERCENT_FIELDS and old_d != 0:
            percent = (change / old_d * 100).quantize(_PAISA, rounding=ROUND_HALF_UP)
    product = row.product if isinstance(getattr(row, "product", None), Product) else None
    user = row.changed_by if isinstance(getattr(row, "changed_by", None), User) else None
    return {
        "id": f"{row.id}" if row.field else f"{row.id}:{field}",
        "at": row.at,
        "productId": row.product_id,
        "sku": product.sku if product else row.product_id,
        "name": product.name if product else "",
        "department": product.department if product else None,
        "field": field,
        "fieldLabel": FIELDS.get(field, field),
        "unit": "percent" if field in PERCENT_FIELDS else "rupees",
        "oldValue": old_d,
        "newValue": new_d,
        "change": change,
        "changePercent": percent,
        "source": row.source,
        "sourceLabel": source_label(row.source, row.reference),
        "reference": row.reference,
        "changedById": str(row.changed_by_id) if row.changed_by_id else None,
        "changedBy": user.name if user else None,
    }


def entries(row: ProductPriceChange) -> list[dict[str, Any]]:
    """What one row says, in the shape the screens show. An older row (no `field`) is its sale price change, plus
    its retail price change when that moved too."""
    if row.field:
        return [_entry(row, row.field, row.old_value, row.new_value)]
    out = []
    first = row.source in ("new-item", "import-new", "transfer-new", "from-branch", "by-hand")
    if first or _paisa(row.old_price) != _paisa(row.new_price):
        out.append(_entry(row, "price", None if first else row.old_price, row.new_price))
    if not first and _paisa(row.old_rpp) != _paisa(row.new_rpp):
        out.append(_entry(row, "rpp", row.old_rpp, row.new_rpp))
    return out or [_entry(row, "price", row.old_price, row.new_price)]


def _field_filter(field: str) -> Q:
    # Older rows have no field and are sale (and sometimes retail) price changes.
    return Q(field=field) | Q(field__isnull=True) if field in SHELF_FIELDS else Q(field=field)


async def for_item(product_id: str, limit: int = 300, include_costs: bool = True) -> list[dict[str, Any]]:
    """One Item's history, newest first."""
    qs = ProductPriceChange.filter(product_id=product_id)
    if not include_costs:
        qs = qs.filter(Q(field__isnull=True) | ~Q(field__in=list(COST_FIELDS)))
    rows = await qs.order_by("-at").limit(limit).prefetch_related("changed_by", "product")
    return [e for r in rows for e in entries(r)]


async def search(
    from_at: datetime | None = None, to_at: datetime | None = None, department: str | None = None,
    user_id: str | None = None, source: str | None = None, field: str | None = None, q: str | None = None,
    limit: int = 100, offset: int = 0, include_costs: bool = True,
) -> dict[str, Any]:
    """The Price Changes report: every change in a window, newest first, narrowed by department, who, where it came
    from, which value, and an Item's name or code."""
    qs = ProductPriceChange.all()
    if from_at:
        qs = qs.filter(at__gte=from_at)
    if to_at:
        qs = qs.filter(at__lte=to_at)
    if department:
        qs = qs.filter(product__department=department)
    if user_id == "none":
        qs = qs.filter(changed_by_id__isnull=True)
    elif user_id:
        qs = qs.filter(changed_by_id=user_id)
    if source:
        qs = qs.filter(source=source)
    if field:
        qs = qs.filter(_field_filter(field))
    if not include_costs:
        qs = qs.filter(Q(field__isnull=True) | ~Q(field__in=list(COST_FIELDS)))
    if q and q.strip():
        term = q.strip()
        qs = qs.filter(Q(product__name__icontains=term) | Q(product__sku__icontains=term) | Q(product__barcode__icontains=term))
    total = await qs.count()
    rows = await qs.order_by("-at", "id").offset(offset).limit(limit).prefetch_related("changed_by", "product")
    out = [e for r in rows for e in entries(r)]
    if field:
        out = [e for e in out if e["field"] == field]
    return {"rows": out, "total": total}


async def choices(include_costs: bool = True) -> dict[str, Any]:
    """What the report's filters offer: the people who have changed a price, where changes come from, which values."""
    ids = await ProductPriceChange.filter(changed_by_id__isnull=False).distinct().values_list("changed_by_id", flat=True)
    people = await User.filter(id__in=list({str(i) for i in ids})).order_by("name").values("id", "name") if ids else []
    columns = Product._meta.fields_map
    return {
        "people": [{"id": str(p["id"]), "name": p["name"]} for p in people],
        "sources": [{"value": k, "label": v} for k, v in SOURCES.items()],
        "fields": [
            {"value": k, "label": v} for k, v in FIELDS.items()
            if k in columns and (include_costs or k not in COST_FIELDS)
        ],
    }
