"""Land one Multi-Tec shop's whole history at head office, through head office's own code.

    python -m scripts.load_legacy_shop --shop GT                      dry run: every refusal, and what it would write
    python -m scripts.load_legacy_shop --shop GT --yes                 writes it
    python -m scripts.load_legacy_shop --shop GT --yes --replace       it has been loaded before
    python -m scripts.load_legacy_shop --shop GT --yes --only figures,books

Reads what the map stages left in `ops/legacy-import/staging/`. Nothing here knows anything about their schema: by
this point it is our own payload shapes and our own tables.

WHAT IT WRITES, AND WHERE
-------------------------
    masters     the whole catalogue through items_service.apply_item_rows, so our own rules, numbering and price
                history apply. Suppliers straight into `suppliers`, deliberately NOT through supplier_service.upsert,
                because that publishes every supplier to every verified branch and would push 534 legacy suppliers
                into live branches for no gain.
    roster      the shop's staff into `branch_users`. NOT a sign-in: nobody arrives with a password of theirs and
                nobody is given one, which is why that table exists rather than `branch_staff`.
    figures     snapshot_service.apply_aggregates, the same call a real branch's two-hourly push makes
    stock       begin_stock / add_stock_chunk / complete_stock, in 2,000-row chunks
    documents   branch_documents_service.replace_window, a month at a time
    books       `acc_groups`, `acc_accounts`, `acc_vouchers`, `acc_voucher_lines`, with `book` set to the shop, so
                each shop's books are its own and readable one shop at a time
    buying      `branch_parties`, `branch_purchase_orders`, `branch_grns`, `branch_purchase_returns` and their lines
    movements   `branch_stock_movements`, a month at a time

WHY IN PROCESS AND NOT OVER HTTP
--------------------------------
`registration_service.authenticate` rejects a branch whose `verified_at` is NULL, and verifying a legacy shop would
mint it a sync secret and make it indistinguishable from a real branch with a server behind it. So the shop stays
unverified, this runs in process, and nothing is ever sent down to it.

EVERY REFUSAL HAPPENS BEFORE ANY WRITE
--------------------------------------
  * a shop whose Branch row is verified, unless --force. A real branch's history comes from its own server.
  * a shop that already has figures, unless --replace.
  * no backup of the database, unless --no-backup. A full load rewrites a shop's whole history.
  * an unbalanced voucher is written as a draft whatever their software said, so a trial balance cannot be wrong
    without somebody being able to see why.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

from tortoise import Tortoise
from tortoise.transactions import in_transaction

from app.core.config import TORTOISE_ORM

HERE = pathlib.Path(__file__).resolve().parent
STAGING = HERE.parent.parent.parent / "ops" / "legacy-import" / "staging"
STAGES = ("masters", "roster", "figures", "stock", "documents", "books", "chart", "buying", "movements")


def payload(shop: str, kind: str, required: bool = True) -> dict:
    path = STAGING / f"payload-{shop}-{kind}.json"
    if not path.exists():
        if required:
            raise SystemExit(f"No {kind} payload for {shop} at {path}.\n"
                             f"From ops/legacy-import:  .venv\\Scripts\\python.exe map_{kind}.py --shop {shop}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def csv_rows(name: str) -> list[dict]:
    path = STAGING / f"payload-{name}.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def dec(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def cut(value, limit: int):
    """A string trimmed to what the column takes, or None.

    Their fields hold more than ours declare: one of their party phone columns holds three numbers separated by
    commas, 35 characters against our 30. Trimming at the boundary is right because the boundary is where the
    width is known, and a load that dies on the 900th account of 1,330 has written 899 and told nobody.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def day_of(value) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def at_of(value) -> datetime:
    text = str(value or "").replace("T", " ")[:19]
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    return datetime(1970, 1, 1)


def masked(url: str) -> str:
    """A connection string safe to print. Everything between the user and the host is the password.

    This existed nowhere and the password went into a log in clear text, from a line whose whole purpose was to say
    which database was about to be written to. Saying which database does not require saying the password.
    """
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"********", url)


def which_database() -> str:
    from app.core.config import settings

    url = str(getattr(settings, "db_url", "") or TORTOISE_ORM["connections"]["default"])
    if "sqlite" in url:
        path = pathlib.Path(url.split("://", 1)[1].lstrip("/"))
        if not path.exists():
            path = pathlib.Path(url.split("://", 1)[1])
        size = f"{path.stat().st_size/1e6:,.0f} MB" if path.exists() else "does not exist yet"
        return f"{path}  ({size})"
    return masked(url)


def back_up(label: str) -> pathlib.Path | None:
    from app.core.config import settings

    url = str(getattr(settings, "db_url", "") or TORTOISE_ORM["connections"]["default"])
    if "sqlite" not in url:
        print("   not SQLite, so no file copy is taken here. Back it up your own way first.")
        return None
    live = pathlib.Path(url.split("://", 1)[1].lstrip("/"))
    if not live.exists():
        live = pathlib.Path(url.split("://", 1)[1])
    target = live.with_name(f"{live.name}.bak-before-{label}-{datetime.now():%Y%m%d-%H%M%S}")
    conn = sqlite3.connect(str(live), timeout=120)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()
    print(f"   backup  {target.name}  ({target.stat().st_size/1e6:,.0f} MB)")
    return target


