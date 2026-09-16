"""What head office's godown has asked suppliers for.

Lifecycle: draft → (submitted) → approved, or waiting for approval → approved / rejected → partially received →
received, or closed when the rest will never come; a draft, a rejected order or an approved one with nothing
received can be cancelled.

Approval follows the money: submitting an order within your own purchase approval limit approves it (the
Executive is told); above it, the order waits for someone whose limit covers it. Nobody approves their own order
that is above their limit, and nothing is received against an order that isn't approved.
"""
from tortoise import fields, models


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
    raised_by: fields.ForeignKeyRelation["User"] = fields.ForeignKeyField("models.User", related_name="purchase_orders_raised")
    raised_at = fields.DatetimeField(auto_now_add=True)
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
    position = fields.IntField(default=0)

    class Meta:
        table = "warehouse_purchase_order_lines"
