"""What head office's godown has asked suppliers for.

Lifecycle: draft → (submitted) → approved, or waiting for approval → approved / rejected → partially received →
received, or closed when the rest will never come; a draft, a rejected order or an approved one with nothing
received can be cancelled.

Approval follows the money: submitting an order within your own purchase approval limit approves it (the
Executive is told); above it, the order waits for someone whose limit covers it. Nobody approves their own order
that is above their limit, and nothing is received against an order that isn't approved.
"""
from tortoise import fields, models

# Where a document came from. The same two words `branch_purchasing.py` uses, so one reading serves both sides.
SOURCES = ("godown", "legacy")


class PurchaseOrder(models.Model):
    id = fields.UUIDField(pk=True)
    po_number = fields.CharField(max_length=20, unique=True)
    supplier: fields.ForeignKeyRelation["Supplier"] = fields.ForeignKeyField("models.Supplier", related_name="purchase_orders")
    # draft · pending_approval · approved · rejected · partially_received · received · closed · cancelled
    status = fields.CharField(max_length=20, default="draft")
    # Why it's being ordered — "Running low", "Ramzan stock" — as the person raising it put it.
    reason = fields.CharField(max_length=120, null=True)
    expected_at = fields.DatetimeField(null=True)
    notes = fields.CharField(max_length=255, null=True)
    total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # NO ACTION: this table also reaches `users` through approved_by, and SQL Server allows one cascade path per
    # table. An order is a record of what was bought; it does not belong to whoever typed it.
    #
    # Nullable since the Multi-Tec import. The 811 orders their head office raised for its own godown were raised by
    # eight of their logins (ZAIN, AWISGT, ASIFPOMT and five more) and `users` holds six rows, the sign-ins made for
    # this system. Their 90 people are records in `branch_users`, deliberately without sign-ins, so none of the eight
    # can be a foreign key here. Inventing six user rows to satisfy a constraint would put names in the sign-in list
    # that nobody can sign in as.
    raised_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="purchase_orders_raised", null=True, on_delete=fields.NO_ACTION
    )
    # Who raised it, as their document spelt it, when there is no user row to point at. Same convention as
    # `branch_purchasing.py`: "Names and codes, not foreign keys ... A name is also what a person reading the
    # document needs." Null on an order raised here, where `raised_by` carries the real user.
    raised_by_name = fields.CharField(max_length=120, null=True)
    # Not auto_now_add: an order's date is a fact about the order, not about when the row was written, and the
    # oldest imported one was raised on 1 September 2024. `purchasing_service.create_order` passes now() for an
    # order raised here, so nothing about the live path changes.
    raised_at = fields.DatetimeField()
    # WHO THIS ORDER IS FOR
    # ---------------------
    # Null means the godown, which is every order raised before this existed and every ordinary order since: head
    # office buys centrally, the goods land in the godown, and they reach a branch later as a transfer. That is
    # the normal path and nothing about it changes.
    #
    # Set, it names the branch the goods are for, and two things follow. The Purchase Orders screen can be read a
    # branch at a time, which it could not before: "which order went to which branch" had no answer on this table
    # at all. And receiving it is meant to put the stock at that branch rather than in the godown -- the pharmacy
    # short road agreed on 12 October 2026, where a one day item cannot afford branch asks, head office buys,
    # godown receives and someone transfers.
    #
    # NO ACTION, like `raised_by` above and for the same reason: SQL Server allows one cascade path per table and
    # this one already has several. An order is a record of what was bought and for whom; deleting a branch must
    # be refused rather than quietly leaving orders addressed to nobody.
    for_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="purchase_orders_for", null=True, on_delete=fields.NO_ACTION
    )
    # Where this order came from: "godown" is one raised on these screens, "legacy" one imported out of Multi-Tec.
    # Without it nobody can answer "which of these did we type", and `scripts/remove_demo_data.py` shows what that
    # costs: it had to identify head office's own seeded orders by an explicit hand-written rule per table.
    source = fields.CharField(max_length=10, default="godown")
    submitted_at = fields.DatetimeField(null=True)
    approved_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="purchase_orders_approved", null=True, on_delete=fields.SET_NULL
    )
    approved_at = fields.DatetimeField(null=True)
    # Approved on submit because it was within the raiser's own limit.
    auto_approved = fields.BooleanField(default=False)
    # Why it was rejected, cancelled or closed.
    decision_note = fields.CharField(max_length=255, null=True)
    closed_at = fields.DatetimeField(null=True)

    class Meta:
        table = "warehouse_purchase_orders"
        ordering = ["-raised_at"]


class PurchaseOrderLine(models.Model):
    id = fields.UUIDField(pk=True)
    purchase_order: fields.ForeignKeyRelation[PurchaseOrder] = fields.ForeignKeyField(
        "models.PurchaseOrder", related_name="lines", on_delete=fields.CASCADE
    )
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField("models.Product", related_name="purchase_order_lines")
    qty = fields.DecimalField(max_digits=16, decimal_places=3)
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=2)
    # Paid-for units received against this line so far; bonus units don't count toward the order.
    received_qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    # WHICH BRANCH THIS QUANTITY WAS BOUGHT FOR
    # -----------------------------------------
    # Null means the godown's own stock, which is the plain case. Set, it names the branch the quantity is meant
    # for, and that is what lets one order answer "who is this for" the way the business actually buys: order 100,
    # 40 for one branch, 40 for another, 20 kept. A single branch on the ORDER cannot say that, which is why the
    # split lives here on the lines and the header's `for_branch` is only for an order wholly for one branch.
    #
    # It is an intention recorded at the moment of buying, not a record of where the goods later went. Those are
    # different facts and only this one is knowable when the order is raised. Nothing downstream is bound by it:
    # the goods are received into the godown and dispatched as usual, because once in the godown units are
    # fungible and no transfer records which receipt's units it sent.
    #
    # It is cheap because the figure already exists: `purchasing_service.suggestions(branch_id=...)` and the
    # `branches` breakdown on every suggested line are exactly this split, worked out from each branch's own
    # sales. Raising the order from the suggestion carries it straight through.
    for_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="purchase_order_lines_for", null=True, on_delete=fields.NO_ACTION
    )
    position = fields.IntField(default=0)

    class Meta:
        table = "warehouse_purchase_order_lines"