# ── the catalogue and the company lists ───────────────────────────────────────────────────────────
async def load_masters(shop: str) -> None:
    from app.models import Supplier, User
    from app.services import item_lists_service, items_service

    items = csv_rows("ALL-items") or csv_rows(f"{shop}-items")
    aliases = csv_rows("ALL-aliases") or csv_rows(f"{shop}-aliases")
    suppliers = csv_rows("ALL-suppliers") or csv_rows(f"{shop}-suppliers")
    user = await User.filter(role_id="system-admin", active=True).first() or await User.all().first()

    if items:
        # A barcode already owned by a different product here cannot be written: ours keeps sku, barcode and alias in
        # one namespace and the column is unique. The mapper cannot see this side, so the decision is taken here and
        # counted, and the Item still loads without that one barcode.
        from app.models import Product, ProductAlias

        taken: dict[str, str] = {}
        for row in await Product.all().values("sku", "barcode"):
            if row["barcode"]:
                taken[str(row["barcode"]).strip()] = str(row["sku"])
            taken.setdefault(str(row["sku"]).strip(), str(row["sku"]))
        for row in await ProductAlias.all().values("code", "product_id"):
            taken.setdefault(str(row["code"]).strip(), "alias")
        clashed = 0
        for row in items:
            code = (row.get("barcode") or "").strip()
            if code and taken.get(code) not in (None, str(row.get("sku") or "").strip()):
                row["barcode"] = ""
                clashed += 1
        if clashed:
            print(f"   items    {clashed:,} barcode(s) already belong to something else here and were left off. "
                  f"The Items still load.")
        summary = await items_service.apply_item_rows(items, user, reference="Multi-Tec import")
        print(f"   items    {getattr(summary, 'created', 0):,} added, {getattr(summary, 'updated', 0):,} updated, "
              f"{len(getattr(summary, 'errors', []) or []):,} refused")
        for problem in (getattr(summary, "errors", []) or [])[:5]:
            print(f"               {problem}")
    if aliases:
        # Ours keeps sku, barcode and alias in ONE namespace: `items_service.code_owner` is the rule and it is right,
        # because a scanned code has to mean exactly one thing at a till. Theirs does not, so an alias code that is
        # already an Item's code, or another Item's barcode, cannot be written and is dropped with a count.
        from app.models import Product, ProductAlias

        owner: dict[str, str] = {}
        for row in await Product.all().values("sku", "barcode"):
            owner[str(row["sku"]).strip()] = str(row["sku"])
            if row["barcode"]:
                owner.setdefault(str(row["barcode"]).strip(), str(row["sku"]))
        skus = {str(row["sku"]).strip() for row in await Product.all().values("sku")}
        already = {str(row["code"]).strip() for row in await ProductAlias.all().values("code")}
        rows, taken, unknown_sku, seen = [], 0, 0, set()
        for a in aliases:
            sku, code = (a.get("sku") or "").strip(), (a.get("code") or "").strip()
            if not sku or not code:
                continue
            if sku not in skus:
                unknown_sku += 1
                continue
            if code in already or code in seen or owner.get(code) not in (None, sku):
                taken += 1
                continue
            seen.add(code)
            # `alias` is the alternate barcode and `sku` is the Item. apply_alias_rows reads a bare `code` as
            # the Item's code, so naming it that left every row with no barcode to file.
            rows.append({"sku": sku, "alias": code, "qty": "1"})
        summary = await items_service.apply_alias_rows(rows)
        problems = getattr(summary, "errors", []) or []
        print(f"   aliases  {getattr(summary, 'created', 0):,} added, {getattr(summary, 'updated', 0):,} updated, "
              f"{len(problems):,} refused, {taken:,} whose code already means something else here, "
              f"{unknown_sku:,} for an Item that did not load")
        for problem in problems[:3]:
            print(f"               {problem}")

    added = updated = 0
    for row in suppliers:
        code = (row.get("code") or "").strip().upper()
        if not code or not (row.get("name") or "").strip():
            continue
        fields = dict(
            name=row["name"].strip()[:160], phone=(row.get("phone") or "").strip()[:40] or None,
            address=(row.get("address") or "").strip()[:255] or None,
            city=(row.get("city") or "").strip()[:120] or None,
            ntn=(row.get("ntn") or "").strip()[:40] or None,
            s_tax_reg_no=(row.get("salesTaxNo") or "").strip()[:40] or None,
            active=(row.get("active") or "yes").lower() in ("yes", "y", "1", "true"),
            origin="legacy",
        )
        existing = await Supplier.get_or_none(code=code)
        if existing:
            for key, value in fields.items():
                setattr(existing, key, value)
            await existing.save()
            updated += 1
        else:
            # Supplier.id is a CharField primary key, formed the way supplier_service.py forms it.
            await Supplier.create(id=f"sup-{uuid.uuid4().hex[:8]}", code=code,
                                  company_id=uuid.uuid4().hex, **fields)
            added += 1
    # Straight into the table, not through supplier_service.upsert: that publishes to every verified branch and
    # would push these into live branches, and it raises a question per near-miss name, which 534 of theirs would
    # turn into 534 questions nobody asked for.
    print(f"   suppliers {added:,} added, {updated:,} updated (written here, not published to any branch)")
    for kind in ("department", "category", "brand", "manufacturer"):
        try:
            await item_lists_service.sync_list(kind)
        except Exception:  # noqa: BLE001: a list that cannot catch up is not worth losing the load over
            pass


