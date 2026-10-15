"""Head office purchase orders, approval limits and low stock. See models/purchasing.py for the lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tortoise.transactions import atomic

from app.core import dialect
from app.models import Product, PurchaseOrder, PurchaseOrderLine, Supplier, User
from app.services import alerts_service, numbering_service

ZERO = Decimal("0")
OPEN = ("draft", "pending_approval", "approved", "partially_received")
RECEIVABLE = ("approved", "partially_received")

# What each starting role may approve on its own when no limit is set on the person. None = no limit. These are the
# limits until someone saves others in Admin > Company & Settings; call `use_saved_limits()` before reading them.
ROLE_PO_LIMITS: dict[str, Decimal | None] = {"executive": None, "warehouse-manager": Decimal("500000")}

LINK = "/warehouse/purchase-orders"
BUYERS = [("warehouse.purchase-orders", "W")]
APPROVERS = [("warehouse.purchase-orders.approve", "X")]
EXECUTIVE = [("executive.dashboard", "R")]


class PurchasingError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rs(v: Decimal) -> str:
    return f"Rs {v.quantize(Decimal('1')):,}"


def money(v: Decimal | None) -> str | None:
    """Plain rupees with paisa for the wire: 1500.00, never 1.5E+3."""
    return None if v is None else format(Decimal(v).quantize(Decimal("0.01")), "f")


async def use_saved_limits() -> None:
    from app.services import office_settings_service

    await office_settings_service.warm()


def po_limit_of(user: User) -> Decimal | None:
    """The largest order this person may approve. None means no limit."""
    if user.po_limit is not None:
        return user.po_limit
    return ROLE_PO_LIMITS.get(user.role_id, ZERO)


def covers(user: User, total: Decimal) -> bool:
    limit = po_limit_of(user)
    return limit is None or total <= limit


async def order_counts() -> dict[str, int]:
    """How many orders each of the page's filters would show.

    The Purchase Orders page opens on Open, which is `OPEN` -- draft, pending approval, approved, partly
    received. On the Multi-Tec history that is 61 orders of 859, every one of them a draft, and a draft has
    nothing received against it by definition: `receive_against` refuses an order that is not approved, and the
    data agrees, 0 of 1,583 draft lines. So the page opens on a list where the Received column reads "0 of N" on
    every row, and nothing on screen says that the 263 received and 535 closed orders are one click away under
    All. It looks like the figure is broken. It is the filter.

    Counting all three in one pass so the page can label them, rather than making it guess or fetch three times.
    """
    from tortoise.functions import Count

    rows = await PurchaseOrder.all().annotate(n=Count("id")).group_by("status").values("status", "n")
    by_status = {str(r["status"]): int(r["n"]) for r in rows}
    return {
        "open": sum(by_status.get(s, 0) for s in OPEN),
        "pending_approval": by_status.get("pending_approval", 0),
        "all": sum(by_status.values()),
    }


# The most orders one request will hand back. A ceiling rather than a page size: the caller says how many it
# wants and this stops a mistake asking for all of them, since every row prefetches its lines, its supplier and
# two users. It was 200 and the page asked for exactly 200, so All read "861" above a table holding 200 of them,
# and the 661 oldest could not be reached by searching, sorting or scrolling. The page now pages properly, so
# this is only the backstop; raised anyway, because a backstop that equals the page size is indistinguishable
# from a cap.
MAX_ORDERS_PER_REQUEST = 500


async def scope_of(user: User | None) -> str | None:
    """The one branch this person may buy for, or None for everybody who buys for the company.

    Read through `po_branch_id` rather than the relation, so asking costs nothing: this is consulted on every
    list and every create, and the branch row itself is never needed, only its id.
    """
    return str(user.po_branch_id) if user is not None and user.po_branch_id else None


async def list_orders(status: str | None, q: str | None, limit: int, offset: int,
                      branch: str | None = None, user: User | None = None) -> tuple[list[PurchaseOrder], int]:
    """`branch` narrows the list to the orders raised FOR that branch; "godown" narrows it to the orders that are
    for no branch, which is every ordinary central order.

    A user scoped to one branch (`users.po_branch`) sees that branch's orders and nothing else, whatever `branch`
    asks for. That is not a convenience: the whole point of the scope is that the person holding it cannot act
    for another branch, and a list is where they would otherwise see one.
    """
    qs = PurchaseOrder.all()
    from tortoise.expressions import Q, Subquery

    # AN ORDER IS "FOR" A BRANCH IF ITS HEADER SAYS SO OR ANY OF ITS LINES DOES
    #
    # Matching the header alone would miss the ordinary case. The way these shops buy is one order of 100 split 40
    # to one branch, 40 to another and 20 kept: the header names no branch and three lines name two, so a
    # header-only filter finds that order under "Godown" and under neither branch, which is the opposite of useful.
    #
    # A SUBQUERY AND NOT A JOIN, which is not a style preference. `qs.filter(lines__...)` joins the lines table and
    # returns the order once per matching line, and `.count()` then counts those rows however much `.distinct()`
    # is added: asked for the godown's orders it answered 17,875 instead of 881, because each order has about
    # twenty lines. `total` is what a pager divides by, so it would have offered 358 pages of 881 orders. The same
    # Subquery shape `vouchers_service` already uses, and it also avoids naming ids as parameters, which is what
    # the 2,100 parameter ceiling punishes.
    def for_branch(branch_id: str):
        return Q(for_branch_id=branch_id) | Q(id__in=Subquery(
            PurchaseOrderLine.filter(for_branch_id=branch_id).values("purchase_order_id")))

    mine = await scope_of(user)
    if mine:
        qs = qs.filter(for_branch(mine))
    elif branch == "godown":
        # Deliberately a word and not an empty string: a caller sending `branch=` by accident means "no filter",
        # and reading that as "only the ones for nobody" would quietly hide every branch order. "Godown" means
        # nothing on this order is for a branch -- not the kept remainder of a split order, which belongs with
        # the branches it was bought alongside.
        qs = qs.filter(for_branch_id=None).exclude(id__in=Subquery(
            PurchaseOrderLine.filter(for_branch_id__not_isnull=True).values("purchase_order_id")))
    elif branch:
        qs = qs.filter(for_branch(branch))
    if status == "open":
        qs = qs.filter(status__in=list(OPEN))
    elif status:
        qs = qs.filter(status=status)
    if q and q.strip():
        from tortoise.expressions import Q

        qs = qs.filter(Q(po_number__icontains=q.strip()) | Q(supplier__name__icontains=q.strip()))
    total = await qs.count()
    rows = await qs.order_by("-raised_at", "id").offset(max(offset, 0)).limit(min(max(limit, 1), MAX_ORDERS_PER_REQUEST)).prefetch_related(
        # for_branch too: `routes/purchasing._out` reads its code and name, and an un-prefetched relation is not
        # the object -- it is a queryset, which is truthy, so the guard would pass and the attribute would fail.
        "lines__for_branch", "supplier", "raised_by", "approved_by", "for_branch",
    )
    return rows, total


async def branch_split(po: PurchaseOrder) -> list[dict]:
    """What each branch's share of one order is, biggest first, with the godown's own share last.

    This is the answer to "who is this order for" in the one form that survives a split: a list, because one order
    routinely buys for several branches at once. An order wholly for the godown comes back empty rather than with
    a single godown row, so a screen can tell "central" from "split" without arithmetic.

    `po.lines` has to be prefetched with `for_branch`, which `list_orders` and `get_order` both do.
    """
    by: dict[str | None, dict] = {}
    for line in po.lines:
        key = str(line.for_branch_id) if line.for_branch_id else None
        row = by.setdefault(key, {
            "branchId": key,
            "branchCode": line.for_branch.code if line.for_branch_id and line.for_branch else None,
            "branchName": line.for_branch.name if line.for_branch_id and line.for_branch else None,
            "qty": ZERO, "value": ZERO, "lines": 0,
        })
        row["qty"] += line.qty
        row["value"] += line.qty * line.unit_cost
        row["lines"] += 1
    if set(by) == {None}:
        return []
    # Biggest share first, and the godown's own remainder last whatever its size: it is the leftover, not a
    # destination, so it reads wrongly at the top of a list of destinations.
    return sorted(by.values(), key=lambda r: (r["branchId"] is None, -r["value"], r["branchCode"] or ""))


async def get_order(order_id: str) -> PurchaseOrder:
    po = await PurchaseOrder.get_or_none(id=order_id).prefetch_related(
        "lines__for_branch", "supplier", "raised_by", "approved_by", "for_branch")
    if not po:
        raise PurchasingError("That purchase order doesn't exist.", 404)
    return po


async def _set_lines(po: PurchaseOrder, lines) -> None:
    """The order's lines. Each is `(productId, qty, unitCost)` or `(productId, qty, unitCost, forBranchId)`.

    THE SAME ITEM MAY APPEAR ONCE PER BRANCH, AND ONLY ONCE PER BRANCH
    ------------------------------------------------------------------
    This refused an Item twice outright -- "put the whole quantity on one line" -- which was right while an order
    was only ever for the godown. It is wrong now: the way these shops buy is 100 of something, 40 for one branch,
    40 for another, 20 kept, and that is three lines of one Item. So the rule is per Item AND branch, which still
    catches the mistake it was written for (the same Item for the same branch on two lines) and allows the split.

    A null branch is the godown's own stock and is one of the possibilities, so "40 FC, 40 GT, 20 godown" is three
    lines and a second godown line is still refused.
    """
    if not lines:
        raise PurchasingError("Add at least one Item to order.")
    from app.models.branch import selling_branches

    scoped = str(po.for_branch_id) if po.for_branch_id else None
    seen: set[tuple[str, str | None]] = set()
    total = ZERO
    await PurchaseOrderLine.filter(purchase_order=po).delete()
    for position, line in enumerate(lines):
        product_id, qty, unit_cost = line[0], line[1], line[2]
        for_branch = (str(line[3]).strip() or None) if len(line) > 3 and line[3] else None
        product = await Product.get_or_none(id=product_id)
        if not product:
            raise PurchasingError(f"Unknown Item {product_id}")
        if (product_id, for_branch) in seen:
            where = "for the same branch" if for_branch else "for the godown"
            raise PurchasingError(f"{product.name} is on the order twice {where}. Put that quantity on one line.")
        seen.add((product_id, for_branch))
        if qty <= ZERO:
            raise PurchasingError(f"{product.name}: order a quantity above zero.")
        if unit_cost < ZERO:
            raise PurchasingError(f"{product.name}: the cost can't be negative.")
        if for_branch:
            # An order wholly for one branch cannot have a line for another: the header says who it is for, and
            # a line disagreeing with it would make the order mean two things at once.
            if scoped and for_branch != scoped:
                raise PurchasingError(f"{product.name}: this order is for one branch, so its lines cannot name "
                                      f"another.")
            if not await selling_branches().filter(id=for_branch).exists():
                raise PurchasingError(f"{product.name}: that branch doesn't exist, or it is head office, which is "
                                      f"the godown itself.", 404)
        await PurchaseOrderLine.create(purchase_order=po, product=product, qty=qty, unit_cost=unit_cost,
                                       for_branch_id=for_branch, position=position)
        total += qty * unit_cost
    po.total = total.quantize(Decimal("0.01"))


@atomic()
async def create_order(user: User, supplier_id: str, lines, expected_at, notes: str | None, reason: str | None,
                       for_branch: str | None = None) -> PurchaseOrder:
    supplier = await Supplier.get_or_none(id=supplier_id)
    if not supplier:
        raise PurchasingError("Pick a supplier.")
    # WHO THE ORDER IS FOR, AND WHO MAY SAY
    #
    # A scoped user's own branch wins outright, and asking for another is refused rather than quietly corrected:
    # they are meant to be unable to buy for anywhere else, and silently rewriting the request would make a
    # screen that looks like it worked. Everybody else may name a branch or leave it out for the godown.
    mine = await scope_of(user)
    wanted = (for_branch or "").strip() or None
    if mine:
        if wanted and wanted != mine:
            raise PurchasingError("This account can only raise orders for its own branch.", 403)
        wanted = mine
    if wanted:
        from app.models.branch import selling_branches

        if not await selling_branches().filter(id=wanted).exists():
            raise PurchasingError("That branch doesn't exist, or it is head office, which is the godown.", 404)
    number = await numbering_service.next_number("warehouse_po", PurchaseOrder, "po_number", "WPO-", 4)
    po = await PurchaseOrder.create(
        po_number=number, supplier=supplier, status="draft", expected_at=expected_at,
        notes=(notes or "").strip() or None, reason=(reason or "").strip()[:120] or None, raised_by=user,
        for_branch_id=wanted,
        # `raised_at` stopped filling itself when the Multi-Tec import needed to carry their own order dates, which
        # run back to 1 September 2024. Passed here so an order raised on this screen is still stamped now.
        raised_at=datetime.now(timezone.utc),
    )
    await _set_lines(po, lines)
    await po.save()
    return await get_order(str(po.id))


@atomic()
async def update_order(user: User, order_id: str, supplier_id: str, lines, expected_at, notes: str | None, reason: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status not in ("draft", "rejected"):
        raise PurchasingError(f"{po.po_number} can't be changed now because it's {po.status.replace('_', ' ')}.")
    if po.raised_by_id != user.id:
        raise PurchasingError("Only the person who raised an order changes it.", 403)
    supplier = await Supplier.get_or_none(id=supplier_id)
    if not supplier:
        raise PurchasingError("Pick a supplier.")
    po.supplier = supplier
    po.expected_at = expected_at
    po.notes = (notes or "").strip() or None
    po.reason = (reason or "").strip()[:120] or None
    po.status = "draft"
    await _set_lines(po, lines)
    await po.save()
    return await get_order(order_id)


@atomic()
async def submit_order(user: User, order_id: str) -> PurchaseOrder:
    """Within the raiser's own limit it's approved there and then, and the Executive is told; above it, it waits
    for someone whose limit covers it."""
    from app.services.rbac_service import has_permission

    await use_saved_limits()
    po = await get_order(order_id)
    if po.status not in ("draft", "rejected"):
        raise PurchasingError(f"{po.po_number} was already submitted.")
    if po.raised_by_id != user.id:
        raise PurchasingError("Only the person who raised an order submits it.", 403)
    now = _now()
    po.submitted_at = now
    po.decision_note = None
    subject = ("purchase-order", str(po.id))
    if await has_permission(user, "warehouse.purchase-orders.approve", "X") and covers(user, po.total):
        po.status, po.approved_by, po.approved_at, po.auto_approved = "approved", user, now, True
        await po.save()
        await alerts_service.notify(
            "po.approved", f"{po.po_number} to {po.supplier.name} approved ({rs(po.total)})",
            body=f"Within {user.name}'s own approval limit.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE, subject=subject, tone="good",
        )
    else:
        po.status, po.auto_approved = "pending_approval", False
        await po.save()
        await alerts_service.notify(
            "po.submitted", f"{po.po_number} to {po.supplier.name} waits for approval ({rs(po.total)})",
            body=f"Raised by {user.name}.{f' Reason: {po.reason}.' if po.reason else ''}", link=LINK,
            audience_any=EXECUTIVE + APPROVERS, subject=subject,
        )
    return await get_order(order_id)


@atomic()
async def approve_order(user: User, order_id: str) -> PurchaseOrder:
    await use_saved_limits()
    po = await get_order(order_id)
    if po.status != "pending_approval":
        raise PurchasingError(f"{po.po_number} isn't waiting for approval.")
    if po.raised_by_id == user.id:
        raise PurchasingError("Nobody approves their own order. It's above your limit, so someone else decides.", 403)
    if not covers(user, po.total):
        raise PurchasingError(
            f"Your purchase approval limit is {rs(po_limit_of(user))}, but {po.po_number} is {rs(po.total)}. Someone with a higher limit approves it.", 403,
        )
    po.status, po.approved_by, po.approved_at = "approved", user, _now()
    await po.save()
    await alerts_service.notify(
        "po.approved", f"{po.po_number} to {po.supplier.name} approved by {user.name}", body=f"{rs(po.total)}. Send it to the supplier.",
        link=LINK, audience_any=BUYERS + EXECUTIVE, users=[str(po.raised_by_id)], subject=("purchase-order", str(po.id)), tone="good",
    )
    return await get_order(order_id)


@atomic()
async def reject_order(user: User, order_id: str, reason: str | None) -> PurchaseOrder:
    if not (reason or "").strip():
        raise PurchasingError("Say why it's rejected, so it can be changed.")
    po = await get_order(order_id)
    if po.status != "pending_approval":
        raise PurchasingError(f"{po.po_number} isn't waiting for approval.")
    if po.raised_by_id == user.id:
        raise PurchasingError("Cancel your own order instead of rejecting it.", 403)
    po.status, po.decision_note = "rejected", reason.strip()[:255]
    await po.save()
    await alerts_service.notify(
        "po.rejected", f"{po.po_number} to {po.supplier.name} rejected by {user.name}", body=po.decision_note,
        link=LINK, users=[str(po.raised_by_id)], audience_any=EXECUTIVE, subject=("purchase-order", str(po.id)), tone="bad",
    )
    return await get_order(order_id)


@atomic()
async def cancel_order(user: User, order_id: str, reason: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status not in ("draft", "pending_approval", "rejected", "approved"):
        raise PurchasingError(f"{po.po_number} can't be cancelled because it's {po.status.replace('_', ' ')}. Close it instead.")
    if any(l.received_qty > ZERO for l in po.lines):
        raise PurchasingError(f"Some of {po.po_number} has been received. Close it instead.")
    was = po.status
    po.status, po.decision_note, po.closed_at = "cancelled", (reason or "").strip()[:255] or None, _now()
    await po.save()
    if was in ("pending_approval", "approved"):
        await alerts_service.notify(
            "po.cancelled", f"{po.po_number} to {po.supplier.name} cancelled by {user.name}", body=po.decision_note,
            link=LINK, audience_any=BUYERS + EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning",
        )
    return await get_order(order_id)


@atomic()
async def close_order(user: User, order_id: str, note: str | None) -> PurchaseOrder:
    po = await get_order(order_id)
    if po.status != "partially_received":
        raise PurchasingError("Only a partly received order is closed. Cancel one that has nothing received.")
    po.status, po.decision_note, po.closed_at = "closed", (note or "").strip()[:255] or "The rest won't come.", _now()
    await po.save()
    await alerts_service.notify(
        "po.closed", f"{po.po_number} closed short by {user.name}", body=po.decision_note, link=LINK,
        audience_any=EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning",
    )
    return await get_order(order_id)


async def receive_against(po_id: str, supplier_id: str, received: list[tuple[str, Decimal, Decimal]],
                          grn_number: str) -> tuple[list[str], dict[str, dict[str, Decimal]]]:
    """Tick off what arrived on a GRN against its order.

    Returns what did not match, in words, for the notice; and what each branch's share of the delivery was, as
    `{branch_id: {product_id: qty}}`, with the godown's own share left out. `warehouse_service.receive_grn` turns
    that into a transfer per branch.

    HOW A DELIVERY IS ALLOCATED, NOW THAT ONE ITEM CAN BE ON SEVERAL LINES
    ----------------------------------------------------------------------
    This kept one line per product -- `{str(l.product_id): l}` -- which was right while an order was wholly for
    the godown. It is wrong now: the way these shops buy is 100 of something, 40 for one branch, 40 for another,
    20 kept, which is three lines of one Item. That dict kept whichever came last, so receiving 100 ticked off one
    line, left two untouched, and reported the order part-received for ever.

    So a product's lines are filled **in the order they sit on the order**, each up to what it still needs:
    receive 50 of the three above and FC gets 40, GT gets 10, the godown none. It is the rule the person
    receiving would apply with a pen, and it is predictable, which matters because they already know what is
    going where -- this is a record of it, not a decision about it.

    Anything beyond what the whole order still needs **stays in the godown**: a surplus nobody ordered for a
    branch is not pushed at one. It is still reported as a mismatch, as it always was.
    """
    po = await get_order(po_id)
    if po.status not in RECEIVABLE:
        raise PurchasingError(f"{po.po_number} isn't approved, so nothing can be received against it.")
    if str(po.supplier_id) != str(supplier_id):
        raise PurchasingError(f"{po.po_number} is from {po.supplier.name}, so pick that supplier on the GRN.")
    by_product: dict[str, list] = {}
    for line in sorted(po.lines, key=lambda l: l.position):
        by_product.setdefault(str(line.product_id), []).append(line)
    names = {str(p.id): p.name for p in await Product.filter(id__in=[pid for pid, _, _ in received])}
    mismatches: list[str] = []
    for_branch: dict[str, dict[str, Decimal]] = {}
    for product_id, qty, unit_price in received:
        lines = by_product.get(product_id)
        if not lines:
            raise PurchasingError(f"{names.get(product_id, product_id)} isn't on {po.po_number}. Receive it on a separate GRN.")
        still_due = sum((max(l.qty - l.received_qty, ZERO) for l in lines), ZERO)
        if qty > still_due:
            mismatches.append(f"{names.get(product_id)}: {qty.normalize():f} received, {still_due.normalize():f} was still due")
        if any(unit_price != l.unit_cost for l in lines):
            ordered = ", ".join(sorted({rs(l.unit_cost) for l in lines}))
            mismatches.append(f"{names.get(product_id)}: charged {rs(unit_price)} a unit, ordered at {ordered}")
        left = qty
        for line in lines:
            if left <= ZERO:
                break
            takes = min(left, max(line.qty - line.received_qty, ZERO))
            if takes <= ZERO:
                continue
            line.received_qty = line.received_qty + takes
            await line.save(update_fields=["received_qty"])
            left -= takes
            if line.for_branch_id:
                share = for_branch.setdefault(str(line.for_branch_id), {})
                share[product_id] = share.get(product_id, ZERO) + takes
        # Whatever is left over was not ordered. It is received into the godown and nowhere else; the mismatch
        # above already says so, and the last line keeps the count so the order's arithmetic still adds up.
        if left > ZERO:
            last = lines[-1]
            last.received_qty = last.received_qty + left
            await last.save(update_fields=["received_qty"])
    all_in = all(l.received_qty >= l.qty for l in await PurchaseOrderLine.filter(purchase_order=po))
    po.status = "received" if all_in else "partially_received"
    if all_in:
        po.closed_at = _now()
    await po.save()
    await alerts_service.notify(
        "po.received", f"{grn_number} received against {po.po_number} from {po.supplier.name}"
        + (", but it doesn't match the order" if mismatches else ""),
        body="; ".join(mismatches) if mismatches else ("Everything ordered has arrived." if all_in else "Part of the order has arrived."),
        link=LINK, audience_any=BUYERS + EXECUTIVE, subject=("purchase-order", str(po.id)), tone="warning" if mismatches else "good",
    )
    return mismatches, for_branch


# How soon counts as running out. A week is what the godown's own "running low" uses (SHORT_DAYS), and the point
# of saying it again here is that this one is measured per branch rather than over all of them together.
RUNNING_OUT_DAYS = 7


async def branches_running_out(within_days: int = RUNNING_OUT_DAYS, limit: int = 200) -> list[dict]:
    """Per branch and per Item: what runs out within `within_days` at that branch's own rate, soonest first.

    WHY THE LOW STOCK TILE DOES NOT ALREADY ANSWER THIS
    ---------------------------------------------------
    That tile counts `BranchStockAlert` rows, which a branch raises against a fixed level: below 20, say. A level
    is not comparable between Items. A line selling 180 a day with a level of 50 is in trouble at 60 and the tile
    says nothing; one selling 2 a week with a level of 50 is fine at 40 and the tile shouts. Days of cover is the
    figure that compares, and it is the same one the per-branch suggestion and the order gate are built on.

    It is also the difference between knowing and being told. Measured 13 October 2026: head office could look up
    that Fort Colony had 3.4 days of Panadol left while Garden Town had 63.4, but nothing said so, so the order
    was raised when a shelf was empty rather than before.

    Stock already on its way to a branch counts, or a branch would be reported as running out of something that
    is on the van.
    """
    rates = await _branch_sales_rates_by_branch(NO_IDS)
    held = await _branches_hold_by_branch(NO_IDS)
    coming = await _heading_to_branches(NO_IDS)
    if not rates:
        return []

    from app.models.branch import selling_branches

    who_is = {str(b["id"]): b for b in await selling_branches().values("id", "code", "name")}
    names, ids_of = {}, {}
    for pid, sku, name in await Product.filter(active=True).values_list("id", "sku", "name"):
        names[sku], ids_of[sku] = name, str(pid)

    out = []
    for sku, per_branch in rates.items():
        if sku not in names:
            continue
        pid = ids_of[sku]
        for branch_id, rate in per_branch.items():
            if rate <= 0 or branch_id not in who_is:
                continue
            on_shelf = held.get(sku, {}).get(branch_id, ZERO) + coming.get(pid, {}).get(branch_id, ZERO)
            # RUNNING OUT MEANS IT HAS STOCK THAT WILL BE GONE, NOT THAT IT HAS NONE
            #
            # Without this the alert was 200 lines of one branch's dead stock. An Item with nothing on the shelf
            # divides to 0.0 days left however slowly it sells, so a line selling one unit a month sorted above
            # a line selling 180 a day with two days left, and filled the whole list. Measured 13 October 2026:
            # every one of the 200 it returned was Fort Colony, and the one it led with sold 0.0 a day.
            #
            # An empty shelf is already the Out of Stock tile's business. This one is for the shelf about to
            # empty, which is the only kind anybody can still act on.
            if on_shelf <= 0:
                continue
            # And a line the branch sells less than one of over the whole window is not news either: it would
            # take weeks to clear one unit, so "runs out in six days" is arithmetic rather than a warning.
            if rate * Decimal(within_days) < 1:
                continue
            days = on_shelf / rate
            if days > within_days:
                continue
            out.append({
                "productId": pid, "sku": sku, "name": names[sku],
                "branchId": branch_id, "branchCode": who_is[branch_id]["code"],
                "branchName": who_is[branch_id]["name"],
                "ratePerDay": rate, "held": on_shelf, "daysLeft": days,
                "suggestedQty": round_up(rate * Decimal(within_days * 2) - on_shelf, None, False)[0],
            })
    # Soonest first, then the branch and the code, so an unchanged database gives the same order twice.
    out.sort(key=lambda r: (r["daysLeft"], r["branchCode"], r["sku"]))
    # Fairly, not just soonest: a global cut keeps one branch's worst 200 and never mentions the other three, so
    # the branch with the most trouble hides the ones with any. Each branch keeps its own soonest lines, in the
    # same order, and the whole is still soonest-first.
    per_branch_cap = max(1, min(limit, 2000) // max(len(who_is), 1))
    kept: dict[str, int] = {}
    trimmed = []
    for row in out:
        if kept.get(row["branchCode"], 0) >= per_branch_cap:
            continue
        kept[row["branchCode"]] = kept.get(row["branchCode"], 0) + 1
        trimmed.append(row)
    return trimmed


async def low_stock() -> list[dict]:
    """Godown Items at or below their reorder level with nothing already on order."""
    from app.services import warehouse_service

    watched = await Product.filter(reorder_level__gt=0, active=True)
    if not watched:
        return []
    on_hand: dict[str, Decimal] = {}
    for row in await warehouse_service.all_balances():
        on_hand[row["product_id"]] = on_hand.get(row["product_id"], ZERO) + Decimal(str(row["total"]))
    on_order: dict[str, Decimal] = {}
    for line in await PurchaseOrderLine.filter(purchase_order__status__in=list(OPEN)):
        on_order[str(line.product_id)] = on_order.get(str(line.product_id), ZERO) + max(line.qty - line.received_qty, ZERO)
    out = []
    for p in watched:
        held = on_hand.get(str(p.id), ZERO)
        if held <= p.reorder_level and on_order.get(str(p.id), ZERO) <= ZERO:
            out.append({"productId": str(p.id), "sku": p.sku, "name": p.name, "onHand": held, "reorderLevel": p.reorder_level, "avgCost": p.avg_cost})
    return sorted(out, key=lambda r: (r["onHand"] - r["reorderLevel"], r["name"]))


# ── suggestions: what to put on an order, by plain rules ─────────────────────────────────────────
#
# Quantity to order = what the branches sell in the cover days (from their last 30 days of sales), or the Item's reorder
# level if that is higher, less what the godown holds and what is already on open orders, rounded up to whole packs.
# Choosing a supplier suggests the Items bought from them before (earlier orders, GRNs and the Item's supplier list);
# without a supplier, the Items running low.

SALES_WINDOW_DAYS = 30
SHORT_DAYS = 7
MAX_SUGGESTIONS = 300


def _num(v: Decimal) -> str:
    """A quantity in words: 12, 1.5, never 12.000."""
    v = Decimal(v)
    if v == v.to_integral_value():
        return f"{int(v):,}"
    return f"{v.quantize(Decimal('0.1')).normalize():f}"


def rate_words(rate: Decimal, who: str = "sells") -> str:
    """How fast an Item goes, the way a person would say it."""
    if rate <= 0:
        return f"no sales in the last {SALES_WINDOW_DAYS} days"
    if rate >= 1:
        return f"{who} {_num(rate if rate >= 10 else rate.quantize(Decimal('0.1')))} a day"
    week = rate * 7
    if week >= 1:
        return f"{who} about {_num(week.quantize(Decimal('0.1')) if week < 10 else week.quantize(Decimal('1')))} a week"
    return f"{who} about {_num(max(Decimal('1'), (rate * 30).quantize(Decimal('1'))))} a month"


def round_up(need: Decimal, pack_size: int | None, weighed: bool = False) -> tuple[Decimal, str | None]:
    """Round what is needed up to what can be bought: whole packs when the pack size is known, whole units otherwise
    (half a unit for a weighed Item). Gives the quantity and, when packs changed it, how."""
    import math

    if need <= 0:
        return ZERO, None
    if pack_size and pack_size > 1:
        packs = math.ceil(need / Decimal(pack_size))
        qty = Decimal(packs * pack_size)
        return qty, (f"rounded up to {packs} full pack{'s' if packs != 1 else ''} of {pack_size}" if qty != need else None)
    if weighed:
        return Decimal(math.ceil(need * 2)) / 2, None
    return Decimal(math.ceil(need)), None


def suggest_line(rate: Decimal, held: Decimal, on_order: Decimal, reorder_level: Decimal | None, cover_days: int,
                 pack_size: int | None = None, weighed: bool = False, who: str = "sells", holder: str = "holds") -> dict:
    """The suggested quantity for one Item and the reasoning in plain words."""
    by_sales = rate * cover_days
    level = reorder_level if reorder_level and reorder_level > 0 else ZERO
    target = max(by_sales, level)
    need = target - held - on_order
    qty, packed = round_up(need, pack_size, weighed)
    parts = [rate_words(rate, who), f"{holder} {_num(held)}"]
    if on_order > 0:
        parts.append(f"{_num(on_order)} already on order")
    if rate > 0:
        parts.append(f"{cover_days} days of cover needs {_num(by_sales.quantize(Decimal('1')) if by_sales >= 10 else by_sales.quantize(Decimal('0.1')))}")
    if level > by_sales:
        parts.append(f"keeps at least its reorder level of {_num(level)}")
    if qty > 0:
        parts.append(f"so order {_num(qty)}" + (f" ({packed})" if packed else ""))
    elif target <= 0:
        parts.append("so nothing is suggested")
    else:
        parts.append("enough for now")
    return {"rate": rate, "target": target, "qty": qty, "reason": ", ".join(parts)}


# ── which Items a helper is being asked about ────────────────────────────────────────────────────
#
# Every helper below answers "for these Items". With a supplier that is the handful bought from them, which is short
# enough to name as parameters. Without one it is every active Item, and here that is 64,589 of them.
#
# Naming those is what made `/warehouse/purchase-orders/suggestions` fail outright with a 500: pyodbc reported "The
# SQL contains -943 parameter markers, but 64593 parameters were supplied" -- SQL Server takes 2,100, and the count
# had wrapped a signed 16-bit field, so the error named neither the limit nor the cause. `active` is a column on
# products, so `None` here means every active Item and the database does that filtering itself: one pass over each
# table instead of 130 chunks of 500, and no parameters at all.
NO_IDS = None


def _item_scope(column: str, values: list[str] | None, on: str = "id") -> tuple[str, list]:
    """A condition on the caller's product column, and the parameters it binds.

    `values is None` is every active Item, as a subquery on products rather than a parameter list. A list is named,
    and the caller keeps it under the ceiling by chunking, the way it always did.
    """
    if values is None:
        return f"{column} IN (SELECT p.{on} FROM products p WHERE p.active = 1)", []
    return f"{column} IN ({','.join('?' * len(values))})", list(values)


def _chunks(values: list[str] | None, size: int = 500) -> list[list[str] | None]:
    """The passes a helper has to make: one per chunk of named ids, or a single pass for every active Item."""
    return [None] if values is None else [values[at:at + size] for at in range(0, len(values), size)]


def _totalled(by_branch: dict[str, dict[str, Decimal]]) -> dict[str, Decimal]:
    """Add each code's branches up, in the order the query returned them.

    The order is not decoration. These values come out of a division, Decimal division rounds, and the same
    rounded numbers added in a different order give different last digits -- which is the whole reason the query
    behind this carries an ORDER BY. A dict keeps its insertion order and the rows were inserted in the query's
    order, so totalling here reproduces exactly what accumulating inside the loop used to.
    """
    return {code: sum(per.values(), ZERO) for code, per in by_branch.items()}


async def _branch_sales_rates_by_branch(skus: list[str] | None) -> dict[str, dict[str, Decimal]]:
    """Units a day each branch sells of each code: that branch's sales over the last 30 days it reported, divided
    by the days it reported. Keyed code, then branch.

    This is the figure the per-branch suggestion is built on, and it is the same one the godown suggestion has
    always used -- `_branch_sales_rates` adds these branches together at the last step. Keeping them apart here
    is the whole of "suggest per branch": no new feed, no new query, one aggregation moved later.

    `skus=None` is every active Item. That used to be 130 chunked queries over all 1,838,739 rows of
    branch_product_stats, each one re-reading the same 30 day window; it is now one.
    """
    from datetime import timedelta

    from tortoise import Tortoise

    from app.core.pk_time import today_pk

    if skus is not None and not skus:
        return {}
    since = str(today_pk() - timedelta(days=SALES_WINDOW_DAYS))
    conn = Tortoise.get_connection("default")
    days = {str(r["branch_id"]): int(r["days"] or 0) for r in await conn.execute_query_dict(
        "SELECT branch_id, COUNT(DISTINCT day) AS days FROM branch_daily_stats WHERE day > ? GROUP BY branch_id", [since])}
    rates: dict[str, dict[str, Decimal]] = {}
    for chunk in _chunks(skus):
        where, params = _item_scope("product_sku", chunk, on="sku")
        for r in await conn.execute_query_dict(
            f"SELECT branch_id, product_sku, SUM({dialect.exact_number('qty')}) AS qty FROM branch_product_stats "
            # ORDER BY is not decoration here. Each row is divided by its branch's day count and then added to a
            # running total per sku, and Decimal addition rounds, so adding the same numbers in a different
            # order gives different last digits. Without an order SQL Server returns the groups however
            # parallelism happens to finish, so the rate moved between runs and the suggestions list
            # re-sorted itself: 11 to 17 of 1,103 lines changed places on an unchanged database.
            f"WHERE day > ? AND {where} GROUP BY branch_id, product_sku "
            f"ORDER BY product_sku, branch_id", [since, *params],
        ):
            d = days.get(str(r["branch_id"]), 0)
            if d > 0 and (r["qty"] or 0) > 0:
                rates.setdefault(r["product_sku"], {})[str(r["branch_id"])] = Decimal(str(r["qty"])) / d
    return rates


async def _branch_sales_rates(skus: list[str] | None) -> dict[str, Decimal]:
    """Units a day the branches sell of each code, together. The godown's question: it buys for all of them."""
    return _totalled(await _branch_sales_rates_by_branch(skus))


