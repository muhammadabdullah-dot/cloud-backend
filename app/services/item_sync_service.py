"""Sending an Item, and its price, down to the branches that carry it.

WHY THIS EXISTS AT ALL
----------------------
Until now nothing about an Item travelled from head office to a branch. An Item arrived at a shop by riding on a
transfer line -- `transfers_service._describe_new` makes it from the shipment -- and after that the branch owned
it outright: `_fill_blanks` takes from a later shipment "only what it leaves blank. Nothing set here changes."

That is the right rule for a shipment, which is a record of goods moving and not an instruction about pricing. It
is the wrong rule for the case this file is for: head office deciding a price and meaning it to apply. Before
this, a price changed at head office reached nobody, and somebody had to retype it at every till.

WHAT TRAVELS, AND WHAT DOES NOT
-------------------------------
Only the fields head office actually changed, named one by one in the message. Not the whole Item. A whole-Item
upsert would carry head office's blanks over the branch's own values -- its reorder level, its loose-sale setup,
its PCT code, the pack prices somebody set at the shop -- and the first price change would quietly undo a month
of local work. So the payload is `{"sku": ..., "fields": {...}}` and the branch sets exactly those.

WHO GETS IT
-----------
The branches that carry the Item: the ones holding stock of it, or that have sold it inside the window head
office keeps figures for. Not every branch, because a supermarket catalogue of 68,030 Items against four shops
would otherwise put three useless messages in a queue for every one that matters. `everywhere=True` overrides
that for the case where an Item is genuinely new and head office wants it on every till.

IT IS AN UPSERT, LIKE EVERY OTHER MESSAGE ON THIS ROAD
------------------------------------------------------
`models/downstream.py` makes the rule: every message is an idempotent upsert of whole state, so a branch that
applies one twice is unchanged. Seeing "set the price to 95" twice sets it to 95 twice. A branch that does not
have the Item creates it, because "push this Item to the branches" is the other half of what this is for.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models import Branch, Product
from app.services import downstream_service

# The Item columns head office may decide for a branch, and the name each goes down the wire as. Deliberately a
# short list and not every column on the row: a branch's own stock, its reorder level, its loose-sale setup and
# its PCT code are facts about that shop, and head office has no business overwriting them from here.
SHARED = {
    "name": "name",
    "price": "price",
    "rpp": "rpp",
    "wholesale_price": "wholesalePrice",
    "tax_rate": "taxRate",
    "barcode": "barcode",
    "unit": "unit",
    "pack_unit": "packUnit",
    "pack_size": "packSize",
    "department": "department",
    "category": "category",
    "item_class": "itemClass",
    "subclass": "subclass",
    "manufacturer": "manufacturer",
    "brand": "brand",
    "disc_percent": "discPercent",
    "disc_flat": "discFlat",
    "lock_disc": "lockDisc",
    "active": "active",
}

# What "carries it" means. A branch that sold an Item in the last ninety days is still carrying it even with an
# empty shelf today, which is exactly when a price change matters most: the next delivery arrives priced wrong.
CARRIES_WINDOW_DAYS = 90


def _wire(value: Any) -> Any:
    """JSON cannot hold a Decimal, and a float would lose paisa on the way. Money goes as a string, as it does
    everywhere else in this application, and the branch reads it back with `Decimal(str(...))`."""
    if isinstance(value, Decimal):
        return str(value)
    return value


def changed_fields(product: Product, columns: list[str]) -> dict[str, Any]:
    """The named columns as they now stand, in the wire names, for the ones this road carries."""
    out: dict[str, Any] = {}
    for column in columns:
        key = SHARED.get(column)
        if key:
            out[key] = _wire(getattr(product, column, None))
    return out


async def carrying_branches(sku: str) -> list[str]:
    """Which branches hold this Item or have sold it lately, as branch ids.

    Read from the two rollups head office already keeps, so this costs one query each and needs no new feed:
    `branch_product_stock` is what each branch last reported holding, `branch_product_stats` is what it sold.
    """
    from datetime import timedelta

    from tortoise import Tortoise

    from app.core import dialect
    from app.core.pk_time import today_pk

    conn = Tortoise.get_connection("default")
    since = str(today_pk() - timedelta(days=CARRIES_WINDOW_DAYS))
    mark = dialect.mark("varchar(60)")
    rows = await conn.execute_query_dict(
        f"SELECT DISTINCT branch_id FROM branch_product_stock WHERE product_sku = {mark} "
        f"UNION SELECT DISTINCT branch_id FROM branch_product_stats WHERE product_sku = {mark} AND day > ?",
        [sku, sku, since])
    return [str(r["branch_id"]) for r in rows]


async def publish(product: Product, columns: list[str], *, everywhere: bool = False,
                  only: str | None = None, why: str = "head office") -> int:
    """Send these columns of this Item to the branches that carry it. Returns how many messages were queued.

    Nothing is sent when none of the changed columns is one this road carries, which is the common case: most
    edits on the Item form are to fields that belong to head office alone, and a message saying nothing would
    still cost a branch a pull, an apply and a row in its history.
    """
    fields = changed_fields(product, columns)
    if not fields:
        return 0

    verified = {str(b.id): b for b in await Branch.filter(verified_at__isnull=False)}
    if only is not None:
        targets = [only] if only in verified else []
    elif everywhere:
        targets = list(verified)
    else:
        targets = [b for b in await carrying_branches(product.sku) if b in verified]

    payload = {"sku": product.sku, "name": product.name, "fields": fields, "why": why}
    for branch_id in targets:
        await downstream_service.enqueue(branch_id, "item.upsert", payload)
    return len(targets)


def shared_snapshot(product: Product) -> dict[str, Any]:
    """The columns this road carries, as they stand, to compare with after a save.

    Its own and not `price_history_service.snapshot`, which tracks the nine money fields and nothing else: a
    name, a barcode or a department changed at head office is an Item change that should travel too, and that
    snapshot would never see it.
    """
    return {column: getattr(product, column, None) for column in SHARED}


async def publish_changes(product: Product, before: dict[str, Any], *, why: str = "head office") -> int:
    """Send only what actually moved, given a `shared_snapshot` taken before the save.

    Comparing against the snapshot rather than against the request is what stops a form that re-submits every
    field unchanged from queuing a message to four branches every time somebody opens an Item and presses Save.
    """
    moved = [column for column in SHARED
             if column in before and before[column] != getattr(product, column, None)]
    return await publish(product, moved, why=why)
