"""Godown > Item Lists: the departments, categories, classes, sub-classes, manufacturers, brands, units, pack units and
GST rates the godown's Items are sorted by. The same rules as a branch's Item Lists (branch server
services/masters_service.py), so a list behaves the same wherever someone keeps it.

See models/masters.py for why the value stays as text on the Item. Every value already on an Item is on its list (the
list catches up whenever it is read), renaming changes every Item at once, renaming onto a value already on the list
merges the two, switching off keeps the value on the Items that have it and stops the Item form offering it, and only a
value nothing uses can be deleted.

These lists are head office's own. A branch keeps its own lists: an Item sent to a branch carries its text, and the
branch's list picks the value up the first time it sees it.
"""
import re
from decimal import Decimal, InvalidOperation

from tortoise import Tortoise
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.models import User
from app.models.masters import ItemListEntry


class ItemListError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


# kind -> (column on products, longest value the column holds, what one is called)
ITEM_LISTS: dict[str, tuple[str, int, str]] = {
    "department": ("department", 80, "department"),
    "category": ("category", 80, "category"),
    "class": ("item_class", 80, "class"),
    "subclass": ("subclass", 80, "sub-class"),
    "manufacturer": ("manufacturer", 120, "manufacturer"),
    "brand": ("brand", 120, "brand"),
    "unit": ("unit", 30, "unit"),
    "pack-unit": ("pack_unit", 30, "pack unit"),
    "gst-rate": ("tax_rate", 6, "GST rate"),
}
# The Item form's fields (as the API names them), by the list each one picks from.
ITEM_FIELD_KINDS: dict[str, str] = {
    "department": "department", "category": "category", "itemClass": "class", "subclass": "subclass",
    "manufacturer": "manufacturer", "brand": "brand", "unit": "unit", "packUnit": "pack-unit", "taxRate": "gst-rate",
}
# What a list is called in an import file, besides its own key.
_KIND_NAMES = {
    "departments": "department", "categories": "category", "classes": "class", "sub-class": "subclass", "sub-classes": "subclass",
    "subclasses": "subclass", "sub class": "subclass", "manufacturers": "manufacturer", "brands": "brand", "units": "unit",
    "pack units": "pack-unit", "pack unit": "pack-unit", "pack-units": "pack-unit", "gst rate": "gst-rate", "gst rates": "gst-rate",
    "gst": "gst-rate",
}


def item_kind(kind: str) -> str:
    key = (kind or "").strip().lower()
    key = _KIND_NAMES.get(key, key)
    if key not in ITEM_LISTS:
        raise ItemListError(f"There's no list called {kind}.")
    return key


def rate_code(value) -> str:
    """17, 17.0 and '17.00' are the same rate. Written the short way: 17, 7.5, 0."""
    try:
        rate = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ItemListError("A GST rate is a number from 0 to 100.")
    if rate < 0 or rate > 100:
        raise ItemListError("A GST rate is a number from 0 to 100.")
    text = format(rate.normalize(), "f")
    return "0" if text in ("-0", "0.00") else text


def shown(kind: str, code: str) -> str:
    return f"{code}%" if kind == "gst-rate" else code


def _clean_value(kind: str, name: str) -> str:
    _, longest, label = ITEM_LISTS[kind]
    if kind == "gst-rate":
        return rate_code(str(name).strip().rstrip("%").strip())
    value = re.sub(r"\s+", " ", (name or "").strip())
    if not value:
        raise ItemListError(f"Type the {label}.")
    if len(value) > longest:
        raise ItemListError(f"A {label} can be at most {longest} characters.")
    return value


async def _used_values(kind: str) -> dict[str, int]:
    column = ITEM_LISTS[kind][0]
    conn = Tortoise.get_connection("default")
    if kind == "gst-rate":
        rows = await conn.execute_query_dict(f"SELECT CAST({column} AS REAL) AS v, COUNT(*) AS n FROM products GROUP BY CAST({column} AS REAL)")
        out: dict[str, int] = {}
        for row in rows:
            if row["v"] is None:
                continue
            code = rate_code(row["v"])
            out[code] = out.get(code, 0) + int(row["n"])
        return out
    rows = await conn.execute_query_dict(
        f"SELECT {column} AS v, COUNT(*) AS n FROM products WHERE {column} IS NOT NULL AND TRIM({column}) != '' GROUP BY {column}"
    )
    return {row["v"]: int(row["n"]) for row in rows}


