"""Empties head office's godown and restocks it with a head-office-only range.

Removes every godown Item, rack, bin, GRN, batch, stock movement, cycle count, requisition and transfer, then
adds the DM SELECT range (own-label packs no branch stocks), new racks, and receives the stock into bins
through the normal receiving path, so average cost, batches and the ledger come out exactly as if the
Warehouse Manager had typed each GRN.

Branch data the Cloud holds (sales, stock snapshots, staff, sync inbox) and suppliers are kept. Number
counters are kept too, so new GRN and transfer numbers never repeat one a branch has already seen.

Run with the Cloud Server stopped, after a backup:
    .venv\\Scripts\\python.exe -m scripts.reset_godown_stock --branch-db ..\\branch-server\\branch.db
"""
import argparse
import asyncio
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import in_transaction

from app.core.config import TORTOISE_ORM
from app.models import Supplier, User
from app.schemas.items import ItemCreate, ItemSupplierIn
from app.schemas.warehouse import GRNCreateRequest, GRNLineIn
from app.services import items_service, racks_service, warehouse_service

CLEAR_ORDER = [
    "transfer_lines", "transfers", "requisitions", "cycle_counts", "warehouse_grn_lines", "warehouse_grns",
    "warehouse_batches", "warehouse_stock_movements", "product_attachments", "product_price_changes",
    "product_suppliers", "product_aliases", "products", "bins", "racks",
]

NEW_SUPPLIERS = [
    ("sup-3", "SUPSRG", "Multan Surgical Supplies", "Faisal Mehmood", "061-4512230"),
    ("sup-4", "SUPAHP", "Al-Hamd Packing House", "Usman Tariq", "0301-6655443"),
]


