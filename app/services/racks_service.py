"""Racks and bins — the godown's floor plan, and how full each bin is.

A rack is `levels` shelves high and `positions` bins along each shelf; adding one generates its bins,
named level-position ("2-07" is level 2, seventh along). A rack can grow but not shrink — a bin that has
held stock has history — so bins that aren't wanted are switched off instead. Nothing that holds stock
can be switched off.
"""
import re
import uuid
from decimal import Decimal

from tortoise.transactions import atomic

from app.models import Bin, Product, Rack
from app.services.warehouse_service import all_balances

ZERO = Decimal("0")
MAX_LEVELS = 30
MAX_POSITIONS = 100


class RackError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _bin_code(level: int, position: int) -> str:
    return f"{level}-{position:02d}"


async def backfill_racks() -> int:
    """Bins that came from before racks were records (the seeded A, B and C) get a rack each, laid out
    on one shelf in their existing order. Runs at startup; does nothing once every bin has a rack."""
    created = 0
    codes = sorted(set(await Bin.all().values_list("rack", flat=True)))
    for code in codes:
        rack = await Rack.get_or_none(code=code)
        unplaced = await Bin.filter(rack=code, level__isnull=True).order_by("bin")
        if rack is None:
            count = await Bin.filter(rack=code).count()
            rack = await Rack.create(code=code, levels=1, positions=max(count, 1))
            created += 1
        if unplaced:
            taken = {
                (b.level, b.position) for b in await Bin.filter(rack=code, level__isnull=False)
            }
            slot = 1
            for bin_ in unplaced:
                while (1, slot) in taken:
                    slot += 1
                bin_.level, bin_.position = 1, slot
                taken.add((1, slot))
                await bin_.save(update_fields=["level", "position"])
            if slot > rack.positions:
                rack.positions = slot
                await rack.save(update_fields=["positions"])
    return created


async def _occupancy(product_id: str | None) -> tuple[dict[str, dict], dict[str, Decimal]]:
    per_bin: dict[str, dict] = {}
    product_qty: dict[str, Decimal] = {}
    for row in await all_balances():
        total = Decimal(str(row["total"]))
        entry = per_bin.setdefault(row["bin_id"], {"used": ZERO, "items": 0})
        if total > ZERO:
            entry["used"] += total
            entry["items"] += 1
        if product_id and row["product_id"] == product_id and total != ZERO:
            product_qty[row["bin_id"]] = total
    return per_bin, product_qty


async def home_counts() -> dict[str, int]:
    """How many Items call each bin home, stock or no stock."""
    counts: dict[str, int] = {}
    for bin_id in await Product.filter(home_bin_id__isnull=False).values_list("home_bin_id", flat=True):
        counts[bin_id] = counts.get(bin_id, 0) + 1
    return counts


async def layout(product_id: str | None = None) -> tuple[list[Rack], dict[str, list[Bin]], dict[str, dict], dict[str, Decimal]]:
    racks = await Rack.all().order_by("code")
    bins: dict[str, list[Bin]] = {}
    for b in await Bin.all().order_by("rack", "level", "position"):
        bins.setdefault(b.rack, []).append(b)
    per_bin, product_qty = await _occupancy(product_id)
    for bin_id, count in (await home_counts()).items():
        per_bin.setdefault(bin_id, {"used": ZERO, "items": 0})["homes"] = count
    return racks, bins, per_bin, product_qty


def _check_size(levels: int, positions: int) -> None:
    if not 1 <= levels <= MAX_LEVELS:
        raise RackError(f"A rack can be 1 to {MAX_LEVELS} levels high.")
    if not 1 <= positions <= MAX_POSITIONS:
        raise RackError(f"A rack can have 1 to {MAX_POSITIONS} bins on each level.")


async def _add_bins(rack: Rack, levels: range, positions: range, capacity: int, priority: int) -> int:
    existing = {(b.level, b.position) for b in await Bin.filter(rack=rack.code)}
    made = 0
    for level in levels:
        for position in positions:
            if (level, position) in existing:
                continue
            await Bin.create(
                id=f"bin-{uuid.uuid4().hex[:10]}", rack=rack.code, bin=_bin_code(level, position),
                level=level, position=position, capacity_units=capacity, priority=priority, active=rack.active,
            )
            made += 1
    return made