async def _uses_of(kind: str, code: str) -> int:
    column = ITEM_LISTS[kind][0]
    conn = Tortoise.get_connection("default")
    if kind == "gst-rate":
        rows = await conn.execute_query_dict(f"SELECT COUNT(*) AS n FROM products WHERE CAST({column} AS REAL) = ?", [float(code)])
    else:
        rows = await conn.execute_query_dict(f"SELECT COUNT(*) AS n FROM products WHERE {column} = ?", [code])
    return int(rows[0]["n"]) if rows else 0


async def _add_missing(kind: str, codes) -> None:
    known = set(await ItemListEntry.filter(kind=kind).values_list("code", flat=True))
    missing = [code for code in codes if code not in known]
    if not missing:
        return
    try:
        await ItemListEntry.bulk_create([ItemListEntry(kind=kind, code=c, active=True) for c in missing], batch_size=500)
    except IntegrityError:
        # Two screens opened the list at the same moment; add what the other didn't.
        for code in missing:
            if not await ItemListEntry.exists(kind=kind, code=code):
                try:
                    await ItemListEntry.create(kind=kind, code=code, active=True)
                except IntegrityError:
                    pass


async def sync_list(kind: str) -> dict[str, int]:
    """Every value written on an Item is on its list, whether it came from the Item form, an import, or was there before
    lists existed. Returns how many Items use each value."""
    used = await _used_values(kind)
    await _add_missing(kind, used.keys())
    return used


async def entries(kind: str) -> list[tuple[ItemListEntry, int]]:
    kind = item_kind(kind)
    used = await sync_list(kind)
    rows = await ItemListEntry.filter(kind=kind).order_by("code")
    return [(entry, used.get(entry.code, 0)) for entry in rows]


async def summary() -> list[dict]:
    """How long each list is, for the tabs."""
    out = []
    for kind in ITEM_LISTS:
        await sync_list(kind)
        total = await ItemListEntry.filter(kind=kind).count()
        off = await ItemListEntry.filter(kind=kind, active=False).count()
        out.append({"kind": kind, "total": total, "switchedOff": off})
    return out


async def choices() -> dict[str, list[str]]:
    """What the Item form offers: switched-on values only."""
    out: dict[str, list[str]] = {}
    for kind in ITEM_LISTS:
        await sync_list(kind)
        codes = await ItemListEntry.filter(kind=kind, active=True).values_list("code", flat=True)
        out[kind] = sorted(codes, key=Decimal) if kind == "gst-rate" else sorted(codes, key=str.lower)
    return out


async def _same_entry(kind: str, code: str, except_id=None) -> ItemListEntry | None:
    qs = ItemListEntry.filter(kind=kind, code=code) if kind == "gst-rate" else ItemListEntry.filter(kind=kind, code__iexact=code)
    if except_id is not None:
        qs = qs.exclude(id=except_id)
    return await qs.first()


async def add_entry(kind: str, name: str, user: User) -> ItemListEntry:
    kind = item_kind(kind)
    code = _clean_value(kind, name)
    await sync_list(kind)
    clash = await _same_entry(kind, code)
    if clash:
        raise ItemListError(f"The {ITEM_LISTS[kind][2]} {shown(kind, clash.code)} is already on the list.")
    return await ItemListEntry.create(kind=kind, code=code, active=True, updated_by_name=user.name)


def _stored_rate(code: str) -> str:
    return format(Decimal(code).quantize(Decimal("0.01")), "f")


async def _move_items(conn, kind: str, old: str, new: str) -> int:
    column = ITEM_LISTS[kind][0]
    if kind == "gst-rate":
        count = await conn.execute_query_dict(f"SELECT COUNT(*) AS n FROM products WHERE CAST({column} AS REAL) = ?", [float(old)])
        await conn.execute_query(f"UPDATE products SET {column} = ? WHERE CAST({column} AS REAL) = ?", [_stored_rate(new), float(old)])
    else:
        count = await conn.execute_query_dict(f"SELECT COUNT(*) AS n FROM products WHERE {column} = ?", [old])
        await conn.execute_query(f"UPDATE products SET {column} = ? WHERE {column} = ?", [new, old])
    return int(count[0]["n"]) if count else 0


