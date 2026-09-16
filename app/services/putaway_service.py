"""Put-away — where stock lives in the godown, and moving it there.

Every Item can have a **home bin**. Receiving puts each line into a bin (the line's own, else the Item's home bin,
else the GRN's), and the first bin an Item is ever received into becomes its home. Stock sitting anywhere else is
on the put-away list until someone moves it home. Picking starts from the home bin and works through the rest by
bin priority, splitting a line across bins rather than pushing one bin below zero.

A move is two ledger rows under one number — out of one bin, into another — so the godown's total never changes
and every bin's history says where its stock went.
"""
from datetime import datetime, timezone
from decimal import Decimal

from tortoise.transactions import atomic

from app.models import Bin, BinMove, Product, StockMovement, User, next_value
from app.services.warehouse_service import all_balances

ZERO = Decimal("0")


class PutAwayError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _num(value) -> Decimal:
    return Decimal(str(value or 0))


async def holdings(product_id: str | None = None) -> dict[str, dict[str, Decimal]]:
    """{product id: {bin id: qty}} for stock above zero (and below zero, which a pick plan must see too)."""
    out: dict[str, dict[str, Decimal]] = {}
    for row in await all_balances():
        if product_id and row["product_id"] != product_id:
            continue
        qty = _num(row["total"])
        if qty != ZERO:
            out.setdefault(row["product_id"], {})[row["bin_id"]] = qty
    return out


async def bin_usage() -> dict[str, Decimal]:
    used: dict[str, Decimal] = {}
    for row in await all_balances():
        qty = _num(row["total"])
        if qty > ZERO:
            used[row["bin_id"]] = used.get(row["bin_id"], ZERO) + qty
    return used


def capacity_warning(bin_: Bin, used: Decimal, adding: Decimal) -> str | None:
    if bin_.capacity_units and used + adding > bin_.capacity_units:
        return f"{bin_.label} will hold {(used + adding).normalize():f} units — over its capacity of {bin_.capacity_units}."
    return None


async def item_bins(product_id: str) -> dict:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise PutAwayError("That Item doesn't exist.", status=404)
    held = (await holdings(product_id)).get(product_id, {})
    bins = {b.id: b for b in await Bin.filter(id__in=list(held) + ([product.home_bin_id] if product.home_bin_id else []))}
    rows = [{"binId": bid, "label": bins[bid].label if bid in bins else bid, "qty": qty, "isHome": bid == product.home_bin_id,
             "active": bins[bid].active if bid in bins else False, "priority": bins[bid].priority if bid in bins else 99}
            for bid, qty in held.items()]
    rows.sort(key=lambda r: (not r["isHome"], r["priority"], r["label"]))
    home = bins.get(product.home_bin_id) if product.home_bin_id else None
    return {"productId": product.id, "sku": product.sku, "name": product.name, "unit": product.unit,
            "homeBinId": product.home_bin_id, "homeBinLabel": home.label if home else None,
            "total": sum((r["qty"] for r in rows), ZERO), "bins": rows}


@atomic()
async def set_home_bin(product_id: str, bin_id: str | None) -> Product:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise PutAwayError("That Item doesn't exist.", status=404)
    if bin_id:
        bin_ = await Bin.get_or_none(id=bin_id)
        if not bin_:
            raise PutAwayError("That bin doesn't exist.")
        if not bin_.active:
            raise PutAwayError(f"{bin_.label} is switched off. Pick a bin that's in use.")
    product.home_bin_id = bin_id or None
    await product.save(update_fields=["home_bin_id"])
    return product


async def _move(user: User | None, product: Product, from_bin: Bin, to_bin: Bin, qty: Decimal, note: str | None, held: Decimal, now: datetime) -> tuple[BinMove, str | None]:
    if qty <= ZERO:
        raise PutAwayError("Move a quantity above zero.")
    if from_bin.id == to_bin.id:
        raise PutAwayError("Pick a different bin to move it to.")
    if not to_bin.active:
        raise PutAwayError(f"{to_bin.label} is switched off and takes no new stock.")
    if qty > held:
        raise PutAwayError(f"{from_bin.label} holds {held.normalize():f} of {product.name} — can't move {qty.normalize():f}.")
    seq = await next_value("bin_move", 1)
    number = f"MV-{seq:05d}"
    for bin_, signed in ((from_bin, -qty), (to_bin, qty)):
        await StockMovement.create(product=product, bin=bin_, kind="move", qty=signed, reason=number, origin_user=user, at=now, unit_cost=product.avg_cost)
    move = await BinMove.create(number=number, product=product, from_bin=from_bin, to_bin=to_bin, qty=qty, note=(note or "").strip()[:255] or None,
                                moved_by=user, moved_by_name=user.name if user else None, at=now)
    warning = capacity_warning(to_bin, (await bin_usage()).get(to_bin.id, ZERO) - qty, qty)
    return move, warning


@atomic()
async def move(user: User, product_id: str, from_bin_id: str, to_bin_id: str, qty: Decimal, note: str | None, make_home: bool = False) -> tuple[BinMove, str | None]:
    product = await Product.get_or_none(id=product_id)
    if not product:
        raise PutAwayError("That Item doesn't exist.", status=404)
    from_bin, to_bin = await Bin.get_or_none(id=from_bin_id), await Bin.get_or_none(id=to_bin_id)
    if not from_bin or not to_bin:
        raise PutAwayError("Pick the bin it comes from and the bin it goes to.")
    held = (await holdings(product_id)).get(product_id, {}).get(from_bin.id, ZERO)
    result = await _move(user, product, from_bin, to_bin, qty, note, held, datetime.now(timezone.utc))
    if make_home and product.home_bin_id != to_bin.id:
        product.home_bin_id = to_bin.id
        await product.save(update_fields=["home_bin_id"])
    return result