@atomic()
async def create_rack(data: dict) -> tuple[Rack, int]:
    code = (data.get("code") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,9}", code):
        raise RackError("A rack code is 1 to 10 letters, numbers or dashes — e.g. A, C2, COLD-1.")
    if await Rack.exists(code=code) or await Bin.exists(rack=code):
        raise RackError(f"There's already a rack {code}.")
    levels, positions = int(data.get("levels") or 1), int(data.get("positions") or 1)
    _check_size(levels, positions)
    capacity = int(data.get("capacityUnits") or 0)
    priority = int(data.get("priority") or 1)
    if capacity < 0:
        raise RackError("Capacity can't be negative.")
    if not 1 <= priority <= 99:
        raise RackError("Priority is 1 (picked first) to 99.")
    rack = await Rack.create(
        code=code, name=(data.get("name") or "").strip() or None, zone=(data.get("zone") or "").strip() or None,
        levels=levels, positions=positions, active=True,
    )
    made = await _add_bins(rack, range(1, levels + 1), range(1, positions + 1), capacity, priority)
    return rack, made


async def _holding(bin_ids: list[str]) -> dict[str, Decimal]:
    held: dict[str, Decimal] = {}
    wanted = set(bin_ids)
    for row in await all_balances():
        if row["bin_id"] in wanted and Decimal(str(row["total"])) > ZERO:
            held[row["bin_id"]] = held.get(row["bin_id"], ZERO) + Decimal(str(row["total"]))
    return held


@atomic()
async def update_rack(rack_id: str, data: dict) -> tuple[Rack, int]:
    rack = await Rack.get_or_none(id=rack_id)
    if not rack:
        raise RackError("That rack doesn't exist.", status=404)
    if "name" in data:
        rack.name = (data["name"] or "").strip() or None
    if "zone" in data:
        rack.zone = (data["zone"] or "").strip() or None
    made = 0
    levels = int(data.get("levels") or rack.levels)
    positions = int(data.get("positions") or rack.positions)
    if levels < rack.levels or positions < rack.positions:
        raise RackError("A rack can grow but not shrink — its bins have history. Switch off the bins you don't need instead.")
    _check_size(levels, positions)
    if levels != rack.levels or positions != rack.positions:
        sample = await Bin.filter(rack=rack.code).order_by("level", "position").first()
        capacity = int(data.get("capacityUnits") if data.get("capacityUnits") is not None else (sample.capacity_units if sample else 0))
        priority = sample.priority if sample else 1
        rack.levels, rack.positions = levels, positions
        await rack.save()
        made = await _add_bins(rack, range(1, levels + 1), range(1, positions + 1), capacity, priority)
    if "active" in data and data["active"] is not None and bool(data["active"]) != rack.active:
        bins = await Bin.filter(rack=rack.code)
        if not data["active"]:
            held = await _holding([b.id for b in bins])
            if held:
                raise RackError(
                    f"Rack {rack.code} still holds stock in {len(held)} bin{'s' if len(held) != 1 else ''} "
                    f"({sum(held.values()).normalize():f} units). Move it out before switching the rack off."
                )
        rack.active = bool(data["active"])
        await Bin.filter(rack=rack.code).update(active=rack.active)
    await rack.save()
    return rack, made


@atomic()
async def update_bin(bin_id: str, data: dict) -> Bin:
    found = await Bin.get_or_none(id=bin_id)
    if not found:
        raise RackError("That bin doesn't exist.", status=404)
    if data.get("capacityUnits") is not None:
        if int(data["capacityUnits"]) < 0:
            raise RackError("Capacity can't be negative.")
        found.capacity_units = int(data["capacityUnits"])
    if data.get("priority") is not None:
        if not 1 <= int(data["priority"]) <= 99:
            raise RackError("Priority is 1 (picked first) to 99.")
        found.priority = int(data["priority"])
    if data.get("active") is not None and bool(data["active"]) != found.active:
        if not data["active"]:
            held = await _holding([found.id])
            if held:
                raise RackError(f"{found.label} still holds {held[found.id].normalize():f} units. Move them out before switching it off.")
        else:
            rack = await Rack.get_or_none(code=found.rack)
            if rack and not rack.active:
                raise RackError(f"Rack {rack.code} is switched off. Switch the rack on first.")
        found.active = bool(data["active"])
    await found.save()
    return found