async def _dispatch_rates(product_ids: list[str] | None) -> dict[str, Decimal]:
    """Units a day the godown sends out to branches of each Item: its last 30 days of dispatches over those days (fewer
    for a godown that started keeping stock more recently).

    `product_ids=None` is every active Item, in one pass (see `_item_scope`).
    """
    from datetime import timedelta

    from tortoise import Tortoise

    from app.core.pk_time import day_start, today_pk

    if product_ids is not None and not product_ids:
        return {}
    conn = Tortoise.get_connection("default")
    first = await conn.execute_query_dict("SELECT MIN(at) AS first FROM warehouse_stock_movements", [])
    if not first or not first[0]["first"]:
        return {}
    try:
        from app.core.pk_time import pk_day

        began = pk_day(datetime.fromisoformat(str(first[0]["first"]).replace("Z", "+00:00")))
    except ValueError:
        began = today_pk() - timedelta(days=SALES_WINDOW_DAYS)
    start = max(today_pk() - timedelta(days=SALES_WINDOW_DAYS - 1), began)
    days = max((today_pk() - start).days + 1, 1)
    since = day_start(start).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, Decimal] = {}
    for chunk in _chunks(product_ids):
        where, params = _item_scope("product_id", chunk)
        for r in await conn.execute_query_dict(
            f"SELECT product_id, -SUM({dialect.exact_number('qty')}) AS sent FROM warehouse_stock_movements "
            f"WHERE kind = 'dispatch' AND at >= ? AND {where} GROUP BY product_id", [since, *params],
        ):
            if (r["sent"] or 0) > 0:
                out[str(r["product_id"])] = Decimal(str(r["sent"])) / days
    return out


