"""The legacy Purchase Summary Group Wise report for the head office godown: what was bought and sent back, grouped by brand (or any other way
an Item is filed), for a period.

Three layouts, like Multi Soft's:
  summary  one row per group
  detail   one row per group with its Items under it
  item     one row per Item, with its GST rate and average price paid

Figures per row:
  purchase qty / bonus qty    paid-for and free units received
  return qty                  units sent back to suppliers
  net qty                     purchase + bonus - return
  gross purchase              qty x price, before any discount
  discount                    % discount and flat rupee discount on the lines
  charges                     freight and loading added to the lines
  net purchase                gross - discount + charges (what the goods cost, before GST)
  purchase GST                GST on the net purchase at each line's rate
  gross return / net return   goods sent back at the return price; supplier returns carry no discount, so the two
                              are the same figure, kept apart to match the legacy layout
  return GST                  GST the supplier gives back on them
  net amount                  net purchase - net return
  net with GST                net amount + purchase GST - return GST

`approved`: "all" (default), "yes" or "no" narrows the deliveries by their approval. The godown sends nothing
back to suppliers from this system yet, so the return columns stay at zero; the layout matches the branch report.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.models import GRN, GRNLine, Product

ZERO = Decimal("0")
HUNDRED = Decimal("100")

GROUPS = ("brand", "category", "department", "itemClass", "manufacturer", "supplier", "item")
VIEWS = ("summary", "detail", "item")
GROUP_NOUN = {"brand": "brand", "category": "category", "department": "department", "itemClass": "class",
              "manufacturer": "manufacturer", "supplier": "supplier", "item": "Item"}


@dataclass
class Row:
    key: str
    label: str
    grns: set = field(default_factory=set)
    returns: set = field(default_factory=set)
    purchase_qty: Decimal = ZERO
    bonus_qty: Decimal = ZERO
    return_qty: Decimal = ZERO
    gross_purchase: Decimal = ZERO
    discount: Decimal = ZERO
    charges: Decimal = ZERO
    purchase_gst: Decimal = ZERO
    gross_return: Decimal = ZERO
    return_gst: Decimal = ZERO
    rates: set = field(default_factory=set)

    @property
    def net_purchase(self) -> Decimal:
        return self.gross_purchase - self.discount + self.charges

    def add(self, other: "Row") -> None:
        self.grns |= other.grns
        self.returns |= other.returns
        self.rates |= other.rates
        for attr in ("purchase_qty", "bonus_qty", "return_qty", "gross_purchase", "discount", "charges", "purchase_gst", "gross_return", "return_gst"):
            setattr(self, attr, getattr(self, attr) + getattr(other, attr))


def _m(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _q(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.001")).normalize(), "f")


def out(row: Row, with_item: dict | None = None) -> dict:
    net_return = row.gross_return
    net_amount = row.net_purchase - net_return
    received = row.purchase_qty + row.bonus_qty
    data = {
        "key": row.key, "label": row.label, "grns": len(row.grns), "returns": len(row.returns),
        "purchaseQty": _q(row.purchase_qty), "bonusQty": _q(row.bonus_qty), "returnQty": _q(row.return_qty),
        "netQty": _q(received - row.return_qty),
        "grossPurchase": _m(row.gross_purchase), "discount": _m(row.discount), "charges": _m(row.charges),
        "netPurchase": _m(row.net_purchase), "purchaseGst": _m(row.purchase_gst),
        "grossReturn": _m(row.gross_return), "netReturn": _m(net_return), "returnGst": _m(row.return_gst),
        "netAmount": _m(net_amount), "netWithGst": _m(net_amount + row.purchase_gst - row.return_gst),
    }
    if with_item is not None:
        data.update(with_item)
        data["taxRates"] = sorted(format(r.normalize(), "f") for r in row.rates)
        data["avgPrice"] = _m(row.net_purchase / received) if received else None
    return data


def fold(group_by: str, view: str, purchase_lines: list[dict], return_lines: list[dict], products: dict) -> dict:
    """Everything above, from plain dicts, so the head office godown can fold its own deliveries the same way.

    purchase_lines: grn_id, supplier, product_id, qty, bonus_qty, unit_price, disc_percent, flat_disc, misc, tax_rate
    return_lines:   return_id, supplier, product_id, qty, unit_price, tax_rate
    products:       id -> {sku, name, unit, brand, category, department, item_class, manufacturer}

    Supplier grouping uses the supplier on each delivery or return, so an Item bought from two suppliers shows
    under both.
    """
    if group_by not in GROUPS:
        group_by = "brand"
    if view not in VIEWS:
        view = "summary"
    if group_by == "item":
        view = "item"

    def group_of(pid: str, supplier: str | None) -> tuple[str, str]:
        p = products.get(pid) or {}
        if group_by == "supplier":
            return (supplier or "~none"), (supplier or "No supplier on record")
        value = p.get({"itemClass": "item_class"}.get(group_by, group_by))
        return (value or "~none"), (value or f"No {GROUP_NOUN[group_by]}")

    # One row per Item (per group and Item, so a supplier grouping can hold the same Item twice).
    by_item: dict[tuple[str, str], Row] = {}
    group_labels: dict[str, str] = {}

    def item_row(line: dict) -> Row:
        pid = str(line["product_id"])
        gkey, glabel = ("", "") if view == "item" else group_of(pid, line.get("supplier"))
        group_labels[gkey] = glabel
        if (gkey, pid) not in by_item:
            p = products.get(pid) or {}
            by_item[(gkey, pid)] = Row(key=pid, label=p.get("name", pid))
        return by_item[(gkey, pid)]

    for line in purchase_lines:
        row = item_row(line)
        qty, price = Decimal(line["qty"]), Decimal(line["unit_price"])
        gross = qty * price
        after_pct = gross * (1 - Decimal(line.get("disc_percent") or 0) / HUNDRED)
        flat, misc = Decimal(line.get("flat_disc") or 0), Decimal(line.get("misc") or 0)
        net = max(ZERO, after_pct - flat + misc)
        rate = Decimal(line.get("tax_rate") or 0)
        row.grns.add(line["grn_id"])
        row.purchase_qty += qty
        row.bonus_qty += Decimal(line.get("bonus_qty") or 0)
        row.gross_purchase += gross
        row.discount += gross - after_pct + flat
        row.charges += misc
        row.purchase_gst += net * rate / HUNDRED
        row.rates.add(rate)

    for line in return_lines:
        row = item_row(line)
        qty, price = Decimal(line["qty"]), Decimal(line["unit_price"])
        rate = Decimal(line.get("tax_rate") or 0)
        row.returns.add(line["return_id"])
        row.return_qty += qty
        row.gross_return += qty * price
        row.return_gst += qty * price * rate / HUNDRED

    def item_extra(pid: str) -> dict:
        p = products.get(pid) or {}
        return {"productId": pid, "sku": p.get("sku"), "unit": p.get("unit")}

    total = Row(key="total", label="Total")
    for row in by_item.values():
        total.add(row)

    if view == "item":
        ordered = sorted(by_item.items(), key=lambda kv: (-kv[1].net_purchase, kv[1].label))
        rows = [out(r, item_extra(pid)) for (_, pid), r in ordered]
    else:
        groups: dict[str, Row] = {}
        members: dict[str, list[str]] = defaultdict(list)
        for (gkey, pid), row in by_item.items():
            if gkey not in groups:
                groups[gkey] = Row(key=gkey, label=group_labels[gkey])
            groups[gkey].add(row)
            members[gkey].append(pid)
        rows = []
        for gkey, grow in sorted(groups.items(), key=lambda kv: (kv[0] == "~none", -kv[1].net_purchase, kv[1].label)):
            data = out(grow)
            data["items"] = len(members[gkey])
            if view == "detail":
                data["children"] = [
                    {**out(by_item[(gkey, pid)], item_extra(pid)), "key": f"{gkey}::{pid}"}
                    for pid in sorted(members[gkey], key=lambda p: (-by_item[(gkey, p)].net_purchase, by_item[(gkey, p)].label))
                ]
            rows.append(data)

    totals = out(total)
    totals["items"] = len({pid for _, pid in by_item})
    return {"groupBy": group_by, "view": view, "rows": rows, "totals": totals}


async def summary(group_by: str, view: str, from_at: datetime | None, to_at: datetime | None,
                  approved: str = "all", supplier_id: str | None = None) -> dict:
    grns = GRN.all()
    if from_at:
        grns = grns.filter(at__gte=from_at)
    if to_at:
        grns = grns.filter(at__lte=to_at)
    if approved == "yes":
        grns = grns.filter(approved=True)
    elif approved == "no":
        grns = grns.filter(approved=False)
    if supplier_id:
        grns = grns.filter(supplier_id=supplier_id)
    grn_ids = await grns.values_list("id", flat=True)

    purchase_lines = [
        {**l, "supplier": l["grn__supplier__name"]}
        for l in await GRNLine.filter(grn_id__in=list(grn_ids)).values(
            "grn_id", "grn__supplier__name", "product_id", "qty", "bonus_qty", "unit_price", "disc_percent", "tax_rate",
        )
    ] if grn_ids else []

    product_ids = {str(l["product_id"]) for l in purchase_lines}
    products = {str(p["id"]): p for p in await Product.filter(id__in=list(product_ids)).values(
        "id", "sku", "name", "unit", "brand", "category", "department", "item_class", "manufacturer",
    )} if product_ids else {}

    result = fold(group_by, view, purchase_lines, [], products)
    result["approved"] = approved if approved in ("yes", "no") else "all"
    return result