async def contents(bin_id: str) -> tuple[Bin, list[dict], list[dict]]:
    """What the bin holds, and the Items that live in it (their home bin), whether or not any stock is there yet."""
    found = await Bin.get_or_none(id=bin_id)
    if not found:
        raise RackError("That bin doesn't exist.", status=404)
    rows = [r for r in await all_balances() if r["bin_id"] == found.id and Decimal(str(r["total"])) != ZERO]
    held = {r["product_id"]: Decimal(str(r["total"])) for r in rows}
    residents = await Product.filter(home_bin_id=found.id).order_by("name")
    products = {p.id: p for p in await Product.filter(id__in=list(held))}
    products.update({p.id: p for p in residents})
    bin_labels = {b.id: b.label for b in await Bin.filter(id__in=[p.home_bin_id for p in products.values() if p.home_bin_id])}
    out = []
    for product_id, qty in held.items():
        p = products.get(product_id)
        out.append({
            "productId": product_id, "name": p.name if p else product_id, "sku": p.sku if p else None,
            "unit": p.unit if p else None, "qty": qty,
            "isHome": bool(p and p.home_bin_id == found.id),
            "homeBinId": p.home_bin_id if p else None, "homeBinLabel": bin_labels.get(p.home_bin_id) if p and p.home_bin_id else None,
        })
    out.sort(key=lambda x: -x["qty"])
    homes = [{"productId": p.id, "name": p.name, "sku": p.sku, "unit": p.unit, "qty": held.get(p.id, ZERO), "category": p.category, "brand": p.brand}
             for p in residents]
    return found, out, homes


async def suggest_bin(product_id: str | None, category: str | None, brand: str | None) -> dict | None:
    """A bin for a new Item: in the rack where most Items of the same category (else brand) live, the first bin on the
    lowest shelf that nothing lives in and holds no stock. With nothing to go on, the emptiest free bin in the godown."""
    live_bins = {b.id: b for b in await Bin.filter(active=True)}
    if not live_bins:
        return None
    homes = await home_counts()
    per_bin, _ = await _occupancy(None)
    free = [b for b in live_bins.values() if not homes.get(b.id) and per_bin.get(b.id, {}).get("used", ZERO) <= ZERO]
    reason = None
    rack_code = None
    for field, value in (("category", category), ("brand", brand)):
        value = (value or "").strip()
        if not value:
            continue
        qs = Product.filter(**{f"{field}__iexact": value}, home_bin_id__isnull=False)
        if product_id:
            qs = qs.exclude(id=product_id)
        racks: dict[str, int] = {}
        for bin_id in await qs.values_list("home_bin_id", flat=True):
            b = live_bins.get(bin_id)
            if b:
                racks[b.rack] = racks.get(b.rack, 0) + 1
        if racks:
            rack_code, count = max(racks.items(), key=lambda kv: (kv[1], -ord(kv[0][0])))
            reason = f"Rack {rack_code} is where {count} other {value} Item{'s' if count != 1 else ''} live"
            break
    pool = [b for b in free if b.rack == rack_code] if rack_code else []
    if not pool:
        pool = free
        if rack_code:
            reason = f"{reason}, but it has no free bin, so this is the next free bin in the godown"
        else:
            reason = "The first free bin in the godown"
    if not pool:
        return None
    pick = sorted(pool, key=lambda b: (b.rack, b.level or 1, b.position or 1))[0]
    return {"binId": pick.id, "label": pick.label, "rack": pick.rack, "level": pick.level, "position": pick.position, "reason": reason}


async def zones() -> list[str]:
    return sorted({z for z in await Rack.filter(zone__isnull=False).values_list("zone", flat=True) if z})