async def update_entry(entry_id: str, name: str | None, active: bool | None, merge: bool, user: User) -> tuple[ItemListEntry, int, int]:
    """Rename (every Item using it changes too), merge into another entry, or switch on or off.
    Returns the entry, how many Items were changed, and how many use it now."""
    entry = await ItemListEntry.get_or_none(id=entry_id)
    if not entry or entry.kind not in ITEM_LISTS:
        raise ItemListError("That list entry doesn't exist.", 404)
    kind, label = entry.kind, ITEM_LISTS[entry.kind][2]
    moved = 0
    if name is not None:
        new_code = _clean_value(kind, name)
        if new_code != entry.code:
            other = await _same_entry(kind, new_code, except_id=entry.id)
            if other and not merge:
                raise ItemListError(
                    f"The {label} {shown(kind, other.code)} is already on the list. Merge the two to move every Item "
                    f"from {shown(kind, entry.code)} onto {shown(kind, other.code)}.", 409,
                )
            async with in_transaction() as conn:
                if other:
                    moved = await _move_items(conn, kind, entry.code, other.code)
                    other.active = (other.active or entry.active) if active is None else active
                    other.updated_by_name = user.name
                    await other.save(using_db=conn)
                    await entry.delete(using_db=conn)
                    entry = other
                else:
                    moved = await _move_items(conn, kind, entry.code, new_code)
                    entry.code = new_code
                    entry.updated_by_name = user.name
                    await entry.save(using_db=conn)
    if active is not None and active != entry.active:
        entry.active = active
        entry.updated_by_name = user.name
        await entry.save()
    return entry, moved, await _uses_of(kind, entry.code)


async def delete_entry(entry_id: str) -> None:
    entry = await ItemListEntry.get_or_none(id=entry_id)
    if not entry or entry.kind not in ITEM_LISTS:
        raise ItemListError("That list entry doesn't exist.", 404)
    uses = await _uses_of(entry.kind, entry.code)
    if uses:
        raise ItemListError(
            f"{uses:,} Item{'' if uses == 1 else 's'} still use {shown(entry.kind, entry.code)}. Switch it off instead, "
            f"or rename it into another {ITEM_LISTS[entry.kind][2]}."
        )
    await entry.delete()


async def refuse_switched_off(values: dict, before: dict | None = None) -> None:
    """The Item form can't put a switched-off value on an Item. `values` uses the form's names (brand, itemClass,
    taxRate). A value the Item already had is left alone, so saving an old Item doesn't fail over a brand switched off
    since. Imports aren't checked: a legacy file is loaded as it is, and anything new it brings shows up on the lists."""
    for field, kind in ITEM_FIELD_KINDS.items():
        if field not in values:
            continue
        value = values[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            code = rate_code(value) if kind == "gst-rate" else str(value).strip()
        except ItemListError:
            continue
        if before is not None and before.get(field) is not None:
            try:
                was = rate_code(before[field]) if kind == "gst-rate" else str(before[field]).strip()
            except ItemListError:
                was = None
            if was == code:
                continue
        if await ItemListEntry.exists(kind=kind, code=code, active=False):
            raise ItemListError(
                f"The {ITEM_LISTS[kind][2]} {shown(kind, code)} is switched off in Item Lists. Pick another, or switch it back on there."
            )


async def import_lists(rows: list[dict], user: User) -> tuple[int, int, list[tuple[int, str]]]:
    """Columns: List (Department, Brand, GST rate...), Name, and optionally Active (yes or no)."""
    from app.services.import_service import cell_str_any

    created = updated = 0
    errors: list[tuple[int, str]] = []
    synced: set[str] = set()
    for i, row in enumerate(rows, start=2):
        try:
            kind = item_kind(cell_str_any(row, "list", "LIST", "kind", "KIND") or "")
            if kind not in synced:
                await sync_list(kind)
                synced.add(kind)
            code = _clean_value(kind, cell_str_any(row, "name", "NAME", "value", "VALUE") or "")
            active_raw = (cell_str_any(row, "active", "ACTIVE", "switched on") or "").strip().lower()
            active = None if not active_raw else active_raw in ("yes", "y", "true", "1", "on", "active")
            entry = await _same_entry(kind, code)
            if entry is None:
                await ItemListEntry.create(kind=kind, code=code, active=True if active is None else active, updated_by_name=user.name)
                created += 1
            elif active is not None and entry.active != active:
                entry.active = active
                entry.updated_by_name = user.name
                await entry.save()
                updated += 1
        except ItemListError as exc:
            errors.append((i, exc.message))
    return created, updated, errors