async def _held_in_godown(product_ids: list[str] | None) -> dict[str, Decimal]:
    """What the godown holds of each Item: its movements folded, stock never being stored (models/warehouse.py).

    `product_ids=None` is every active Item in one GROUP BY. It was 130 queries of 500 ids, which is the same scan
    of warehouse_stock_movements 130 times over.
    """
    from tortoise import Tortoise

    if product_ids is not None and not product_ids:
        return {}
    conn = Tortoise.get_connection("default")
    held: dict[str, Decimal] = {}
    for chunk in _chunks(product_ids):
        where, params = _item_scope("product_id", chunk)
        for r in await conn.execute_query_dict(
            f"SELECT product_id, SUM(qty) AS total FROM warehouse_stock_movements WHERE {where} GROUP BY product_id", params,
        ):
            held[str(r["product_id"])] = Decimal(str(r["total"] or 0))
    return held


async def _branches_hold_by_branch(skus: list[str] | None) -> dict[str, dict[str, Decimal]]:
    """What each branch holds of each code, from its latest complete stock list. Keyed code, then branch.

    Grouping by branch as well as by code does not change what the totals come to: these are stock quantities
    added, not divided, and Decimal addition of three-decimal quantities is exact, so a sum of per-branch
    subtotals equals the sum of the rows. The ORDER BY is for a stable answer, not for the arithmetic.

    This asked `items_service.branch_rows` for 500 codes at a time and added the rows up here. For every active Item
    that was 130 round trips returning 42,683 rows between them, so the sum moved into the database: one pass, and
    the clamp that drops a negative shelf quantity moves with it unchanged.
    """
    from tortoise import Tortoise

    from app.services import items_service

    if skus is not None and not skus:
        return {}
    lists = await items_service.latest_branch_lists()
    if not lists:
        return {}
    conn = Tortoise.get_connection("default")
    pairs = "(" + " OR ".join(f"(s.branch_id = {dialect.mark()} AND s.snapshot_id = {dialect.mark('varchar(60)')})" for _ in lists) + ")"
    out: dict[str, dict[str, Decimal]] = {}
    for chunk in _chunks(skus):
        where, params = _item_scope("s.product_sku", chunk, on="sku")
        for r in await conn.execute_query_dict(
            "SELECT s.product_sku AS sku, s.branch_id AS branch, "
            "SUM(CASE WHEN s.qty > 0 THEN s.qty ELSE 0 END) AS held "
            f"FROM branch_product_stock s WHERE {pairs} AND {where} "
            "GROUP BY s.product_sku, s.branch_id ORDER BY s.product_sku, s.branch_id",
            [v for entry in lists for v in (entry["branchId"], entry["snapshotId"])] + params,
        ):
            out.setdefault(r["sku"], {})[str(r["branch"])] = Decimal(str(r["held"] or 0))
    return out


