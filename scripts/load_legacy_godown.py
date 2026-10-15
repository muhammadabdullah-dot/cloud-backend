"""Head office's own godown documents, out of Multi-Tec and into the warehouse tables.

    python -m scripts.load_legacy_godown                     what it would write
    python -m scripts.load_legacy_godown --yes                write it
    python -m scripts.load_legacy_godown --yes --no-backup    when a backup was already taken this minute

Reads `staging/payload-HO-godown.json`, written by `ops/legacy-import/map_godown.py`, which explains the filter:
head office's own godown is Multi-Tec's godown 2 HEADOFFICE and 3 NEW HEADOFFICE, and the test is the godown on the
delivery **line**, never `BranchSno` on the header, which says only who typed the document.

    5,968 deliveries  + 92,509 lines  -> warehouse_grns, warehouse_grn_lines
      811 orders      + 16,186 lines  -> warehouse_purchase_orders, warehouse_purchase_order_lines
    6,332 dispatches  + 115,871 lines -> transfers, transfer_lines

WHAT THIS DOES NOT WRITE, AND WHY /warehouse/balances STAYING EMPTY IS NOT A BUG
-------------------------------------------------------------------------------
**No stock movements. No batches. No average cost. No reorder levels.** So after this runs, `/warehouse/grns`,
`/warehouse/purchase-orders`, `/warehouse/transfers` and `/warehouse/purchase-summary` have rows and
`/warehouse/balances`, `/warehouse/low-stock`, `/warehouse/movements` and `/warehouse/batches` are still empty. That
is the intended outcome, and anybody who later reads an empty balances screen as a fault should read this paragraph
first.

The reason is bins. Our godown ledger is bin-addressed: every `StockMovement` names a bin, `/warehouse/putaway`
compares where stock is against where it belongs, and a cycle count counts one bin. Multi-Tec's godown has no bins
at all. `dbo.Godown` is the whole location, there is no table beneath it, and `crosswalk.py` maps `dbo.Godown` to
branch:locations with nothing mapped to bins. Their godown handled 20,521 distinct Items; head office has built 8
bins across 2 racks. Folding their 221,345 ledger rows into ours would have to invent a shelf for every one of those
Items and would then publish a stock figure, an average cost and a reorder suggestion that nobody counted. A
purchase history is a record of what happened; a stock balance is a claim about what is on the shelf right now. The
first can be imported and the second cannot.

So the godown's stock position comes from head office counting its godown, not from here.

THE ONE BIN THESE DOCUMENTS DO NAME
-----------------------------------
`warehouse_grns.bin` cannot be null, so the load makes rack **LEGACY**, bin **UNPLACED**, and every imported
delivery and delivery line points at it. That is the honest answer to "which shelf": we do not know, their software
never recorded one, and the bin says so in its own name rather than a real shelf being picked for it. Stock is
re-binned out of it as it is counted, which is what `/warehouse/putaway` and `/warehouse/moves` are for.

ATTRIBUTION
-----------
The people on these documents are Multi-Tec logins: ZAIN, AWISGT, ASIFPOMT, MOHSIN RAZA. `users` holds six rows,
the sign-ins made for this system, and their 90 people are records in `branch_users` with no sign-in, decided that
way in the migration plan. So `raised_by` and `received_by` are left null and `raised_by_name`, `received_by_name`
and `dispatched_by_name` carry the name their document carried. Same convention as `branch_purchasing.py`: "Names
and codes, not foreign keys ... A name is also what a person reading the document needs."

IDEMPOTENT
----------
Every document is keyed on its own Multi-Tec number, which is unique in its table: `grn_number`, `po_number`,
`transfer_number`. A second run finds each one already there, leaves it alone and writes nothing, so this can be
re-run after a failure without doubling anything. It never deletes a document somebody raised here: only rows with
`source = 'legacy'` are its own, and it touches nothing else.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

STAGING = pathlib.Path(r"D:\MUHAMMAD ABDULLAH\DMARINA\ops\legacy-import\staging")
PAYLOAD = STAGING / "payload-HO-godown.json"

# Rack and bin named so the screen says what it is rather than implying a shelf nobody chose.
LEGACY_RACK = "LEGACY"
LEGACY_BIN = "UNPLACED"
LEGACY_BIN_ID = "bin-legacy-unplaced"


def dec(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def cut(value, limit: int):
    if value is None:
        return None
    return str(value).strip()[:limit] or None


def day_of(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def at_of(value) -> datetime | None:
    text = str(value or "").replace("T", " ")[:19]
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    return None


# A GST rate their software worked out by dividing, where the base it divided by was almost nothing.
IMPOSSIBLE_RATES: list[tuple[str, str]] = []


def rate(value, grn_number: str) -> Decimal:
    """A GST percentage, or zero when their number is not a percentage at all.

    Four of their 282,522 purchase lines carry a GstPerc above 100: 678.0224, 678.1971, 2565.3846 and 12727.2727.
    The last one is on delivery 2005702015 and it is arithmetic, not a rate: the line's tax is 56.00 and its taxable
    base is 0.44, so their software divided one by the other. `tax_rate` is decimal(6,2), which holds 9999.99, and
    the first run of this loader died on that one row after writing 5,968 deliveries and 10,130 of their lines.

    Widening the column to fit a number that is not a rate would be the wrong fix. The purchase summary multiplies
    by this rate to show GST, and 12,727% of anything is a figure nobody should be shown. So a rate above 100 is
    carried as zero and counted, and the count is printed where whoever runs this will read it.
    """
    got = dec(value)
    if got > Decimal("100") or got < Decimal("0"):
        IMPOSSIBLE_RATES.append((grn_number, str(value)))
        return Decimal("0")
    return got


async def legacy_bin():
    """Rack LEGACY, bin UNPLACED: made once, found on every run after."""
    from app.models import Bin, Rack

    rack = await Rack.get_or_none(code=LEGACY_RACK)
    if rack is None:
        rack = await Rack.create(
            code=LEGACY_RACK, name="Imported from Multi-Tec, shelf unknown",
            zone="Re-bin as it is counted", levels=1, positions=1, active=True,
        )
        print(f"   rack     {LEGACY_RACK} made")
    spot = await Bin.get_or_none(id=LEGACY_BIN_ID)
    if spot is None:
        spot = await Bin.create(
            id=LEGACY_BIN_ID, rack=LEGACY_RACK, bin=LEGACY_BIN, level=1, position=1,
            # Priority 99 is picked last, so a counted bin is always preferred over one whose shelf is unknown.
            priority=99, capacity_units=0, active=True,
        )
        print(f"   bin      {spot.label} made")
    else:
        print(f"   bin      {spot.label} already there")
    return spot


async def lookups() -> tuple[dict, dict, dict]:
    """Supplier by their party number, product by their Item number, branch by our code.

    `suppliers.code` IS Multi-Tec's PartySno and `products.sku` IS their ISno, checked rather than assumed: all 379
    suppliers the godown bought from and all 20,521 Items it received resolve, with none missing.
    """
    from app.models import Branch, Product, Supplier

    suppliers = {s[0]: s[1] for s in await Supplier.all().values_list("code", "id")}
    products = {p[0]: p[1] for p in await Product.all().values_list("sku", "id")}
    branches = {b[0]: b[1] for b in await Branch.all().values_list("code", "id")}
    print(f"   lookups  {len(suppliers):,} suppliers, {len(products):,} Items, {len(branches)} branches")
    return suppliers, products, branches


def _same_moment(parsed, stored) -> bool:
    """Is a datetime we just parsed the same moment as the one the database handed back.

    `at_of` returns a naive datetime; Tortoise returns an aware one in UTC. `naive == aware` is False in Python
    however identical the instant, so comparing them directly reports a difference that can never be resolved:
    the header would be rewritten every night with the value it already held, and the log would claim work that
    never needed doing. Both sides are read as UTC here, and only then compared.
    """
    if parsed is None or stored is None:
        return parsed is None and stored is None
    a = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    b = stored if stored.tzinfo else stored.replace(tzinfo=timezone.utc)
    return a == b


async def already(Doc, Line, number_field: str, doc_field: str) -> tuple[dict, dict]:
    """The legacy documents already here, and how many lines each one actually has.

    WHY THE LINE COUNT AND NOT JUST THE NUMBER
    ------------------------------------------
    The first version of this loader decided "already loaded" from the document number alone. Its first run died on
    one bad GST rate after writing 5,968 deliveries and 10,130 of their 92,509 lines, and a second run then skipped
    all 5,968 as done and left 82,379 lines permanently missing. Nothing would have reported that: no constraint
    fires when rows merely fail to arrive, which is the same trap `load_legacy_shop.within()` describes.

    So a document counts as loaded only when it holds every line the payload has for it. One that holds the wrong
    number has its lines cleared and rewritten, which is how a failed run heals itself on the next one.
    """
    numbers = dict(await Doc.filter(source="legacy").values_list(number_field, "id"))
    counts: dict = {}
    ids = list(numbers.values())
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for row in await Line.filter(**{f"{doc_field}_id__in": part}).values(f"{doc_field}_id"):
            key = str(row[f"{doc_field}_id"])
            counts[key] = counts.get(key, 0) + 1
    return numbers, counts


async def _relines(Line, doc_field: str, doc_ids: list) -> int:
    """Clear the lines of documents being rewritten. These are rows this loader wrote in a run that did not
    finish, never anything typed on a screen: only `source = 'legacy'` documents reach here."""
    cleared = 0
    for start in range(0, len(doc_ids), 500):
        part = doc_ids[start:start + 500]
        cleared += await Line.filter(**{f"{doc_field}_id__in": part}).delete() or 0
    return cleared


async def load_orders(rows: list[dict], suppliers: dict, products: dict, write: bool) -> tuple[int, int, int]:
    from app.models import PurchaseOrder, PurchaseOrderLine

    have, counts = await already(PurchaseOrder, PurchaseOrderLine, "po_number", "purchase_order")
    # The header fields of orders already here, so a field this mapper learned to send AFTER they were loaded
    # still reaches them. Without this an order was skipped whole the moment its lines were complete, and the
    # header it was first written with was the header it kept for ever. That is how `closed_at` came to be null
    # on all 863 orders while `approved_at`, which the mapper reads from the SAME column of theirs, was filled on
    # 807: closedAt was added later, and nothing ever went back. Caught by ops/checks/legacy_blank_columns.py,
    # which is the whole reason that check exists.
    heads = {str(o[0]): o for o in await PurchaseOrder.filter(source="legacy").values_list(
        "po_number", "id", "status", "expected_at", "approved_at", "submitted_at", "closed_at", "notes")}
    docs, lines, skipped, mended, refreshed = [], [], 0, [], []
    for r in rows:
        number = cut(r["poNumber"], 20)
        wanted = sum(1 for l in (r.get("lines") or []) if str(l["productSku"]) in products)
        if number in have:
            was = heads.get(number)
            if was is not None:
                now = (r["status"], at_of(r.get("expectedAt")), at_of(r.get("approvedAt")),
                       at_of(r.get("approvedAt")), at_of(r.get("closedAt")), cut(r.get("notes"), 255))
                moved = (now[0] != was[2] or now[5] != was[7]
                         or not all(_same_moment(a, b) for a, b in zip(now[1:5], was[3:7])))
                if moved:
                    refreshed.append(PurchaseOrder(
                        id=was[1], status=now[0], expected_at=now[1], approved_at=now[2],
                        submitted_at=now[3], closed_at=now[4], notes=now[5],
                    ))
            if counts.get(str(have[number]), 0) == wanted:
                continue
            # Header is here, its lines are not all here. Reuse the header and rewrite the lines.
            oid = have[number]
            mended.append(oid)
            for position, l in enumerate(r.get("lines") or [], 1):
                product_id = products.get(str(l["productSku"]))
                if product_id is not None:
                    lines.append(PurchaseOrderLine(
                        id=uuid.uuid4(), purchase_order_id=oid, product_id=product_id, qty=dec(l["qty"]),
                        unit_cost=dec(l["unitCost"]), received_qty=dec(l["receivedQty"]), position=position,
                    ))
            continue
        supplier_id = suppliers.get(r.get("supplierCode"))
        if supplier_id is None:
            skipped += 1
            continue
        oid = uuid.uuid4()
        docs.append(PurchaseOrder(
            id=oid, po_number=number, supplier_id=supplier_id, status=r["status"],
            expected_at=at_of(r.get("expectedAt")), notes=cut(r.get("notes"), 255),
            total=dec(r["total"]), raised_by=None, raised_by_name=cut(r.get("raisedByName"), 120),
            raised_at=at_of(r["at"]), submitted_at=at_of(r.get("approvedAt")),
            approved_at=at_of(r.get("approvedAt")), auto_approved=False,
            closed_at=at_of(r.get("closedAt")), source="legacy",
        ))
        for position, l in enumerate(r.get("lines") or [], 1):
            product_id = products.get(str(l["productSku"]))
            if product_id is None:
                continue
            lines.append(PurchaseOrderLine(
                id=uuid.uuid4(), purchase_order_id=oid, product_id=product_id, qty=dec(l["qty"]),
                unit_cost=dec(l["unitCost"]), received_qty=dec(l["receivedQty"]), position=position,
            ))
    if write:
        if mended:
            await _relines(PurchaseOrderLine, "purchase_order", mended)
        if docs:
            await PurchaseOrder.bulk_create(docs, batch_size=500)
        if refreshed:
            await PurchaseOrder.bulk_update(
                refreshed,
                ["status", "expected_at", "approved_at", "submitted_at", "closed_at", "notes"],
                batch_size=500,
            )
        if lines:
            await PurchaseOrderLine.bulk_create(lines, batch_size=1000)
    if mended:
        print(f"   orders     {len(mended):,} already here with their lines incomplete, lines rewritten")
    if refreshed:
        print(f"   orders     {len(refreshed):,} already here with a header that had moved on"
              f"{', header rewritten' if write else ''}")
    return len(docs), len(lines), skipped


async def load_deliveries(rows: list[dict], suppliers: dict, products: dict, bin_id: str,
                          write: bool) -> tuple[int, int, int]:
    from app.models import GRN, GRNLine, PurchaseOrder

    have, counts = await already(GRN, GRNLine, "grn_number", "grn")
    # A delivery names the order it came against. Only the godown's 811 orders are here, so a delivery quoting a
    # shop's order number links to nothing rather than to the wrong order.
    orders = {o[0]: o[1] for o in await PurchaseOrder.all().values_list("po_number", "id")}
    docs, lines, skipped, mended = [], [], 0, []

    def line_of(l, gid, number):
        product_id = products.get(str(l["productSku"]))
        if product_id is None:
            return None
        return GRNLine(
            id=uuid.uuid4(), grn_id=gid, product_id=product_id, qty=dec(l["qty"]),
            bonus_qty=dec(l["bonusQty"]), unit_price=dec(l["unitPrice"]),
            disc_percent=dec(l["discPercent"]), tax_rate=rate(l["taxRate"], number),
            expiry=at_of(l.get("expiresOn")), bin_id=bin_id,
        )

    for r in rows:
        number = cut(r["grnNumber"], 30)
        wanted = sum(1 for l in (r.get("lines") or []) if str(l["productSku"]) in products)
        if number in have:
            if counts.get(str(have[number]), 0) == wanted:
                continue
            gid = have[number]
            mended.append(gid)
            lines += [x for x in (line_of(l, gid, number) for l in (r.get("lines") or [])) if x is not None]
            continue
        supplier_id = suppliers.get(r.get("supplierCode"))
        if supplier_id is None:
            skipped += 1
            continue
        gid = uuid.uuid4()
        docs.append(GRN(
            id=gid, grn_number=number, supplier_id=supplier_id, party_inv_no=cut(r.get("partyInvNo"), 60),
            bin_id=bin_id, gst_mode=r.get("gstMode") or "normal", advance_tax=dec(r.get("advanceTax")),
            approved=bool(r.get("approved")), received_by=None,
            received_by_name=cut(r.get("receivedByName"), 120),
            purchase_order_id=orders.get(cut(r.get("poNumber"), 20)),
            at=at_of(r["at"]), source="legacy",
        ))
        lines += [x for x in (line_of(l, gid, number) for l in (r.get("lines") or [])) if x is not None]
    if write:
        if mended:
            await _relines(GRNLine, "grn", mended)
        if docs:
            await GRN.bulk_create(docs, batch_size=500)
        if lines:
            await GRNLine.bulk_create(lines, batch_size=1000)
    if mended:
        print(f"   deliveries {len(mended):,} already here with their lines incomplete, lines rewritten")
    return len(docs), len(lines), skipped


async def refresh(Model, key_field: str, rows: list[dict], label: str,
                  fields: dict[str, str], write: bool) -> int:
    """Correct the documents already here from their data, for the fields a mapper fix has changed.

    This pass exists because the loaders above are insert-only on purpose: a second run must not duplicate a
    document, so one already present is skipped and its header is never looked at again. That is right until a
    mapper is corrected, and then every document loaded before the correction keeps the old answer for ever. Three
    of those had happened at once:

        status            read off their `Transfered` flag, 'N' on all 7,963, so no transfer was ever received
        dispatched_at     read off their varchar PostDate, which the date parser returned None for, silently
        received_by_name  their TransferedBy is a USno; it was never joined to a name, and never assigned

    Only rows whose `source` is 'legacy' are touched, and only the fields named by the caller. A value is written
    when it differs from what their system says, because on these rows their system is the authority: nobody
    operates the godown on imported data, so there is no local edit here to protect. It is idempotent, so it can
    run every night and will report nothing once the history is right.
    """
    by_number = {}
    for r in rows:
        number = cut(r[key_field[0]], 30)
        if number:
            by_number[number] = r
    if not by_number:
        return 0
    column = key_field[1]
    changed = 0
    existing = await Model.filter(source="legacy").only("id", column, *fields)
    for row in existing:
        theirs = by_number.get(getattr(row, column))
        if theirs is None:
            continue
        update: list[str] = []
        for ours, (payload_key, kind) in fields.items():
            if payload_key not in theirs:
                continue                      # a mapper that stopped emitting a field must not blank the column
            value = theirs.get(payload_key)
            if kind == "at":
                value = at_of(value)
            elif kind == "text":
                value = cut(value, 120)
            elif kind == "flag":
                value = bool(value)           # a flag column is NOT NULL, so False rather than None
            if value is None and kind != "flag":
                continue                      # never blank something out; this pass only ever adds or corrects
            if getattr(row, ours) != value:
                setattr(row, ours, value)
                update.append(ours)
        if update:
            changed += 1
            if write:
                await row.save(update_fields=update)
    if changed:
        print(f"   {label:<10} {changed:,} already here, corrected from their data")
    return changed


async def load_dispatches(rows: list[dict], products: dict, branches: dict, write: bool) -> tuple[int, int, int]:
    from app.models import Transfer, TransferLine

    have, counts = await already(Transfer, TransferLine, "transfer_number", "transfer")
    docs, lines, skipped, mended = [], [], 0, []

    def line_of(l, tid):
        product_id = products.get(str(l["productSku"]))
        if product_id is None:
            return None
        return TransferLine(
            id=uuid.uuid4(), transfer_id=tid, product_id=product_id, qty_sent=dec(l["qtySent"]),
            # Left null on purpose. Their data records that the far end took the shipment in but never what it
            # counted, and "nobody wrote down what arrived" is not the same claim as "all of it arrived".
            qty_received=None, unit_cost=dec(l["unitCost"]),
        )

    for r in rows:
        number = cut(r["transferNumber"], 30)
        wanted = sum(1 for l in (r.get("lines") or []) if str(l["productSku"]) in products)
        if number in have:
            if counts.get(str(have[number]), 0) == wanted:
                continue
            tid = have[number]
            mended.append(tid)
            lines += [x for x in (line_of(l, tid) for l in (r.get("lines") or [])) if x is not None]
            continue
        branch_id = branches.get(r.get("toBranch"))
        if branch_id is None:
            skipped += 1
            continue
        tid = uuid.uuid4()
        when = at_of(r["at"])
        docs.append(Transfer(
            id=tid, transfer_number=number, branch_id=branch_id,
            # Null: it left the godown, which is what a null source_branch means on this model.
            source_branch=None, notes=cut(r.get("notes"), 255),
            dispatched_by_name=cut(r.get("dispatchedByName"), 120), status=r["status"],
            requested_at=when, approved_at=when, dispatched_at=at_of(r.get("dispatchedAt")),
            # Who signed the stock in. The model has carried this column all along and nothing filled it, so the
            # Transfer screen asked "received by" of 6,393 transfers and answered blank every time, while their
            # TransferedBy held the name on every one.
            received_at=at_of(r.get("receivedAt")), received_by_name=cut(r.get("receivedByName"), 120),
            source="legacy",
        ))
        lines += [x for x in (line_of(l, tid) for l in (r.get("lines") or [])) if x is not None]
    if write:
        if mended:
            await _relines(TransferLine, "transfer", mended)
        if docs:
            await Transfer.bulk_create(docs, batch_size=500)
        if lines:
            await TransferLine.bulk_create(lines, batch_size=1000)
    if mended:
        print(f"   dispatches {len(mended):,} already here with their lines incomplete, lines rewritten")
    return len(docs), len(lines), skipped


async def main() -> int:
    ap = argparse.ArgumentParser(description="head office's own godown documents from Multi-Tec")
    ap.add_argument("--yes", action="store_true", help="write it")
    ap.add_argument("--payload", default=str(PAYLOAD))
    args = ap.parse_args()

    path = pathlib.Path(args.payload)
    if not path.exists():
        print(f"No payload at {path}. Run ops/legacy-import/map_godown.py first.")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"payload: {path.name}  godowns {data.get('godowns')}  "
          f"{len(data.get('orders') or []):,} orders, {len(data.get('grns') or []):,} deliveries, "
          f"{len(data.get('transfers') or []):,} dispatches")

    await Tortoise.init(config=TORTOISE_ORM)
    write = args.yes
    print("writing" if write else "dry run, nothing is written")
    spot = await legacy_bin() if write else None
    bin_id = spot.id if spot else LEGACY_BIN_ID
    suppliers, products, branches = await lookups()

    made_o, lines_o, skip_o = await load_orders(data.get("orders") or [], suppliers, products, write)
    print(f"   orders     {made_o:,} written, {lines_o:,} lines" + (f", {skip_o:,} skipped, no supplier" if skip_o else ""))
    made_g, lines_g, skip_g = await load_deliveries(data.get("grns") or [], suppliers, products, bin_id, write)
    print(f"   deliveries {made_g:,} written, {lines_g:,} lines" + (f", {skip_g:,} skipped, no supplier" if skip_g else ""))
    made_t, lines_t, skip_t = await load_dispatches(data.get("transfers") or [], products, branches, write)
    print(f"   dispatches {made_t:,} written, {lines_t:,} lines" + (f", {skip_t:,} skipped, no branch" if skip_t else ""))
    # The documents already here, brought up to date with their data. Insert-only loading is right for not
    # duplicating a document and wrong for ever correcting one, so every mapper fix needs this pass or it only
    # ever applies to documents nobody had loaded yet.
    from app.models import GRN as _GRN, PurchaseOrder as _PO, Transfer as _T
    await refresh(_T, ("transferNumber", "transfer_number"), data.get("transfers") or [], "dispatches", {
        "status": ("status", "plain"),
        "dispatched_at": ("dispatchedAt", "at"),
        "received_at": ("receivedAt", "at"),
        "received_by_name": ("receivedByName", "text"),
        "dispatched_by_name": ("dispatchedByName", "text"),
    }, write)
    await refresh(_PO, ("poNumber", "po_number"), data.get("orders") or [], "orders", {
        "approved_at": ("approvedAt", "at"),
    }, write)
    await refresh(_GRN, ("grnNumber", "grn_number"), data.get("grns") or [], "deliveries", {
        "approved": ("approved", "flag"),
    }, write)

    if IMPOSSIBLE_RATES:
        print(f"   {len(IMPOSSIBLE_RATES)} delivery line(s) carried a GST rate above 100 and were loaded at 0: "
              + ", ".join(f"{n} at {v}%" for n, v in IMPOSSIBLE_RATES[:4]))

    if write:
        from app.models import GRN, GRNLine, PurchaseOrder, PurchaseOrderLine, StockMovement, Transfer, TransferLine
        print("\nin the database now:")
        for label, model in (("warehouse_purchase_orders", PurchaseOrder), ("warehouse_purchase_order_lines", PurchaseOrderLine),
                             ("warehouse_grns", GRN), ("warehouse_grn_lines", GRNLine),
                             ("transfers", Transfer), ("transfer_lines", TransferLine),
                             ("warehouse_stock_movements", StockMovement)):
            print(f"   {label:32} {await model.all().count():>9,}")
        print("\n   warehouse_stock_movements is 0 on purpose. The godown's stock position comes from a count,")
        print("   not from their ledger. See this script's own docstring.")
    else:
        print("\nAdd --yes to write it.")
    await Tortoise.close_connections()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