async def load_roster(branch, rows: list[dict], whole: bool = True) -> None:
    """The shop's people.

    A roster is a list of who works there, not a stream of dated events, so it cannot be windowed the way bills can.
    On a whole load it is replaced. On a windowed one it is **merged**: the people the window mentions are added or
    updated and nobody is removed.

    That distinction is the whole point of `whole`. Replacing on a windowed run would delete every person the shop
    has ever had and put back only those who happened to touch a document in the last two days.
    """
    from app.models import BranchUser

    if not rows:
        return
    if whole:
        await BranchUser.filter(branch=branch).delete()
        await BranchUser.bulk_create([
            BranchUser(branch=branch, code=str(r["code"])[:40], name=str(r["name"])[:140],
                       role_name=(r.get("roleName") or None), active=bool(r.get("active")),
                       last_active_on=day_of(r.get("lastActiveOn")), can_sign_in=False, source="legacy")
            for r in rows
        ], batch_size=500)
        print(f"   roster   {len(rows):,} people, none of whom can sign in")
        return

    # Merged, keyed on (branch, code), which is the table's own unique rule. `can_sign_in` is never touched here:
    # it is the one field a person might have been granted by hand, and a sync has no business revoking it.
    have = {u.code: u for u in await BranchUser.filter(branch=branch)}
    made, changed = [], 0
    for r in rows:
        code = str(r["code"])[:40]
        fields = dict(name=str(r["name"])[:140], role_name=(r.get("roleName") or None),
                      active=bool(r.get("active")), last_active_on=day_of(r.get("lastActiveOn")), source="legacy")
        existing = have.get(code)
        if existing is None:
            made.append(BranchUser(branch=branch, code=code, can_sign_in=False, **fields))
            continue
        if any(getattr(existing, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(existing, k, v)
            await existing.save(update_fields=list(fields))
            changed += 1
    if made:
        await BranchUser.bulk_create(made, batch_size=500)
    print(f"   roster   {len(made):,} added, {changed:,} updated, {len(have):,} already there and kept")


# ── documents, in bulk ────────────────────────────────────────────────────────────────────────────
async def load_documents_bulk(branch, start: date, end: date, sales: list[dict], returns: list[dict],
                              fbr: list[dict]) -> dict:
    """A window of documents written with bulk_create instead of one transaction per bill.

    Same columns, same window-replace meaning, about twenty times the speed. `replace_window` already clears the day
    range before writing, so nothing inside the window can exist to be upserted onto, which is what makes a plain
    insert correct here rather than merely faster.
    """
    from app.models import (BranchFbrInvoice, BranchFbrInvoiceLine, BranchReturnLine, BranchReturnRecord,
                            BranchSale, BranchSaleLine, BranchSaleTender)

    def inside(rows):
        keep, out = [], 0
        for row in rows:
            day = day_of(row.get("day")) or at_of(row.get("at")).date()
            if start <= day <= end:
                keep.append((row, day))
            else:
                out += 1
        return keep, out

    sales_in, out_sales = inside(sales)
    returns_in, out_returns = inside(returns)
    fbr_in, out_fbr = inside(fbr)

    async with in_transaction():
        await BranchSale.filter(branch=branch, day__gte=start, day__lte=end).delete()
        await BranchReturnRecord.filter(branch=branch, day__gte=start, day__lte=end).delete()
        await BranchFbrInvoice.filter(branch=branch, day__gte=start, day__lte=end).delete()

    # The children are deleted explicitly: SQLite honours ON DELETE CASCADE only with `PRAGMA foreign_keys` on, and
    # this connection has it off, so a cascade cannot be relied on to have taken the lines with their document.
    async with in_transaction() as db:
        for table, parent in (("branch_sale_lines", "branch_sales"), ("branch_sale_tenders", "branch_sales"),
                              ("branch_return_lines", "branch_return_records"),
                              ("branch_fbr_invoice_lines", "branch_fbr_invoices")):
            column = {"branch_sale_lines": "sale_id", "branch_sale_tenders": "sale_id",
                      "branch_return_lines": "return_record_id",
                      "branch_fbr_invoice_lines": "invoice_id"}[table]
            await db.execute_query(
                f'DELETE FROM "{table}" WHERE "{column}" NOT IN (SELECT "id" FROM "{parent}")')

    made = {"sales": 0, "lines": 0, "tenders": 0, "returns": 0, "returnLines": 0, "fbr": 0, "fbrLines": 0,
            "skippedOutsideWindow": out_sales + out_returns + out_fbr, "noNumber": 0}

    docs, lines, tenders = [], [], []
    seen: set[str] = set()
    for row, day in sales_in:
        number = cut(row.get("invoiceNumber"), 30)
        if not number or number in seen:
            made["noNumber"] += 1
            continue
        seen.add(number)
        sale = BranchSale(
            id=uuid.uuid4(), branch=branch, invoice_number=number, at=at_of(row.get("at")), day=day,
            cashier_name=cut(row.get("cashierName"), 120), party_name=cut(row.get("partyName"), 160),
            party_code=cut(row.get("partyCode"), 40),
            till_session_number=cut(row.get("tillSessionNumber"), 20),
            counter_name=cut(row.get("counterName"), 80),
            gross=dec(row.get("gross")), disc_total=dec(row.get("discTotal")), fare=dec(row.get("fare")),
            gst=dec(row.get("gst")), grand_total=dec(row.get("grandTotal")),
            net_value=dec(row.get("netValue")), received=dec(row.get("received")),
            cash_back=dec(row.get("cashBack")), cogs=dec(row.get("cogs")),
            is_credit_sale=bool(row.get("isCreditSale")), voided=bool(row.get("voided")),
            fbr_invoice_number=cut(row.get("fbrInvoiceNumber"), 60),
            member_code=cut(row.get("memberCode"), 40), earned_points=int(row.get("earnedPoints") or 0),
            source="legacy")
        docs.append(sale)
        numbers_used: set[int] = set()
        for i, l in enumerate(row.get("lines") or [], 1):
            no = int(l["lineNo"]) if l.get("lineNo") is not None else i
            while no in numbers_used:
                no += 1
            numbers_used.add(no)
            lines.append(BranchSaleLine(
                sale_id=sale.id, line_no=no, product_sku=cut(l.get("productSku"), 40) or "?",
                product_name=cut(l.get("productName"), 200), department=cut(l.get("department"), 80),
                qty=dec(l.get("qty")), unit_price=dec(l.get("unitPrice")),
                disc_amount=dec(l.get("discAmount")), tax_amount=dec(l.get("taxAmount")),
                unit_cost=dec(l.get("unitCost")), is_return=bool(l.get("isReturn")),
                alias_code=cut(l.get("aliasCode"), 60), promotion_code=cut(l.get("promotionCode"), 20),
                sell_level=cut(l.get("sellLevel"), 20)))
        codes: set[str] = set()
        for t in row.get("tenders") or []:
            code = cut(t.get("code"), 20) or "?"
            if code in codes:
                continue
            codes.add(code)
            tenders.append(BranchSaleTender(
                sale_id=sale.id, code=code, name=cut(t.get("name"), 80), amount=dec(t.get("amount")),
                reference=cut(t.get("reference"), 120)))
    for chunk in range(0, len(docs), 1000):
        await BranchSale.bulk_create(docs[chunk:chunk + 1000], batch_size=1000)
    for chunk in range(0, len(lines), 2000):
        await BranchSaleLine.bulk_create(lines[chunk:chunk + 2000], batch_size=2000)
    for chunk in range(0, len(tenders), 2000):
        await BranchSaleTender.bulk_create(tenders[chunk:chunk + 2000], batch_size=2000)
    made["sales"], made["lines"], made["tenders"] = len(docs), len(lines), len(tenders)

    records, record_lines, seen = [], [], set()
    for row, day in returns_in:
        number = cut(row.get("number"), 30)
        if not number or number in seen:
            continue
        seen.add(number)
        record = BranchReturnRecord(
            id=uuid.uuid4(), branch=branch, number=number, at=at_of(row.get("at")), day=day,
            against_invoice=cut(row.get("againstInvoice"), 30), cashier_name=cut(row.get("cashierName"), 120),
            refund_total=dec(row.get("refundTotal")), tax_total=dec(row.get("taxTotal")),
            refund_method=cut(row.get("refundMethod"), 20), reason=cut(row.get("reason"), 160),
            note=cut(row.get("note"), 255), till_session_number=cut(row.get("tillSessionNumber"), 20),
            source="legacy")
        records.append(record)
        used: set[int] = set()
        for i, l in enumerate(row.get("lines") or [], 1):
            no = int(l["lineNo"]) if l.get("lineNo") is not None else i
            while no in used:
                no += 1
            used.add(no)
            record_lines.append(BranchReturnLine(
                return_record_id=record.id, line_no=no, product_sku=cut(l.get("productSku"), 40) or "?",
                product_name=cut(l.get("productName"), 200), qty=dec(l.get("qty")),
                unit_price=dec(l.get("unitPrice")), tax_amount=dec(l.get("taxAmount")),
                unit_cost=dec(l.get("unitCost"))))
    if records:
        await BranchReturnRecord.bulk_create(records, batch_size=1000)
        await BranchReturnLine.bulk_create(record_lines, batch_size=2000)
    made["returns"], made["returnLines"] = len(records), len(record_lines)

    stamps, stamp_lines, seen = [], [], set()
    for row, day in fbr_in:
        number = cut(row.get("invoiceNumber"), 30)
        kind = (cut(row.get("kind"), 10) or "sale").lower()
        if not number or f"{number}:{kind}" in seen:
            continue
        seen.add(f"{number}:{kind}")
        stamp = BranchFbrInvoice(
            id=uuid.uuid4(), branch=branch, invoice_number=number, kind=kind, at=at_of(row.get("at")), day=day,
            fbr_invoice_number=cut(row.get("fbrInvoiceNumber"), 60),
            status=cut(row.get("status"), 20) or "sent", pos_id=cut(row.get("posId"), 40),
            total=dec(row.get("total")), tax_total=dec(row.get("taxTotal")),
            message=cut(row.get("message"), 255), buyer_name=cut(row.get("buyerName"), 160),
            buyer_ntn=cut(row.get("buyerNtn"), 40), buyer_cnic=cut(row.get("buyerCnic"), 40), source="legacy")
        stamps.append(stamp)
        used = set()
        for i, l in enumerate(row.get("lines") or [], 1):
            no = int(l["lineNo"]) if l.get("lineNo") is not None else i
            while no in used:
                no += 1
            used.add(no)
            stamp_lines.append(BranchFbrInvoiceLine(
                invoice_id=stamp.id, line_no=no, product_sku=cut(l.get("productSku"), 40),
                product_name=cut(l.get("productName"), 200), hs_code=cut(l.get("hsCode"), 40),
                qty=dec(l.get("qty")), unit_price=dec(l.get("unitPrice")),
                disc_amount=dec(l.get("discAmount")), tax_rate=dec(l.get("taxRate")),
                tax_amount=dec(l.get("taxAmount")), total=dec(l.get("total"))))
    for chunk in range(0, len(stamps), 1000):
        await BranchFbrInvoice.bulk_create(stamps[chunk:chunk + 1000], batch_size=1000)
    for chunk in range(0, len(stamp_lines), 2000):
        await BranchFbrInvoiceLine.bulk_create(stamp_lines[chunk:chunk + 2000], batch_size=2000)
    made["fbr"], made["fbrLines"] = len(stamps), len(stamp_lines)
    return made


# ── the books ─────────────────────────────────────────────────────────────────────────────────────
async def load_books(branch, data: dict, chart_only: bool = False) -> None:
    """Their chart of accounts, and the vouchers from their entry forms.

    `chart_only` loads the chart and leaves every voucher alone. That is what a windowed top-up wants, for two
    reasons that both matter:

      * The vouchers here come from `dbo.JournalVoucher`, their entry form, which holds one side of each entry.
        `scripts/load_legacy_ledger.py` loads `dbo.AccountLedger` instead, which holds both, and the two must never
        be in the books together or the same money is counted twice.
      * Their chart grows. Live has 1,335 accounts where the backup had 1,330, and a windowed ledger load hit two
        postings to accounts it had never heard of, dropped the lines, and left the window out by Rs 75,440 against
        a ledger of theirs that balances exactly. The chart has to arrive before the postings that use it.
    """
    from app.models import Account, AccountGroup, AccountSubGroup, Voucher, VoucherLine
    from app.services import accounts_chart_service as chart

    book = branch.code
    await chart.ensure_types_and_categories()

    if not chart_only:
        async with in_transaction():
            vouchers = list(await Voucher.filter(book=book, source__startswith="legacy:")
                            .values_list("id", flat=True))
            if vouchers:
                await VoucherLine.filter(voucher_id__in=vouchers).delete()
                await Voucher.filter(id__in=vouchers).delete()

    # A GROUP THAT HAS MOVED IS REWRITTEN, NOT SKIPPED
    # ------------------------------------------------
    # Both of these loops used to create what was missing and skip what was there, so anything the mapper learned
    # later never reached a chart that had already been loaded once. It is the same fault
    # `scripts/load_legacy_godown.py` was fixed for, and it is worse here, because the category on a group is the
    # one thing deciding which section of the income statement every account under it lands in.
    #
    # It cost two separate figures, both measured 13 October 2026 over 12 Sep to 11 Oct. Their category 7 was
    # remapped from our 52 to our 51 and no group moved, so cost of sales stayed at 0.00 with Rs 46,005,401.93 of
    # purchases sitting in operating expenses. Then `map_books._group_code` stopped collapsing 41 of their groups
    # into 26 codes, and no account moved into the groups that fix created, so Rs 1,192,505 of electricity bills
    # stayed filed under capital and out of the statement altogether.
    made_groups = moved_groups = 0
    for g in data["groups"]:
        gid = chart.gid(book, g["code"])
        have = await AccountGroup.filter(id=gid).values("category_id", "name", "priority")
        if not have:
            await AccountGroup.create(id=gid, book=book, code=g["code"], name=cut(g["name"], 100),
                                      category_id=g["categoryCode"], priority=g.get("priority") or 0,
                                      manual_code=cut(g.get("manualCode"), 30), standard=False)
            made_groups += 1
        elif str(have[0]["category_id"]) != str(g["categoryCode"]):
            await AccountGroup.filter(id=gid).update(category_id=g["categoryCode"], name=cut(g["name"], 100))
            moved_groups += 1
        sub = chart.gid(book, f"{g['code']}01")
        if not await AccountSubGroup.exists(id=sub):
            await AccountSubGroup.create(id=sub, book=book, code=f"{g['code']}01", name="DEFAULT SUBGROUP",
                                         group_id=gid, standard=False)

    # The account's id is kept. It is what every voucher line in the books points at, 5.3 million of them, so an
    # account that has changed groups is updated in place rather than replaced: replacing it would leave every
    # line pointing at an account that no longer exists.
    existing = {row["code"]: row for row in
                await Account.filter(book=book).values("id", "code", "group_id")}
    by_code: dict[str, str] = {code: str(row["id"]) for code, row in existing.items()}
    made_accounts = moved_accounts = 0
    for a in data["accounts"]:
        want_group = chart.gid(book, a["groupCode"])
        row = existing.get(a["code"])
        if row is not None:
            if str(row["group_id"]) != str(want_group):
                await Account.filter(id=row["id"]).update(
                    group_id=want_group, sub_group_id=chart.gid(book, f"{a['groupCode']}01"))
                moved_accounts += 1
            continue
        account = await Account.create(
            book=book, code=a["code"], name=cut(a["name"], 160), kind=cut(a["kind"], 12),
            group_id=want_group, sub_group_id=chart.gid(book, f"{a['groupCode']}01"),
            active=a["active"], restricted=a["restricted"], check_limit=a["checkLimit"],
            balance_limit=dec(a["balanceLimit"]), bank_account_no=cut(a.get("bankAccountNo"), 40),
            manual_code=cut(a.get("manualCode"), 30), remarks=cut(a.get("remarks"), 255),
            party_code=cut(a.get("partyCode"), 20), party_phone=cut(a.get("partyPhone"), 30), standard=False)
        by_code[a["code"]] = str(account.id)
        made_accounts += 1
    print(f"   books    {made_groups:,} groups, {made_accounts:,} accounts in book {book}")
    if moved_groups or moved_accounts:
        print(f"   books    {moved_groups:,} group(s) recategorised and {moved_accounts:,} account(s) moved to "
              f"where their chart now puts them")
    if chart_only:
        return

    written = drafted = skipped = renumbered = 0
    batch: list[Voucher] = []
    lines: list[VoucherLine] = []
    # Every number already in the table, not just the ones this run writes. `number` is unique company-wide, so a
    # shop loaded after another can collide with it, and the mapper cannot see the other shop.
    seen_numbers: set[str] = {row["number"] for row in await Voucher.all().values("number")}
    for v in data["vouchers"]:
        account_ids = [(l, by_code.get(l["accountCode"])) for l in v["lines"]]
        if any(aid is None for _l, aid in account_ids):
            skipped += 1
            continue
        number = v["number"]
        if number in seen_numbers:
            renumbered += 1
            source_key = v["source"].rsplit(":", 1)[-1]
            number = f"{number[:22]}-{source_key}"[:30]
        while number in seen_numbers:
            number = f"{number[:26]}-{len(seen_numbers) % 1000}"[:30]
        seen_numbers.add(number)
        voucher = Voucher(
            id=uuid.uuid4(), book=book, number=number[:30], vtype=v["vtype"], date=day_of(v["date"]),
            status=v["status"], auto=False, source=v["source"][:160],
            header_account_id=by_code.get(v.get("headerAccountCode") or ""),
            reference_no=cut(v.get("referenceNo"), 60), description=cut(v.get("description"), 500),
            cheque_no=cut(v.get("chequeNo"), 30), total=dec(v["total"]),
            created_by_name=cut(v.get("createdByName"), 120), posted_by_name=cut(v.get("postedByName"), 120),
            posted_at=at_of(v["date"]) if v["status"] == "posted" else None)
        batch.append(voucher)
        for l, aid in account_ids:
            lines.append(VoucherLine(voucher_id=voucher.id, line_no=l["lineNo"], account_id=aid,
                                     debit=dec(l["debit"]), credit=dec(l["credit"]),
                                     description=cut(l.get("description"), 255), reference_no=cut(l.get("referenceNo"), 60)))
        written += 1
        if v["status"] == "draft":
            drafted += 1
        if len(batch) >= 500:
            await Voucher.bulk_create(batch, batch_size=500)
            await VoucherLine.bulk_create(lines, batch_size=1000)
            batch, lines = [], []
    if batch:
        await Voucher.bulk_create(batch, batch_size=500)
        await VoucherLine.bulk_create(lines, batch_size=1000)
    print(f"   vouchers {written:,} written, {drafted:,} of them drafts"
          + (f", {skipped:,} skipped for an account that is not in the book" if skipped else "")
          + (f", {renumbered:,} renumbered because their number was already taken" if renumbered else ""))


# ── buying, and the parties it names ──────────────────────────────────────────────────────────────
async def load_buying(branch, data: dict, window: tuple[date, date] | None = None) -> None:
    """Suppliers, customers, and the three buying documents.

    WITH A WINDOW, NOTHING OUTSIDE IT IS TOUCHED
    --------------------------------------------
    This replaced everything for the branch, unconditionally, which is right for a whole load and catastrophic for an
    incremental one: a two day run would have deleted 13,789 purchase orders, 25,068 deliveries, 4,044 returns and
    1,158 parties and put back only what those two days mentioned. Not a duplicate; the opposite, and silent, because
    no constraint fires when rows merely go missing.

    So with a window the documents are replaced by day, the same way bills are, and the parties are merged rather
    than replaced, because a party list is a list of who exists and not a stream of dated events.
    """
    from app.models import (BranchGrn, BranchGrnLine, BranchParty, BranchPurchaseOrder, BranchPurchaseOrderLine,
                            BranchPurchaseReturn, BranchPurchaseReturnLine)

    parties = data.get("parties") or []
    if window is None:
        await BranchParty.filter(branch=branch).delete()
        await BranchParty.bulk_create([
            BranchParty(branch=branch, code=str(p["code"])[:40], side=p["side"], name=p["name"][:200],
                        phone=p.get("phone"), phone2=p.get("phone2"), email=p.get("email"),
                        address=p.get("address"), city=p.get("city"), ntn=p.get("ntn"),
                        s_tax_reg_no=p.get("sTaxRegNo"), cnic=p.get("cnic"),
                        contact_person=p.get("contactPerson"), credit_limit=dec(p.get("creditLimit")),
                        due_days=p.get("dueDays"), discount_percent=dec(p.get("discountPercent")),
                        account_code=p.get("accountCode"), active=bool(p.get("active")), source="legacy")
            for p in parties
        ], batch_size=500)
        print(f"   parties  {len(parties):,}")
    else:
        # Merged on (branch, code, side), the table's own unique rule.
        have = {(p.code, p.side): p for p in await BranchParty.filter(branch=branch)}
        made, changed = [], 0
        for p in parties:
            key = (str(p["code"])[:40], p["side"])
            fields = dict(name=p["name"][:200], phone=p.get("phone"), phone2=p.get("phone2"),
                          email=p.get("email"), address=p.get("address"), city=p.get("city"),
                          ntn=p.get("ntn"), s_tax_reg_no=p.get("sTaxRegNo"), cnic=p.get("cnic"),
                          contact_person=p.get("contactPerson"), credit_limit=dec(p.get("creditLimit")),
                          due_days=p.get("dueDays"), discount_percent=dec(p.get("discountPercent")),
                          account_code=p.get("accountCode"), active=bool(p.get("active")), source="legacy")
            existing = have.get(key)
            if existing is None:
                made.append(BranchParty(branch=branch, code=key[0], side=key[1], **fields))
                continue
            if any(getattr(existing, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(existing, k, v)
                await existing.save(update_fields=list(fields))
                changed += 1
        if made:
            await BranchParty.bulk_create(made, batch_size=500)
        print(f"   parties  {len(made):,} added, {changed:,} updated, {len(have):,} already there and kept")

    # The key is named explicitly per document, never chained with `or`: a delivery carries the number of the order
    # it came from as well as its own, so `poNumber or grnNumber` numbered every delivery after its order and several
    # deliveries from one order collided on a unique constraint.
    for label, rows, Doc, Line, number_field, payload_key, doc_field in (
        ("orders", data.get("orders") or [], BranchPurchaseOrder, BranchPurchaseOrderLine,
         "po_number", "poNumber", "purchase_order"),
        ("deliveries", data.get("grns") or [], BranchGrn, BranchGrnLine,
         "grn_number", "grnNumber", "grn"),
        ("returns", data.get("purchaseReturns") or [], BranchPurchaseReturn, BranchPurchaseReturnLine,
         "return_number", "returnNumber", "purchase_return"),
    ):
        if window is None:
            await Doc.filter(branch=branch).delete()
        else:
            # The same shape as the bills: drop what falls outside, clear the window, then insert. A document whose
            # day is outside the window is skipped rather than written, so nothing can land on a day the delete
            # never cleared.
            start, end = window
            before = len(rows)
            rows = [r for r in rows if (day_of(r["day"]) or date.min) >= start
                    and (day_of(r["day"]) or date.max) <= end]
            if before != len(rows):
                print(f"   {label:<8} {before - len(rows):,} outside the window, not written")
            # Clear the window by day, AND clear any document whose number we are about to write again, wherever it
            # currently sits. The second half is not belt and braces: the unique key is (branch, number) while the
            # delete was keyed on day, so a document that changes day between runs is not cleared and its insert
            # violates the constraint. That is a crash, not a bad row, and it stops the whole shop -- the 2 Oct run
            # died on GT and so GG, MT, FC and head office never loaded at all, which is why the day looked empty.
            # It is an ordinary event, not a rarity: a three-day window had four of them across three shops, their
            # software having re-dated three documents to the day they were touched and reused one order number.
            # Their system is the authority on both the number and the date, so the old row goes and the new one
            # lands wherever they now say it belongs.
            doomed = set(await Doc.filter(branch=branch, day__gte=start, day__lte=end).values_list("id", flat=True))
            incoming = [str(r[payload_key])[:30] for r in rows]
            moved = 0
            for part in range(0, len(incoming), 500):      # 500 at a time: SQL Server takes 2,100 parameters in all
                elsewhere = await Doc.filter(branch=branch, **{f"{number_field}__in": incoming[part:part + 500]})                                      .values_list("id", flat=True)
                for one in elsewhere:
                    if one not in doomed:
                        doomed.add(one)
                        moved += 1
            if moved:
                print(f"   {label:<8} {moved:,} already here on another day, replaced where their system now puts it")
            # The lines go first and by name. SQL Server does cascade these, checked rather than assumed: all three
            # carry ON DELETE CASCADE. SQLite honours a cascade only with PRAGMA foreign_keys on and this connection
            # has it off, so leaning on the cascade would clear the lines on one engine and orphan them on the other.
            # Both deletes key on the ids gathered above, so the rows whose lines went are exactly the rows that go.
            doomed = list(doomed)
            for part in range(0, len(doomed), 500):
                await Line.filter(**{f"{doc_field}_id__in": doomed[part:part + 500]}).delete()
            for part in range(0, len(doomed), 500):
                await Doc.filter(id__in=doomed[part:part + 500]).delete()
        docs, doc_lines = [], []
        for r in rows:
            number = str(r[payload_key])[:30]
            common = dict(id=uuid.uuid4(), branch=branch, at=at_of(r["at"]), day=day_of(r["day"]),
                          supplier_code=r.get("supplierCode"), supplier_name=r.get("supplierName"),
                          source="legacy")
            common[number_field] = number
            if Doc is BranchPurchaseOrder:
                doc = Doc(**common, status=r["status"], raised_by_name=r.get("raisedByName"),
                          approved_by_name=r.get("approvedByName"),
                          expected_at=at_of(r["expectedAt"]) if r.get("expectedAt") else None,
                          approved_at=at_of(r["approvedAt"]) if r.get("approvedAt") else None,
                          closed_at=at_of(r["closedAt"]) if r.get("closedAt") else None,
                          notes=r.get("notes"), total=dec(r["total"]))
            elif Doc is BranchGrn:
                doc = Doc(**common, party_inv_no=cut(r.get("partyInvNo"), 40),
                          party_inv_date=day_of(r.get("partyInvDate")), po_number=cut(r.get("poNumber"), 30),
                          gst_mode=r.get("gstMode"), received_by_name=r.get("receivedByName"),
                          posted_by_name=r.get("postedByName"), approved=bool(r.get("approved")),
                          gross_total=dec(r["grossTotal"]), disc_total=dec(r["discTotal"]),
                          tax_total=dec(r["taxTotal"]), advance_tax=dec(r["advanceTax"]),
                          net_total=dec(r["netTotal"]), paid=dec(r["paid"]),
                          outstanding=dec(r["outstanding"]), payment_mode=r.get("paymentMode"))
            else:
                doc = Doc(**common, against_grn=r.get("againstGrn"), party_inv_no=r.get("partyInvNo"),
                          reason=r.get("reason"), notes=r.get("notes"),
                          submitted_by_name=r.get("submittedByName"),
                          total=dec(r["total"]), tax_total=dec(r["taxTotal"]))
            docs.append(doc)
            for l in r.get("lines") or []:
                fields = dict(line_no=l["lineNo"], product_sku=str(l["productSku"])[:40],
                              product_name=(l.get("productName") or None), qty=dec(l["qty"]),
                              unit_price=dec(l["unitPrice"]))
                fields[f"{doc_field}_id"] = doc.id
                if Line is BranchPurchaseOrderLine:
                    fields |= dict(received_qty=dec(l["receivedQty"]), disc_percent=dec(l["discPercent"]),
                                   total=dec(l["total"]), stock_at_order=dec(l["stockAtOrder"]))
                elif Line is BranchGrnLine:
                    fields |= dict(bonus_qty=dec(l["bonusQty"]), disc_percent=dec(l["discPercent"]),
                                   flat_disc=dec(l["flatDisc"]), tax_rate=dec(l["taxRate"]),
                                   tax_amount=dec(l["taxAmount"]), total=dec(l["total"]),
                                   batch_no=l.get("batchNo"), expires_on=day_of(l.get("expiresOn")),
                                   new_sale_price=dec(l["newSalePrice"]),
                                   new_retail_price=dec(l["newRetailPrice"]), unit_cost=dec(l["unitCost"]))
                else:
                    fields |= dict(tax_rate=dec(l["taxRate"]), tax_amount=dec(l["taxAmount"]),
                                   total=dec(l["total"]), unit_cost=dec(l["unitCost"]),
                                   batch_no=l.get("batchNo"), expires_on=day_of(l.get("expiresOn")))
                doc_lines.append(Line(**fields))
        if docs:
            await Doc.bulk_create(docs, batch_size=500)
            await Line.bulk_create(doc_lines, batch_size=1000)
        print(f"   {label:<8} {len(docs):,} documents, {len(doc_lines):,} lines")


def within(month: dict, window: tuple[date, date] | None) -> tuple[date, date] | None:
    """The part of a monthly file a run should replace, or None when the month is outside the window entirely.

    Both the documents and the movements are filed one month to a file, and both loops used to replace **every**
    month whatever window was asked for. The rows came back identical in number, so nothing looked wrong, and every
    one of them got a new id: a windowed run rewrote 547,396 bills to change 4,637 of them, and would have done it
    again every night. The safety check caught it by comparing ids rather than counts, which is the only way that
    fault is visible at all.
    """
    first, last = day_of(month["from"]), day_of(month["to"])
    if window is None:
        return first, last
    start, end = window
    if last < start or first > end:
        return None
    return max(first, start), min(last, end)


async def load_movements(branch, index: dict, window: tuple[date, date] | None = None) -> None:
    from app.models import BranchStockMovement

    months = index.get("months") or []
    if not months:
        print("   movements none mapped")
        return
    total = 0
    for n, month in enumerate(months, 1):
        path = STAGING / month["file"]
        if not path.exists():
            print(f"   {month['file']} is in the index and not on disk")
            continue
        span = within(month, window)
        if span is None:
            continue
        first, last = span
        rows = [r for r in (json.loads(path.read_text(encoding="utf-8")).get("movements") or [])
                if first <= (day_of(r["day"]) or date.min) <= last]
        await BranchStockMovement.filter(branch=branch, day__gte=first, day__lte=last).delete()
        for start in range(0, len(rows), 2000):
            await BranchStockMovement.bulk_create([
                BranchStockMovement(
                    branch=branch, at=at_of(r["at"]), day=day_of(r["day"]), kind=r["kind"][:10],
                    document_number=(r.get("documentNumber") or None), product_sku=str(r["productSku"])[:40],
                    qty_in=dec(r["qtyIn"]), qty_out=dec(r["qtyOut"]), balance_after=dec(r["balanceAfter"]),
                    rate=dec(r["rate"]), avg_cost_after=dec(r["avgCostAfter"]),
                    party_code=(r.get("partyCode") or None), user_name=(r.get("userName") or None),
                    batch_no=(r.get("batchNo") or None), expires_on=day_of(r.get("expiresOn")),
                    remarks=(r.get("remarks") or None), source="legacy")
                for r in rows[start:start + 2000]
            ], batch_size=1000)
        total += len(rows)
        print(f"   [{n:>2}/{len(months)}] {month['from'][:7]}  {len(rows):>9,} movements")
    print(f"   movements {total:,} in all")


# ── the run ───────────────────────────────────────────────────────────────────────────────────────
async def run(args) -> int:
    only = set(x.strip() for x in args.only.split(",")) if args.only else set(STAGES)
    unknown = only - set(STAGES)
    if unknown:
        raise SystemExit(f"not a stage: {', '.join(sorted(unknown))}. They are {', '.join(STAGES)}")

    figures = payload(args.shop, "figures", required="figures" in only or "stock" in only)
    if not figures:
        # Only the stages that were asked for need a payload. A shop loaded for its books alone does not need one.
        figures = {"shopName": args.shop, "branches": [], "from": "-", "to": "-"}
    documents = payload(args.shop, "documents", required="documents" in only)
    books = payload(args.shop, "books", required="books" in only)
    buying = payload(args.shop, "buying", required="buying" in only)
    stock_index = payload(args.shop, "stock", required=False)

    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Branch, BranchDailyStat, BranchSale
        from app.services import branch_documents_service, snapshot_service

        print(f"   writing  {which_database()}")
        print(f"   DB_URL   {masked(os.environ.get('DB_URL') or '(not set, so the development default)')}")

        code = args.shop
        branch = await Branch.get_or_none(code=code)
        if branch and branch.verified_at is not None and not args.force:
            raise SystemExit(
                f"\n   {code} has its own branch server (verified {branch.verified_at:%Y-%m-%d}). Its history comes "
                f"from that server, never from a file.\n   --force overrides this and you should be sure why.")
        had = bool(branch) and await BranchDailyStat.filter(branch=branch).exists()
        if had and not args.replace:
            raise SystemExit(f"\n   {code} already has figures at head office. --replace loads it again.")

        window = None
        if args.window:
            a, _, b = args.window.partition(":")
            window = (date.fromisoformat(a), date.fromisoformat(b))
        # A load with no window replaces everything the stage covers. If this payload stops earlier than the days
        # already here, the days in between are deleted and nothing goes back in their place.
        #
        # That is what the restored backup does: it stops on the day it was taken, and the days after it live only
        # in the copy fetched over the VPN. It cost three separate screens in one morning -- the daily figures went
        # back to 22 September, the bills followed, and so did the books -- and each time it looked like the sync
        # had failed when the sync had been fine. The nightly does backup first and live window after; by hand that
        # order is the easy thing to forget, so it is checked here rather than remembered.
        if window is None and branch is not None:
            latest_here = await BranchDailyStat.filter(branch=branch).order_by("-day").first()
            payload_last = str(figures.get("to") or "")[:10]
            here_last = latest_here.day.isoformat() if latest_here else ""
            if here_last and payload_last and here_last > payload_last and not args.even_if_older:
                raise SystemExit(
                    f"\n   STOPPING. {code} has figures to {here_last} and this payload stops at {payload_last}."
                    f"\n   Loading it without a window would delete every day in between and put nothing back."
                    f"\n   Map the tail from the live copy and load it as a window:"
                    f"\n      LEGACY_STAGING=live python map_figures.py --shop {code} --from {payload_last} --to {here_last}"
                    f"\n      python -m scripts.load_legacy_shop --shop {code} --window {payload_last}:{here_last} ..."
                    f"\n   or pass --even-if-older if losing those days is what you meant.")

        months = [m for m in documents.get("months", [])
                  if not window or not (m["to"] < window[0].isoformat() or m["from"] > window[1].isoformat())]

        print(f"\n   {figures['shopName']} ({code}), their branch {', '.join(figures['branches'])}")
        print(f"   {figures['from']} to {figures['to']}"
              + (f", narrowed to {window[0]}..{window[1]}" if window else ""))
        print(f"   stages   {', '.join(s for s in STAGES if s in only)}")
        for key in ("daily", "products", "cashiers", "hourly", "tillCloses", "tenders", "returns",
                    "creditCustomers", "stock"):
            print(f"      {key:<18} {len(figures.get(key) or []):>9,}")
        print(f"      {'documents':<18} {sum(m['sales'] for m in months):>9,} bills, "
              f"{sum(m['lines'] for m in months):,} lines")
        if books:
            b = books.get("balance") or {}
            print(f"      {'accounts':<18} {len(books.get('accounts') or []):>9,}")
            print(f"      {'vouchers':<18} {len(books.get('vouchers') or []):>9,}  "
                  f"({b.get('not at all', 0):,} unbalanced, carried as drafts)")
        if buying:
            for key, label in (("orders", "purchase orders"), ("grns", "deliveries"),
                               ("purchaseReturns", "purchase returns"), ("parties", "parties"),
                               ("roster", "staff")):
                print(f"      {label:<18} {len(buying.get(key) or []):>9,}")
        print(f"      {'stock movements':<18} "
              f"{sum(m['movements'] for m in stock_index.get('months', [])):>9,}")

        if not args.yes:
            print("\n   Dry run. Nothing written. Add --yes.")
            return 0

        if not branch:
            branch = await Branch.create(code=code, name=figures["shopName"], city=args.city,
                                         status="active", timezone="Asia/Karachi")
            print(f"\n   created branch {code} ({figures['shopName']}), unverified, no sync secret")
        if not args.no_backup:
            back_up(code)
        print()

        if "masters" in only:
            await load_masters(code)
        if "roster" in only and buying:
            await load_roster(branch, buying.get("roster") or [], whole=window is None)
        if figures.get("note"):
            print(f"   note     {figures['note']}")
        counted: dict = {}
        if "figures" in only and (figures.get("daily") or []):
            summary = await snapshot_service.apply_aggregates(branch, figures, source="legacy", window=window)
            counted = summary
            print(f"   figures  {summary['tradingDays']:,} trading days, {summary['productDays']:,} Item-days, "
                  f"{summary['skippedRows']:,} skipped")
        if "stock" in only and (figures.get("stock") or []):
            rows = figures["stock"]
            snapshot_id = uuid.uuid4().hex
            await snapshot_service.begin_stock(branch, snapshot_id, source="legacy")
            for start in range(0, len(rows), 2000):
                await snapshot_service.add_stock_chunk(branch, snapshot_id, rows[start:start + 2000])
            done = await snapshot_service.complete_stock(branch, snapshot_id)
            print(f"   stock    {done}")
            # The two counters on the run row that nothing has ever written. They were printed a line above and
            # thrown away, so every Sync Health row read "0 trading days, 0 Item-days" for a load that had just
            # placed thousands of both. The numbers are the figures stage's own summary, not a second count.
            if counted:
                from app.models import BranchSnapshotRun
                await BranchSnapshotRun.filter(branch=branch, snapshot_id=snapshot_id).update(
                    trading_days=int(counted.get("tradingDays") or 0),
                    product_days=int(counted.get("productDays") or 0))
        if "documents" in only:
            filed = 0
            skipped = 0
            for n, month in enumerate(months, 1):
                span = within(month, window)
                if span is None:
                    skipped += 1
                    continue
                first, last = span
                path = STAGING / month["file"]
                if not path.exists():
                    print(f"   {month['file']} is in the index and not on disk")
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if args.per_bill:
                    result = await branch_documents_service.replace_window(
                        branch, first, last,
                        data.get("sales") or [], data.get("returns") or [], data.get("fbr") or [])
                    result["lines"] = 0
                else:
                    result = await load_documents_bulk(
                        branch, first, last,
                        data.get("sales") or [], data.get("returns") or [], data.get("fbr") or [])
                filed += result["sales"]
                print(f"   [{n:>2}/{len(months)}] {month['from'][:7]}  {result['sales']:>7,} bills  "
                      f"{result.get('lines', 0):>8,} lines  {result['returns']:>5,} returns  "
                      f"{result.get('fbr', result.get('fbrInvoices', 0)):>7,} fbr")
            if skipped:
                print(f"   documents {skipped} month(s) outside the window, left alone")
            print(f"   documents {filed:,} bills this run, "
                  f"{await BranchSale.filter(branch=branch).count():,} at head office for {code}")
        if "books" in only and books:
            await load_books(branch, books)
        elif "chart" in only and books:
            # The chart without the entry-form vouchers: what a windowed run needs before the ledger is loaded.
            await load_books(branch, books, chart_only=True)
        if "buying" in only and buying:
            await load_buying(branch, buying, window=window)
        if "movements" in only:
            await load_movements(branch, stock_index, window=window)

        print(f"\n   {figures['shopName']} is at head office. It has no branch server and never will be sent "
              f"anything.")
        return 0
    finally:
        await Tortoise.close_connections()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shop", required=True, help="GT, GG, MT, FC or HO")
    ap.add_argument("--yes", action="store_true", help="write it. Without this nothing is written")
    ap.add_argument("--replace", action="store_true", help="this shop has figures already: rewrite them")
    ap.add_argument("--force", action="store_true", help="load a shop that has its own branch server. Be sure")
    ap.add_argument("--window", help="FROM:TO, narrows what is replaced to those days")
    ap.add_argument("--only", help=f"a comma separated subset of {', '.join(STAGES)}")
    ap.add_argument("--no-backup", action="store_true", help="skip the VACUUM INTO copy of the database")
    ap.add_argument("--even-if-older", action="store_true",
                    help="load without a window even though this payload stops earlier than the days already "
                         "loaded, which deletes the days in between. Only when losing them is what you meant")
    ap.add_argument("--per-bill", action="store_true",
                    help="file each bill through branch_documents_service one at a time, the way a live branch "
                         "does. Correct and about twenty times slower; the bulk path is the default")
    ap.add_argument("--city", default=None)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
