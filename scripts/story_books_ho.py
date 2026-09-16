"""Complete head office's story from 1 Aug to 15 Sep 2026: the DM SELECT godown's launch, its buying, and head office's own books.

    python -m scripts.story_books_ho            # dry run
    python -m scripts.story_books_ho --yes      # writes it (stop the Cloud Server and back up cloud.db first)

The godown was restocked on 14 Sep with the DM SELECT range: 21 one-line GRNs all stamped the same minute, no purchase
orders, no supplier payments, head office's books starting that day. This keeps every Item, bin, stock quantity and
transfer exactly as they are and tells the story that stock implies:

  August      head office gets ready: godown rent, staff, racks and pallet trucks bought, electricity, internet
  late Aug    the Warehouse Managers raise the launch orders; the one above Rs 5 lakh waits for the Owner's approval
  2-10 Sep    the deliveries arrive against those orders (one GRN per supplier bill, a price that differed, a bonus)
  Sep         suppliers paid by bank, Model Town supplied from the godown (the transfers already there), a reorder
              waiting for approval and another on its way
  books       opening balances on 31 Jul, books from 1 Aug, numbers in date order, August closed

Purchase orders go through the purchasing service, so approval limits and receiving work exactly as on screen.
It refuses to run twice.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from tortoise import Tortoise

from app.core.config import TORTOISE_ORM

STORY_KEY = "story:books-ho-2026-09-15"
BOOKS_START = date(2026, 8, 1)
END = date(2026, 9, 15)
PKT = timezone(timedelta(hours=5))
ZERO = Decimal("0")
NOW = datetime.now(timezone.utc)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    moment = datetime.combine(day, time(hour, minute), tzinfo=PKT).astimezone(timezone.utc)
    return min(moment, NOW - timedelta(minutes=20)) if day >= END else moment


def promised(day: date) -> datetime:
    """A date a supplier promised: noon that day, which may well be after today."""
    return datetime.combine(day, time(12), tzinfo=PKT).astimezone(timezone.utc)


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def say(text: str) -> None:
    print(f"  {text}", flush=True)


# Supplier bill -> (delivery day, hour, received by, supplier contact on the bill)
DELIVERIES = {
    "MSS-26-4387": (date(2026, 9, 3), 11, "sana"),
    "AHP-2609-098": (date(2026, 9, 2), 12, "wm"),
    "GUD-55871": (date(2026, 9, 4), 10, "sana"),
    "AHP-2609-114": (date(2026, 9, 5), 11, "wm"),
    "FINO-7730": (date(2026, 9, 7), 15, "sana"),
    "AHP-2609-121": (date(2026, 9, 8), 12, "wm"),
    "MSS-26-4410": (date(2026, 9, 10), 14, "sana"),
}

# The launch orders: supplier code, raised on, raised by, reason, the bills received against it, lines ordered at (unit cost)
LAUNCH_ORDERS = [
    ("SUPAHP", date(2026, 8, 25), "sana", "DM SELECT launch stock: rice, daal and spices", ("AHP-2609-098", "AHP-2609-114", "AHP-2609-121")),
    ("SUPGRO", date(2026, 8, 27), "wm", "DM SELECT launch stock: sugar and green tea", ("GUD-55871",)),
    ("SUP786", date(2026, 8, 28), "sana", "DM SELECT launch stock: household and baby care", ("FINO-7730",)),
    ("SUPSRG", date(2026, 8, 29), "wm", "DM SELECT launch stock: surgical and first aid", ("MSS-26-4387", "MSS-26-4410")),
]
# What each order was placed at, where the bill came in different
ORDERED_AT = {"2108260000018": Decimal("1980"), "2108260000179": Decimal("310")}


class Story:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.notes: list[str] = []

    async def load(self) -> None:
        from app.models import HEAD_OFFICE_BOOK, Account, Supplier, User
        from app.services.accounts_chart_service import Resolver

        self.wm = await User.get(email="warehousemanager@cloud.dmarina.pk")
        self.sana = await User.get(email="sana@cloud.dmarina.pk")
        self.owner = await User.get(email="executive@cloud.dmarina.pk")
        self.accounts_admin = await User.get(email="accountsadmin@cloud.dmarina.pk")
        self.people = {"wm": self.wm, "sana": self.sana}
        self.suppliers = {s.code: s for s in await Supplier.all()}
        self.acc = Resolver(HEAD_OFFICE_BOOK)
        self.A = {}
        for key in ("cash.main", "cash.petty", "bank.main", "tax.wht_payable", "equity.capital"):
            self.A[key] = await self.acc.key(key)
        for code in ("52040001", "52020001", "52030001", "52030004", "52010006", "52010008", "52050002", "52050001", "52040002", "52010003", "52010004",
                     "52010007", "12010001", "12010002", "12010003", "12010004", "12020001", "21040001", "21040002", "53010001"):
            self.A[code] = await Account.get(book=HEAD_OFFICE_BOOK, code=code)

    # ── the godown's deliveries: one GRN per supplier bill, dated when it came ────────────────────
    async def deliveries(self) -> None:
        from app.models import GRN, GRNLine, Notice, StockMovement

        grns = await GRN.all().order_by("grn_number")
        by_bill = defaultdict(list)
        for grn in grns:
            by_bill[grn.party_inv_no].append(grn)
        unknown = [bill for bill in by_bill if bill not in DELIVERIES]
        if unknown:
            raise SystemExit(f"Godown GRNs this story doesn't know: {unknown}")
        self.grn_for_bill = {}
        for bill, group in by_bill.items():
            day, hour, who = DELIVERIES[bill]
            keep, rest = group[0], group[1:]
            when = at(day, hour, 10 + len(bill) % 40)
            for other in rest:
                await GRNLine.filter(grn_id=other.id).update(grn_id=keep.id)
                await StockMovement.filter(reason=other.grn_number).update(reason=keep.grn_number)
                await Notice.filter(subject_id=str(other.id)).delete()
                if await GRNLine.filter(grn_id=other.id).exists():
                    raise SystemExit(f"{other.grn_number} still has lines")
                await other.delete()
                self.counts["one-line GRNs of the same bill joined into one"] += 1
            await GRN.filter(id=keep.id).update(at=when, approved=True, received_by_id=self.people[who].id)
            await StockMovement.filter(reason=keep.grn_number).update(at=when, origin_user_id=self.people[who].id)
            self.grn_for_bill[bill] = str(keep.id)
        # numbers in date order, continuing from where the godown's numbering started
        ordered = await GRN.all().order_by("at", "id")
        first = min(int(g.grn_number.split("-")[1]) for g in ordered)
        planned = [(g, g.grn_number, f"WGRN-{first + i:04d}") for i, g in enumerate(ordered)]
        for g, _old, _new in planned:
            await GRN.filter(id=g.id).update(grn_number=f"TMP-{g.id}"[:30])
        for g, old, new in planned:
            await GRN.filter(id=g.id).update(grn_number=new)
            await StockMovement.filter(reason=old).update(reason=f"~{new}")
        for _g, _old, new in planned:
            await StockMovement.filter(reason=f"~{new}").update(reason=new)
        from app.models import Counter

        await Counter.filter(id="warehouse_grn").update(value=first + len(planned))
        self.counts["godown GRNs"] = len(planned)

    # ── the orders behind them ────────────────────────────────────────────────────────────────────
    async def orders(self) -> None:
        from app.models import GRN, GRNLine, Notice, PurchaseOrder
        from app.services import purchasing_service

        async def stamp(po_id: str, raised: datetime, submitted: datetime | None = None, approved: datetime | None = None, closed: datetime | None = None):
            fields = {"raised_at": raised}
            if submitted:
                fields["submitted_at"] = submitted
            if approved:
                fields["approved_at"] = approved
            if closed:
                fields["closed_at"] = closed
            await PurchaseOrder.filter(id=po_id).update(**fields)
            for notice in await Notice.filter(subject_id=po_id):
                moment = {"po.submitted": submitted, "po.approved": approved}.get(notice.kind)
                if moment:
                    await Notice.filter(id=notice.id).update(at=moment)

        for code, day, who, reason, bills in LAUNCH_ORDERS:
            raiser = self.people[who]
            wanted: dict[str, list] = {}
            for bill in bills:
                for line in await GRNLine.filter(grn_id=self.grn_for_bill[bill]).order_by("id"):
                    pid = str(line.product_id)
                    cost = ORDERED_AT.get(pid, Decimal(line.unit_price))
                    if pid in wanted:
                        wanted[pid][1] += Decimal(line.qty)
                    else:
                        wanted[pid] = [pid, Decimal(line.qty), cost]
            po = await purchasing_service.create_order(raiser, self.suppliers[code].id, [tuple(v) for v in wanted.values()], promised(day + timedelta(days=8)), None, reason)
            po_id = str(po.id)
            raised = at(day, 11, 15)
            submitted = raised + timedelta(minutes=12)
            po = await purchasing_service.submit_order(raiser, po_id)
            approved = submitted
            if po.status == "pending_approval":
                po = await purchasing_service.approve_order(self.owner, po_id)
                approved = at(day + timedelta(days=1), 10, 5)
                self.counts["launch orders approved by the Owner (above the Warehouse Manager's Rs 5 lakh limit)"] += 1
            else:
                self.counts["launch orders approved within the raiser's own limit"] += 1
            await stamp(po_id, raised, submitted, approved)
            for bill in bills:
                grn = await GRN.get(id=self.grn_for_bill[bill])
                lines = await GRNLine.filter(grn_id=grn.id)
                before = datetime.now(timezone.utc) - timedelta(seconds=1)
                await purchasing_service.receive_against(po_id, self.suppliers[code].id, [(str(l.product_id), Decimal(l.qty), Decimal(l.unit_price)) for l in lines], grn.grn_number)
                await GRN.filter(id=grn.id).update(purchase_order_id=po_id)
                await Notice.filter(subject_id=po_id, kind="po.received", at__gte=before).update(at=grn.at)
                fresh = await PurchaseOrder.get(id=po_id)
                if fresh.status == "received":
                    await PurchaseOrder.filter(id=po_id).update(closed_at=grn.at)
            self.counts["launch purchase orders"] += 1

        # after the first deliveries went out to Model Town: a reorder on its way, and a bigger one waiting for the Owner
        from app.models import Product

        async def product(pid):
            return await Product.get(id=pid)

        on_way = await purchasing_service.create_order(self.sana, self.suppliers["SUPSRG"].id,
                                                       [("2108260000179", Decimal("300"), Decimal("310")), ("2108260000155", Decimal("300"), Decimal("98"))],
                                                       promised(date(2026, 9, 18)), "Masks and bandages going out to Model Town faster than planned", "Reorder")
        await purchasing_service.submit_order(self.sana, str(on_way.id))
        await stamp(str(on_way.id), at(date(2026, 9, 12), 10, 40), at(date(2026, 9, 12), 10, 52), at(date(2026, 9, 12), 10, 52))
        waiting = await purchasing_service.create_order(self.wm, self.suppliers["SUPAHP"].id,
                                                        [("2108260000018", Decimal("200"), Decimal("1980")), ("2108260000094", Decimal("120"), Decimal("1760")),
                                                         ("2108260000025", Decimal("100"), Decimal("345"))],
                                                        promised(date(2026, 9, 22)), "Rice and dry fruit ahead of the wedding season", "Season stock")
        await purchasing_service.submit_order(self.wm, str(waiting.id))
        await stamp(str(waiting.id), at(END, 10, 20), at(END, 10, 35))
        del product
        self.counts["purchase orders open (one on its way, one waiting for the Owner)"] = 2

    # ── transfers dispatched before the godown's cost went onto them ──────────────────────────────
    async def transfer_costs(self) -> None:
        from app.models import Product, StockMovement, Transfer, TransferLine

        for transfer in await Transfer.filter(source_branch_id__isnull=True).exclude(status="cancelled"):
            for line in await TransferLine.filter(transfer_id=transfer.id, unit_cost__isnull=True):
                product = await Product.get(id=line.product_id)
                cost = Decimal(product.avg_cost or 0).quantize(Decimal("0.0001"))
                await TransferLine.filter(id=line.id).update(unit_cost=cost)
                await StockMovement.filter(kind="dispatch", reason=transfer.transfer_number, product_id=line.product_id, unit_cost__isnull=True).update(unit_cost=cost)
                self.counts["godown transfer lines given their cost"] += 1

    # ── head office's books ───────────────────────────────────────────────────────────────────────
    async def voucher(self, day: date, vtype: str, lines, description: str, header=None, reference=None, cheque=None, hour=12):
        from app.models import Voucher
        from app.services import vouchers_service

        payload = {"vtype": vtype, "date": day.isoformat(), "description": description, "referenceNo": reference,
                   "headerAccountId": str(header.id) if header else None, "chequeNo": cheque, "chequeDate": day.isoformat() if cheque else None,
                   "lines": [{"accountId": str(a.id), "debit": str(money(dr)), "credit": str(money(cr)), "description": d} for a, dr, cr, d in lines]}
        draft = await vouchers_service.create_draft(self.accounts_admin, payload)
        posted = await vouchers_service.post(self.accounts_admin, str(draft.id))
        stamp = at(day, hour, 20)
        await Voucher.filter(id=posted.id).update(created_at=stamp - timedelta(minutes=15), posted_at=stamp, updated_at=stamp)
        self.counts[f"{vtype} vouchers made by hand"] += 1
        return posted

    async def books(self) -> None:
        from app.models import GRN, GRNLine, Voucher
        from app.services import accounts_posting_service, vouchers_service

        A = self.A
        bank, safe = A["bank.main"], A["cash.main"]
        row = await vouchers_service.settings()
        row.books_start = BOOKS_START
        row.locked_until = None
        await row.save()
        await accounts_posting_service.run(full=True)

        # opening balances: what head office had on 31 Jul
        suggestion = await accounts_posting_service.opening_suggestion()
        lines = [(await self._account(l["accountId"]), Decimal(l["debit"]), Decimal(l["credit"]), l["description"]) for l in suggestion["lines"]
                 if abs(Decimal(l["debit"]) - Decimal(l["credit"])) >= 1]  # paisa left over from average costs isn't an opening balance
        lines += [
            (bank, Decimal("6500000"), 0, "Bank statement balance on 31 Jul"),
            (safe, Decimal("80000"), 0, "Cash at head office"),
            (A["12010004"], Decimal("3200000"), 0, "Delivery truck (Hino 300) for branch supplies"),
            (A["12010003"], Decimal("450000"), 0, "Head office computers and the Cloud Server machine"),
            (A["12010001"], Decimal("350000"), 0, "Head office furniture"),
            (A["12020001"], 0, Decimal("185000"), "Depreciation to 31 Jul"),
        ]
        total = sum((money(dr) - money(cr) for _a, dr, cr, _d in lines), ZERO)
        lines.append((A["equity.capital"], 0, total, "Owner's capital in head office on 31 Jul"))
        ob = await self.voucher(date(2026, 7, 31), "OB", lines, "Opening balances on 31 Jul 2026: bank, cash and head office's fixed assets", reference="OPENING-2026", hour=8)
        await Voucher.filter(id=ob.id).update(posted_at=at(date(2026, 8, 1), 9, 0), created_at=at(date(2026, 8, 1), 8, 40))

        # August: getting the godown ready
        for d in (date(2026, 8, 1), date(2026, 9, 1)):
            await self.voucher(d, "JV", [(A["52040001"], Decimal("150000"), 0, f"Godown and head office rent for {d:%B %Y}"), (bank, 0, Decimal("135000"), "Cheque to Sheikh Properties"),
                                         (A["tax.wht_payable"], 0, Decimal("15000"), "10% withholding tax on rent")],
                               f"Rent for {d:%B %Y}, withholding tax deducted", reference=f"HO-RENT-{d:%b-%Y}".upper(), cheque=str(7702140 + d.month), hour=11)
        await self.voucher(date(2026, 8, 12), "BPV", [(A["12010001"], Decimal("1450000"), 0, "Steel racking for the godown, Punjab Steel Racks: 4 racks, 132 bins")],
                           "Godown racks bought", header=bank, reference="PSR-2026-0412", cheque="7702151", hour=15)
        await self.voucher(date(2026, 8, 19), "BPV", [(A["12010002"], Decimal("185000"), 0, "Two hand pallet trucks and a stock scanner")],
                           "Godown equipment bought", header=bank, reference="MHE-0819", hour=13)
        await self.voucher(date(2026, 8, 14), "BPV", [(A["tax.wht_payable"], Decimal("15000"), 0, "Withholding tax on August rent")], "Withholding tax deposited with FBR", header=bank, reference="CPR-HO-2026-08")
        await self.voucher(date(2026, 9, 14), "BPV", [(A["tax.wht_payable"], Decimal("15000"), 0, "Withholding tax on September rent")], "Withholding tax deposited with FBR", header=bank, reference="CPR-HO-2026-09")
        await self.voucher(date(2026, 8, 20), "CPV", [(A["52010006"], Decimal("12000"), 0, "Godown deep cleaning before the racks went in")], "Cleaning", header=safe, reference="CLEAN-HO-AUG")
        await self.voucher(date(2026, 8, 5), "BPV", [(A["52030004"], Decimal("8500"), 0, "Fibre internet for head office and the Cloud Server")], "Internet bill", header=bank, reference="NAYATEL-AUG")
        await self.voucher(date(2026, 9, 5), "BPV", [(A["52030004"], Decimal("8500"), 0, "Fibre internet for head office and the Cloud Server")], "Internet bill", header=bank, reference="NAYATEL-SEP")
        await self.voucher(date(2026, 8, 28), "BPV", [(A["52010007"], Decimal("35000"), 0, "Chartered accountants: chart of accounts and opening balances review")], "Professional fees", header=bank, reference="HKA-0826")
        await self.voucher(date(2026, 8, 31), "JV", [(A["52020001"], Decimal("185000"), 0, "August salaries from 11 Aug, when the godown team joined: Warehouse Managers, pickers, accounts and IT"), (A["21040001"], 0, Decimal("185000"), "Owed to staff")],
                           "August salaries accrued", reference="HO-SAL-2026-08", hour=18)
        await self.voucher(date(2026, 8, 31), "JV", [(A["52030001"], Decimal("41200"), 0, "Godown and head office electricity, August"), (A["21040002"], 0, Decimal("41200"), "Bill received")],
                           "August electricity accrued", reference="HO-UTIL-2026-08", hour=18)
        await self.voucher(date(2026, 8, 31), "JV", [(A["52010008"], Decimal("46000"), 0, "Depreciation for August: truck, racks, equipment, computers and furniture"), (A["12020001"], 0, Decimal("46000"), "Depreciation")],
                           "Depreciation for August", reference="HO-DEP-2026-08", hour=18)
        await self.voucher(date(2026, 8, 30), "CPV", [(A["52050002"], Decimal("9500"), 0, "Truck fuel: trial runs to Model Town and Fort Colony")], "Delivery truck fuel", header=safe, reference="FUEL-AUG")
        await self.voucher(date(2026, 9, 3), "BPV", [(A["21040001"], Decimal("185000"), 0, "August salaries")], "August salaries paid by bank transfer", header=bank, reference="HO-SAL-2026-08")
        await self.voucher(date(2026, 9, 9), "BPV", [(A["21040002"], Decimal("41200"), 0, "August electricity")], "August electricity paid", header=bank, reference="MEPCO-HO-AUG")

        # September: the launch deliveries, paid by bank; loading and fuel for supplying Model Town
        from app.services.accounts_chart_service import Resolver
        from app.models import HEAD_OFFICE_BOOK

        resolver = Resolver(HEAD_OFFICE_BOOK)

        async def bill_total(bill: str) -> Decimal:
            lines = await GRNLine.filter(grn_id=self.grn_for_bill[bill])
            return money(sum((Decimal(l.qty) * Decimal(l.unit_price) * (1 - Decimal(l.disc_percent or 0) / 100) * (1 + Decimal(l.tax_rate or 0) / 100) for l in lines), ZERO))

        payments = [
            (date(2026, 9, 9), "SUPSRG", ("MSS-26-4387",), "Online transfer"),
            (date(2026, 9, 11), "SUPGRO", ("GUD-55871",), "Online transfer"),
            (date(2026, 9, 12), "SUPAHP", ("AHP-2609-098", "AHP-2609-114"), "Cheque"),
            (date(2026, 9, 14), "SUP786", ("FINO-7730",), "Online transfer"),
        ]
        for day, code, bills, how in payments:
            supplier = self.suppliers[code]
            account = await resolver.supplier(supplier)
            amount = sum([await bill_total(b) for b in bills], ZERO)
            grns = [await GRN.get(id=self.grn_for_bill[b]) for b in bills]
            described = "; ".join(f"{g.grn_number} ({b})" for g, b in zip(grns, bills))
            await self.voucher(day, "BPV", [(account, amount, 0, described)], f"{how} to {supplier.name}", header=bank,
                               reference=bills[0], cheque=str(7702160 + day.day) if how == "Cheque" else None, hour=15)
            self.counts["supplier payments"] += 1
        await self.voucher(date(2026, 9, 14), "CPV", [(A["52050001"], Decimal("3500"), 0, "Loaders for TR-0048 to Model Town"), (A["52050002"], Decimal("6500"), 0, "Truck fuel, godown to Model Town and back")],
                           "Supplying Model Town", header=safe, reference="TR-0048", hour=17)
        await self.voucher(date(2026, 9, 15), "CPV", [(A["52050002"], Decimal("4200"), 0, "Truck fuel, godown to Model Town and back")], "Supplying Model Town", header=safe, reference="TR-0049", hour=12)
        await self.voucher(date(2026, 9, 10), "CPV", [(A["52010003"], Decimal("6800"), 0, "Shelf labels, bin tags and GRN books"), (A["52010004"], Decimal("2400"), 0, "Tea for the unloading crew")],
                           "Godown petty spending", header=safe, reference="PETTY-HO", hour=16)
        await self.voucher(END, "JV", [(A["52020001"], Decimal("155000"), 0, "Salaries 1 to 15 September"), (A["52030001"], Decimal("23800"), 0, "Electricity 1 to 15 September (meter reading)"),
                                       (A["52010008"], Decimal("25200"), 0, "Depreciation 1 to 15 September"), (A["21040001"], 0, Decimal("155000"), "Owed to staff"),
                                       (A["21040002"], 0, Decimal("23800"), "Electricity owed"), (A["12020001"], 0, Decimal("25200"), "Depreciation")],
                           "Costs to 15 September accrued for the mid-month review", reference="HO-ACCRUAL-2026-09-15", hour=12)
        await self.voucher(date(2026, 8, 31), "BPV", [(A["53010001"], Decimal("1250"), 0, "Account maintenance and cheque book")], "Bank charges for August", header=bank, reference="HO-BANK-AUG", hour=18)

        await accounts_posting_service.run(full=True)

        # numbers in date order
        from app.models import Counter

        from app.models import Transfer

        async def when_it_happened(v) -> datetime:
            """An automatic voucher is stamped with the moment its record happened."""
            kind, _, ref = (v.source or "").partition(":")
            moment = None
            try:
                if kind == "grn":
                    moment = (await GRN.get(id=ref)).at
                elif kind == "transfer-out":
                    moment = (await Transfer.get(id=ref)).dispatched_at
                elif kind.startswith("transfer"):
                    transfer = await Transfer.get(id=ref)
                    moment = transfer.received_at or transfer.dispatched_at
            except Exception:  # noqa: BLE001 — a record that's gone falls back to the day's close
                moment = None
            if moment is None or moment.astimezone(PKT).date() != v.date:
                moment = at(v.date, 23, 30) if v.date < END else NOW - timedelta(minutes=5)
            return min(moment + timedelta(seconds=40), NOW - timedelta(minutes=2))

        stamped = []
        for v in await Voucher.filter(book=HEAD_OFFICE_BOOK):
            stamped.append((v.date, await when_it_happened(v) if v.auto else v.created_at, str(v.id), v))
        stamped.sort(key=lambda s: (s[0], s[1], s[2]))
        per_type = defaultdict(int)
        planned = []
        for _day, moment, _id, v in stamped:
            per_type[v.vtype] += 1
            planned.append((v, f"HO-{v.vtype}-{per_type[v.vtype]:06d}", moment))
        for v, _n, _m in planned:
            await Voucher.filter(id=v.id).update(number=f"TMP-{v.id}"[:30])
        for v, number, moment in planned:
            fields = {"number": number}
            if v.auto:
                fields.update(created_at=moment, posted_at=moment, updated_at=moment)
            await Voucher.filter(id=v.id).update(**fields)
        for vtype, count in per_type.items():
            await Counter.filter(id=f"voucher:{vtype}").delete()
            await Counter.create(id=f"voucher:{vtype}", value=count + 1)
        self.counts["head office vouchers numbered in date order"] = len(planned)

        # August closed
        from app.services import alerts_service
        from app.models import Notice

        row = await vouchers_service.settings()
        row.locked_until = date(2026, 8, 31)
        await row.save()
        notice = await alerts_service.notify("accounts.period", "Closed the books up to 31 Aug 2026", body=f"By {self.accounts_admin.name}", link="/accounts/settings",
                                             audience_any=[("accounts.period", "X"), ("accounts.books", "R")], tone="warning")
        await Notice.filter(id=notice.id).update(at=at(date(2026, 9, 5), 16, 30))
        del GRN

    async def _account(self, account_id: str):
        from app.models import Account

        return await Account.get(id=account_id)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        from app.models import Counter

        if await Counter.exists(id=STORY_KEY):
            raise SystemExit("This database already has head office's story. Restore the backup from before it to run it again.")
        if not args.yes:
            print(__doc__)
            print("  Dry run: nothing written. Stop the Cloud Server, back up cloud.db, then run with --yes.")
            return
        story = Story()
        await story.load()
        for title, step in (("Godown deliveries, one GRN per bill", story.deliveries), ("The purchase orders behind them", story.orders),
                             ("Transfers given the godown's cost", story.transfer_costs), ("Head office's books", story.books)):
            say(f"{title} ...")
            await step()
        await Counter.create(id=STORY_KEY, value=1)
        print("\n  Done:")
        for key, value in story.counts.items():
            print(f"    {key}: {value}")
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