async def _branches_hold(skus: list[str] | None) -> dict[str, Decimal]:
    """What the branches hold of each code, added up over their latest complete stock lists."""
    return _totalled(await _branches_hold_by_branch(skus))


async def _heading_to_branches(product_ids: list[str] | None) -> dict[str, dict[str, Decimal]]:
    """What is already on its way to each branch, per Item. Keyed Item, then branch.

    Only `dispatched` and `in_transit`. An `approved` transfer has not left the godown, so its stock is still
    counted by `_held_in_godown`, and counting it here as well would take the same units off a suggestion twice.
    A `requested` one is a branch asking, not stock moving.
    """
    from tortoise import Tortoise

    if product_ids is not None and not product_ids:
        return {}
    conn = Tortoise.get_connection("default")
    out: dict[str, dict[str, Decimal]] = {}
    for chunk in _chunks(product_ids):
        where, params = _item_scope("l.product_id", chunk)
        for r in await conn.execute_query_dict(
            f"SELECT l.product_id AS pid, t.branch_id AS branch, SUM({dialect.exact_number('l.qty_sent')}) AS qty "
            "FROM transfer_lines l JOIN transfers t ON t.id = l.transfer_id "
            f"WHERE t.status IN ('dispatched', 'in_transit') AND {where} "
            "GROUP BY l.product_id, t.branch_id ORDER BY l.product_id, t.branch_id", params,
        ):
            if (r["qty"] or 0) > 0:
                out.setdefault(str(r["pid"]), {})[str(r["branch"])] = Decimal(str(r["qty"]))
    return out