def ean13(first12: str) -> str:
    """GS1 prefix 21 is for restricted, in-house circulation — it can never clash with a manufacturer's barcode."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return first12 + str((10 - total % 10) % 10)


def code(n: int) -> str:
    return ean13(f"210826{n:06d}")


# (n, name, department, category, class, unit, pack size, price, rpp, supplier)
ITEMS = [
    (1, "DM SELECT SELA RICE 1121 5KG", "GROCERY", "FOOD", "RICE", 4, 2450, 2499, "sup-4"),
    (2, "DM SELECT DAAL CHANA 1KG", "GROCERY", "FOOD", "PULSES", 20, 420, 440, "sup-4"),
    (3, "DM SELECT DAAL MASOOR 1KG", "GROCERY", "FOOD", "PULSES", 20, 380, 399, "sup-4"),
    (4, "DM SELECT KABULI CHANA 1KG", "GROCERY", "FOOD", "PULSES", 20, 480, 499, "sup-4"),
    (5, "DM SELECT BROWN SUGAR 1KG", "GROCERY", "FOOD", "SUGAR", 24, 260, 270, "sup-2"),
    (6, "DM SELECT HIMALAYAN PINK SALT FINE 800G", "GROCERY", "FOOD", "SALT", 24, 120, 130, "sup-4"),
    (7, "DM SELECT KASHMIRI RED CHILLI POWDER 200G", "GROCERY", "FOOD", "SPICES", 48, 340, 350, "sup-4"),
    (8, "DM SELECT HALDI POWDER 200G", "GROCERY", "FOOD", "SPICES", 48, 260, 270, "sup-4"),
    (9, "DM SELECT MIXED DRY FRUIT 500G", "GROCERY", "FOOD", "DRY FRUIT", 12, 2200, 2300, "sup-4"),
    (10, "DM SELECT GREEN TEA JASMINE 25 BAGS", "GROCERY", "BEVERAGES", "TEA", 36, 390, 399, "sup-2"),
    (11, "DM SELECT GARBAGE BAGS LARGE 30PCS", "NON-FOOD", "HOUSEHOLD", "BAGS", 50, 350, 360, "sup-1"),
    (12, "DM SELECT KITCHEN TOWEL 2 ROLLS", "NON-FOOD", "HOUSEHOLD", "TISSUE", 24, 420, 440, "sup-1"),
    (13, "DM SELECT BABY WIPES FRAGRANCE FREE 80PCS", "KIDS CARE", "BABY  ACCESSORIES", "WIPES", 24, 450, 470, "sup-1"),
    (14, "DM SELECT SURGICAL COTTON ROLL 100G", "PHARMACY", "SURGICAL ITEM (PHARMACY)", "DRESSING", 50, 180, 190, "sup-3"),
    (15, "DM SELECT CREPE BANDAGE 4 INCH", "PHARMACY", "SURGICAL ITEM (PHARMACY)", "DRESSING", 100, 150, 160, "sup-3"),
    (16, "DM SELECT LATEX EXAMINATION GLOVES MEDIUM 100PCS", "PHARMACY", "SURGICAL ITEM (PHARMACY)", "GLOVES", 10, 1650, 1700, "sup-3"),
    (17, "DM SELECT 3-PLY SURGICAL FACE MASK 50PCS", "PHARMACY", "SURGICAL ITEM (PHARMACY)", "MASKS", 40, 450, 480, "sup-3"),
    (18, "DM SELECT ALCOHOL PREP PADS 100PCS", "PHARMACY", "SURGICAL ITEM (PHARMACY)", "FIRST AID", 50, 350, 370, "sup-3"),
    (19, "DM SELECT FAMILY FIRST AID KIT", "PHARMACY", "CONSUMER (PHARMACY)", "FIRST AID", 12, 1850, 1950, "sup-3"),
]

# (code, name, zone, levels, bins per level, capacity per bin, priority)
RACKS = [
    ("A", "Rice & pulses", "Dry goods", 4, 8, 150, 1),
    ("B", "Spices, sugar & tea", "Dry goods", 4, 8, 300, 1),
    ("H", "Household & baby", "Household", 3, 6, 250, 2),
    ("P", "Surgical & first aid", "Pharmacy", 5, 10, 400, 1),
]

# (rack, bin, supplier invoice, [(item n, qty, bonus, unit cost, disc %, expiry or None)])
GRNS = [
    ("A", "1-01", "AHP-2609-114", [(1, 96, 4, 1980, 0, "2027-09-30")]),
    ("A", "1-02", "AHP-2609-098", [(1, 40, 0, 1960, 0, "2027-06-30")]),
    ("A", "2-01", "AHP-2609-114", [(2, 140, 0, 345, 0, "2027-05-31")]),
    ("A", "2-02", "AHP-2609-114", [(3, 120, 0, 310, 0, "2027-04-30")]),
    ("A", "2-03", "AHP-2609-114", [(4, 80, 0, 395, 0, "2027-06-30")]),
    ("B", "1-01", "GUD-55871", [(5, 240, 0, 215, 0, "2027-08-31")]),
    ("B", "1-02", "AHP-2609-121", [(6, 264, 24, 88, 0, "2029-01-31")]),
    ("B", "2-01", "AHP-2609-121", [(7, 192, 0, 262, 2, "2027-03-31")]),
    ("B", "2-02", "AHP-2609-121", [(8, 144, 0, 198, 2, "2027-03-31")]),
    ("B", "3-01", "AHP-2609-121", [(9, 60, 0, 1760, 0, "2027-01-31")]),
    ("B", "3-02", "GUD-55871", [(10, 180, 0, 300, 0, "2028-02-29")]),
    ("H", "1-01", "FINO-7730", [(11, 200, 0, 250, 0, None)]),
    ("H", "1-02", "FINO-7730", [(12, 120, 0, 318, 0, None)]),
    ("H", "2-01", "FINO-7730", [(13, 144, 0, 335, 0, "2028-06-30")]),
    ("P", "1-01", "MSS-26-4410", [(14, 300, 0, 128, 0, "2029-12-31")]),
    ("P", "1-02", "MSS-26-4410", [(15, 400, 0, 98, 0, "2030-06-30")]),
    ("P", "2-01", "MSS-26-4410", [(16, 50, 10, 1290, 0, "2028-11-30")]),
    ("P", "2-02", "MSS-26-4410", [(17, 200, 0, 310, 0, "2028-09-30")]),
    ("P", "2-03", "MSS-26-4387", [(17, 80, 0, 305, 0, "2028-03-31")]),
    ("P", "3-01", "MSS-26-4410", [(18, 250, 0, 240, 0, "2028-07-31")]),
    ("P", "3-02", "MSS-26-4410", [(19, 36, 0, 1380, 0, "2028-12-31")]),
]


def branch_taken(branch_db: str) -> set[str]:
    """Every code, barcode, alternate barcode and upper-cased name the branch already has."""
    db = sqlite3.connect(f"file:{branch_db}?mode=ro", uri=True)
    taken = {c for (c,) in db.execute("select sku from products") if c}
    taken |= {c for (c,) in db.execute("select barcode from products") if c}
    taken |= {c for (c,) in db.execute("select code from product_aliases") if c}
    taken |= {("NAME", (n or "").strip().upper()) for (n,) in db.execute("select name from products")}
    db.close()
    return taken


async def main(branch_db: str) -> None:
    taken = branch_taken(branch_db)
    for n, name, *_ in ITEMS:
        if code(n) in taken or ("NAME", name.upper()) in taken:
            raise SystemExit(f"{name} ({code(n)}) is already at the branch — pick another.")
    print(f"checked against the branch: none of the {len(ITEMS)} codes or names exist there")

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        # All on the transaction's own connection: SQLite has one writer, and the base connection would wait on it forever.
        async with in_transaction() as conn:
            for table in CLEAR_ORDER:
                _, rows = await conn.execute_query(f"SELECT COUNT(*) AS n FROM {table}")
                await conn.execute_query(f"DELETE FROM {table}")
                print(f"cleared {table}: {rows[0]['n']}")

        manager = await User.get(email="warehousemanager@cloud.dmarina.pk")
        for sid, scode, sname, contact, phone in NEW_SUPPLIERS:
            if not await Supplier.exists(id=sid) and not await Supplier.exists(code=scode):
                await Supplier.create(id=sid, code=scode, name=sname, contact_person=contact, phone=phone)
                print(f"supplier added: {sname}")

        ids: dict[int, str] = {}
        for n, name, dept, cat, cls, pack, price, rpp, supplier in ITEMS:
            product = await items_service.create(ItemCreate(
                sku=code(n), name=name, price=Decimal(price), rpp=Decimal(rpp), taxRate=Decimal("0"), unit="pc",
                packUnit="ctn", packSize=pack, department=dept, category=cat, itemClass=cls, brand="DM SELECT",
                manufacturer="D.MARINA HEAD OFFICE", origin="local", remarks="Head office own label — godown only until sent to a branch",
            ), user=manager)
            await items_service.replace_suppliers(product.id, [ItemSupplierIn(supplierId=supplier, priority=1)])
            ids[n] = product.id
        print(f"items added: {len(ids)}")

        bins: dict[tuple[str, str], str] = {}
        for rcode, rname, zone, levels, positions, capacity, priority in RACKS:
            rack, made = await racks_service.create_rack({
                "code": rcode, "name": rname, "zone": zone, "levels": levels, "positions": positions,
                "capacityUnits": capacity, "priority": priority,
            })
            print(f"rack {rack.code} ({zone}): {made} bins")
        from app.models import Bin
        for b in await Bin.all():
            bins[(b.rack, b.bin)] = b.id

        supplier_of = {n: s for n, *_, s in ITEMS}
        for rcode, bcode, invoice, lines in GRNS:
            grn = await warehouse_service.receive_grn(manager, GRNCreateRequest(
                supplierId=supplier_of[lines[0][0]], partyInvNo=invoice, binId=bins[(rcode, bcode)], gstMode="normal",
                advanceTax=Decimal("0"), approved=True,
                lines=[GRNLineIn(
                    productId=ids[n], qty=Decimal(qty), bonusQty=Decimal(bonus), unitPrice=Decimal(cost),
                    discPercent=Decimal(disc), taxRate=Decimal("0"),
                    expiry=datetime.fromisoformat(expiry).replace(tzinfo=timezone.utc) if expiry else None,
                ) for n, qty, bonus, cost, disc, expiry in lines],
            ))
            print(f"{grn.grn_number} -> Rack {rcode} Bin {bcode}: " + ", ".join(f"{qty + bonus} x {ITEMS[n - 1][1]}" for n, qty, bonus, *_ in lines))
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch-db", required=True)
    asyncio.run(main(parser.parse_args().branch_db))