@atomic()
async def put_home(user: User, product_id: str) -> list[BinMove]:
    """Everything of an Item sitting outside its home bin, moved home in one go."""
    product = await Product.get_or_none(id=product_id)
    if not product or not product.home_bin_id:
        raise PutAwayError("Give the Item a home bin first.")
    home = await Bin.get(id=product.home_bin_id)
    held = (await holdings(product_id)).get(product_id, {})
    now = datetime.now(timezone.utc)
    moves = []
    for bin_id, qty in held.items():
        if bin_id == home.id or qty <= ZERO:
            continue
        from_bin = await Bin.get(id=bin_id)
        move_, _ = await _move(user, product, from_bin, home, qty, "Put away to the home bin", qty, now)
        moves.append(move_)
    if not moves:
        raise PutAwayError(f"All of {product.name} is already in {home.label}.")
    return moves


async def putaway_list() -> dict:
    """Items with stock outside their home bin, and Items holding stock with no home bin yet."""
    held = await holdings()
    products = {p.id: p for p in await Product.filter(id__in=list(held))}
    bins = {b.id: b for b in await Bin.all()}
    away, homeless = [], []
    for product_id, per_bin in held.items():
        p = products.get(product_id)
        if not p:
            continue
        positive = {b: q for b, q in per_bin.items() if q > ZERO}
        if not positive:
            continue
        where = [{"binId": b, "label": bins[b].label if b in bins else b, "qty": q} for b, q in sorted(positive.items(), key=lambda kv: -kv[1])]
        base = {"productId": p.id, "sku": p.sku, "name": p.name, "unit": p.unit}
        if not p.home_bin_id:
            homeless.append({**base, "bins": where, "total": sum(positive.values(), ZERO)})
            continue
        stray = [w for w in where if w["binId"] != p.home_bin_id]
        if stray:
            home = bins.get(p.home_bin_id)
            away.append({**base, "homeBinId": p.home_bin_id, "homeBinLabel": home.label if home else p.home_bin_id,
                         "homeActive": bool(home and home.active), "inHome": positive.get(p.home_bin_id, ZERO),
                         "elsewhere": stray, "strayQty": sum((w["qty"] for w in stray), ZERO)})
    away.sort(key=lambda r: -r["strayQty"])
    homeless.sort(key=lambda r: r["name"])
    return {"awayFromHome": away, "noHomeBin": homeless}


async def list_moves(product_id: str | None, bin_id: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    from tortoise.expressions import Q

    qs = BinMove.all()
    if product_id:
        qs = qs.filter(product_id=product_id)
    if bin_id:
        qs = qs.filter(Q(from_bin_id=bin_id) | Q(to_bin_id=bin_id))
    total = await qs.count()
    rows = await qs.order_by("-at").offset(offset).limit(limit).prefetch_related("product", "from_bin", "to_bin")
    return [{"id": str(m.id), "number": m.number, "productId": m.product_id, "productName": m.product.name, "sku": m.product.sku,
             "fromBinId": m.from_bin_id, "fromBinLabel": m.from_bin.label, "toBinId": m.to_bin_id, "toBinLabel": m.to_bin.label,
             "qty": m.qty, "note": m.note, "movedBy": m.moved_by_name, "at": m.at} for m in rows], total


async def pick_plan(product_id: str, qty: Decimal, held: dict[str, Decimal] | None = None, bins: dict[str, Bin] | None = None) -> tuple[list[tuple[str, Decimal]], Decimal]:
    """Which bins a line is picked from: the home bin first, then the others by bin priority. Returns
    [(bin id, qty)] and whatever couldn't be found."""
    product = await Product.get_or_none(id=product_id)
    held = held if held is not None else (await holdings(product_id)).get(product_id, {})
    bins = bins if bins is not None else {b.id: b for b in await Bin.filter(id__in=list(held))}
    order = sorted(((b, q) for b, q in held.items() if q > ZERO),
                   key=lambda kv: (kv[0] != (product.home_bin_id if product else None), bins[kv[0]].priority if kv[0] in bins else 99, kv[0]))
    plan, left = [], Decimal(qty)
    for bin_id, available in order:
        if left <= ZERO:
            break
        take = min(available, left)
        plan.append((bin_id, take))
        left -= take
    return plan, left


async def home_bin_for_receipt(product: Product, line_bin_id: str | None, grn_bin_id: str) -> str:
    """The bin a received line goes into: the line's own, else the Item's home bin if it's in use, else the GRN's."""
    if line_bin_id:
        return line_bin_id
    if product.home_bin_id:
        home = await Bin.get_or_none(id=product.home_bin_id)
        if home and home.active:
            return home.id
    return grn_bin_id


async def backfill_home_bins() -> int:
    """Items already in the godown with no home bin get the bin holding most of them (ties go to the bin picked
    first). Runs at startup; an Item that has a home bin is never changed."""
    held = await holdings()
    bins = {b.id: b for b in await Bin.filter(active=True)}
    set_count = 0
    for product in await Product.filter(home_bin_id=None, id__in=list(held)):
        choices = [(bid, qty) for bid, qty in held[product.id].items() if qty > ZERO and bid in bins]
        if not choices:
            continue
        best = sorted(choices, key=lambda kv: (-kv[1], bins[kv[0]].priority, kv[0]))[0][0]
        product.home_bin_id = best
        await product.save(update_fields=["home_bin_id"])
        set_count += 1
    return set_count