async def _on_order(product_ids: list[str] | None, exclude_order_id: str | None) -> dict[str, Decimal]:
    """Paid-for units still owed on open orders, per Item.

    `product_ids=None` is every active Item, reached by joining products and filtering on `active` rather than by
    naming the ids: naming 64,589 of them is the parameter overflow described at `_item_scope`.
    """
    out: dict[str, Decimal] = {}
    for chunk in _chunks(product_ids):
        qs = PurchaseOrderLine.filter(purchase_order__status__in=list(OPEN))
        qs = qs.filter(product__active=True) if chunk is None else qs.filter(product_id__in=chunk)
        if exclude_order_id:
            qs = qs.exclude(purchase_order_id=exclude_order_id)
        for pid, qty, got in await qs.values_list("product_id", "qty", "received_qty"):
            out[str(pid)] = out.get(str(pid), ZERO) + max(Decimal(str(qty)) - Decimal(str(got)), ZERO)
    return out


async def suggestions(supplier_id: str | None, cover_days: int = 30, exclude_order_id: str | None = None,
                      branch_id: str | None = None) -> dict:
    """With a supplier: the Items bought from them before, each with a suggested quantity and why. Without one: the
    godown Items running low (at or below their reorder level, or lasting under a week at the branches' rate).

    WITH A BRANCH, IT IS A DIFFERENT QUESTION
    -----------------------------------------
    Without `branch_id` this answers "what should the godown buy", so every branch's sales are added together and
    the stock counted is the godown's. With one it answers "what is THIS branch running out of": that branch's own
    sales a day, that branch's own shelves, and what is already on its way to it. The branch never buys -- head
    office does -- but it is the branch's own figures that say when.

    Three things deliberately differ in the branch answer:

      * The godown's dispatch rate is not consulted. What the godown sent a branch is a measure of the godown's
        own outflow and is the right answer to the godown's question; for one branch, what that branch sold is
        the truer figure, and using the faster of the two would quietly reintroduce the aggregation.
      * "On order" means in transit to that branch, not on a purchase order. A purchase order fills the godown.
      * `reorder_level` is the Item's, because that is the only one that exists. A per-branch level is a real
        thing to want and there is nowhere to put it yet; said here so nobody reads it as a per-branch floor.

    Either way every line carries `branches`: which branches sell it, what each holds, and how many days each has
    left at its own rate. That is what lets whoever is buying see who they are buying for, which was the point.
    """
    from app.models import GRNLine, ProductSupplier
    from app.models.branch import selling_branches

    cover_days = min(max(int(cover_days or 30), 1), 365)
    # Head office is in `branches` but is not a shop, so it is not a branch anything can be suggested for; see
    # `models.branch.selling_branches`.
    who_is = {str(b["id"]): b for b in await selling_branches().values("id", "code", "name")}
    branch = None
    if branch_id:
        branch = who_is.get(str(branch_id))
        if not branch:
            raise PurchasingError("That branch doesn't exist, or it is head office, which does not sell.", 404)
        branch_id = str(branch_id)
    supplier = None
    last_cost: dict[str, tuple[datetime, Decimal]] = {}
    source: dict[str, str] = {}
    if supplier_id:
        supplier = await Supplier.get_or_none(id=supplier_id)
        if not supplier:
            raise PurchasingError("That supplier doesn't exist.", 404)
        for pid, cost, at in await GRNLine.filter(grn__supplier_id=supplier_id).values_list("product_id", "unit_price", "grn__at"):
            source[str(pid)] = "received from them"
            if at and (str(pid) not in last_cost or at > last_cost[str(pid)][0]):
                last_cost[str(pid)] = (at, Decimal(str(cost)))
        ordered_at: dict[str, tuple[datetime, Decimal]] = {}
        for pid, cost, at in await PurchaseOrderLine.filter(purchase_order__supplier_id=supplier_id).exclude(
            purchase_order__status="cancelled",
        ).values_list("product_id", "unit_cost", "purchase_order__raised_at"):
            source.setdefault(str(pid), "ordered from them")
            if at and (str(pid) not in ordered_at or at > ordered_at[str(pid)][0]):
                ordered_at[str(pid)] = (at, Decimal(str(cost)))
        # What was last paid them on a GRN, or else the price last agreed on an order.
        for pid, seen in ordered_at.items():
            last_cost.setdefault(pid, seen)
        for pid in await ProductSupplier.filter(supplier_id=supplier_id).values_list("product_id", flat=True):
            source.setdefault(str(pid), "on the Item's supplier list")
        products = await Product.filter(id__in=list(source), active=True) if source else []
    else:
        # `.only(...)` rather than the whole row. Without a supplier this is every active Item -- 64,589 of them --
        # and building that many full Product objects measured 3.34s of this call's 6.37, more than everything else
        # together. The nine columns below are every field the loop and the output below actually read, so the
        # answer is unchanged; `.values()` was tried and is no faster, because the cost is the row count and not
        # the column count.
        products = await Product.filter(active=True).only(
            "id", "sku", "name", "unit", "pack_size", "reorder_level", "is_weighed", "avg_cost", "needs_details")
    ids = [p.id for p in products]
    # Without a supplier the Items are every active one, which is what each helper's `None` means: it filters on the
    # `active` column instead of being handed 64,589 ids. With a supplier the short list is named, as before.
    on_items = ids if supplier_id else NO_IDS
    on_codes = [p.sku for p in products] if supplier_id else NO_IDS
    held = await _held_in_godown(on_items) if products else {}
    on_order = await _on_order(on_items, exclude_order_id) if products else {}
    # Read per branch and total it here, rather than reading it twice. `_branch_sales_rates` and `_branches_hold`
    # are these two totalled, so the godown answer below is arithmetically the one it always was.
    rates_by = await _branch_sales_rates_by_branch(on_codes) if products else {}
    hold_by = await _branches_hold_by_branch(on_codes) if products else {}
    rates, branches = _totalled(rates_by), _totalled(hold_by)
    sent = await _dispatch_rates(on_items) if products else {}
    coming = await _heading_to_branches(on_items) if (products and branch) else {}
    if not supplier_id and ids:
        # Without a supplier, the cost is the last price paid anyone. `product__active=True` joins products and
        # filters on the column; `product_id__in=ids` named all 64,589 and would overflow the parameter ceiling the
        # moment this line was reached, the same way `_on_order` did.
        for pid, cost, at in await GRNLine.filter(product__active=True).values_list("product_id", "unit_price", "grn__at"):
            if at and (str(pid) not in last_cost or at > last_cost[str(pid)][0]):
                last_cost[str(pid)] = (at, Decimal(str(cost)))

    lines = []
    for p in products:
        if branch:
            r, who = rates_by.get(p.sku, {}).get(branch_id, ZERO), "this branch sells"
            h = hold_by.get(p.sku, {}).get(branch_id, ZERO)
            o = coming.get(p.id, {}).get(branch_id, ZERO)
            s = suggest_line(r, h, o, p.reorder_level, cover_days, p.pack_size, p.is_weighed,
                             who=who, holder="the branch holds")
        else:
            h, o = held.get(p.id, ZERO), on_order.get(p.id, ZERO)
            # The faster of the two: what the branches sell of it, or what the godown sends out to them.
            r, who = rates.get(p.sku, ZERO), "branches sell"
            if sent.get(p.id, ZERO) > r:
                r, who = sent[p.id], "the godown sends branches"
            s = suggest_line(r, h, o, p.reorder_level, cover_days, p.pack_size, p.is_weighed, who=who, holder="godown holds")
        if not supplier_id:
            low = (p.reorder_level is not None and p.reorder_level > 0 and h + o <= p.reorder_level) or (r > 0 and h + o < r * SHORT_DAYS)
            if not low or s["qty"] <= 0:
                continue
        cost = last_cost.get(p.id, (None, p.avg_cost))[1]
        # Who this quantity is for. A branch appears when it sells the Item or holds it, and `daysLeft` is what
        # it has at its own rate -- the one figure that is comparable across a five rupee sweet and a three
        # thousand rupee pharmacy line, and the same measure the order gate is built on. Most urgent first, and
        # a branch that holds stock it never sells sorts last rather than looking infinitely well supplied.
        split = []
        for bid, meta in who_is.items():
            br = rates_by.get(p.sku, {}).get(bid, ZERO)
            bh = hold_by.get(p.sku, {}).get(bid, ZERO)
            if br <= 0 and bh <= 0:
                continue
            split.append({"branchId": bid, "branchCode": meta["code"], "branchName": meta["name"],
                          "ratePerDay": br, "held": bh, "onTheWay": coming.get(p.id, {}).get(bid, ZERO),
                          "daysLeft": (bh / br) if br > 0 else None})
        split.sort(key=lambda b: (b["daysLeft"] is None, b["daysLeft"] or ZERO, b["branchCode"]))
        lines.append({
            "productId": p.id, "sku": p.sku, "name": p.name, "unit": p.unit, "packSize": p.pack_size,
            "ratePerDay": s["rate"], "held": h, "onOrder": o, "reorderLevel": p.reorder_level, "branchesHold": branches.get(p.sku, ZERO),
            "suggestedQty": s["qty"], "unitCost": cost or ZERO, "reason": s["reason"], "why": source.get(p.id, "running low"),
            "needsDetails": p.needs_details, "branches": split,
        })
    # sku last, because it is unique: without it two lines agreeing on rate and name had no defined
    # order at all, and any wobble in the rate decided it differently on each run.
    lines.sort(key=lambda l: (l["suggestedQty"] <= 0, -(l["ratePerDay"] or ZERO), l["name"], l["sku"]))
    rule = (f"Suggested quantity is what the branches sell in {cover_days} days (from their last {SALES_WINDOW_DAYS} days of sales, "
            "or what the godown sent them if that is more), "
            "or the Item's reorder level if that is higher, less what the godown holds and what is already on open orders, "
            "rounded up to whole packs where the pack size is known.")
    if branch:
        rule = (f"Suggested quantity is what {branch['name']} sells in {cover_days} days (from its own last "
                f"{SALES_WINDOW_DAYS} days of sales), or the Item's reorder level if that is higher, less what that "
                "branch holds and what is already on its way to it, rounded up to whole packs where the pack size "
                "is known. The branch does not buy: this is what head office should be buying for it.")
    return {
        "supplierId": supplier_id, "supplierName": supplier.name if supplier else None, "coverDays": cover_days,
        "branchId": branch_id if branch else None, "branchName": branch["name"] if branch else None,
        "branchCode": branch["code"] if branch else None,
        "salesDays": SALES_WINDOW_DAYS, "count": len(lines), "lines": lines[:MAX_SUGGESTIONS],
        "rule": rule,
    }
