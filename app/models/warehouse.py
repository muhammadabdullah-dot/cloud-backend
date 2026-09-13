"""The godown's own ledger and the documents that move stock through it.

Same discipline as the Branch Server's inventory domain: stock on hand is never stored, only
folded from `StockMovement`. A stored balance drifts from reality; a derived one cannot.

Two entities here carry a real `branch` foreign key — `Requisition` and `Transfer`. Both used to
hold a free-text branch name on the frontend, which frontend-baseline.md §4 names as the single
biggest structural gap on this side: nothing tied "Fort Colony" the requisition to "Fort Colony"
the registered branch.
"""
from tortoise import fields, models

MOVEMENT_KINDS = ("receive", "dispatch", "count-correction", "adjust")
REQUISITION_STATUSES = ("pending", "approved", "rejected")
# The full pass-the-pen lifecycle. Cloud owns this record (contracts.md §5.4) — it is the source
# of dispatch truth — and the receiving branch sees the same row rather than a mirrored copy.
TRANSFER_STATUSES = ("requested", "approved", "dispatched", "in_transit", "received", "received_short")
COUNT_STATUSES = ("pending", "approved")
GST_MODES = ("normal", "normal-bonus")


class StockMovement(models.Model):
    """One line of the godown ledger. Signed: positive is stock in, negative is stock out."""
    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="warehouse_movements"
    )
    bin: fields.ForeignKeyRelation["Bin"] = fields.ForeignKeyField("models.Bin", related_name="movements")
    kind = fields.CharField(max_length=30)
    qty = fields.DecimalField(max_digits=16, decimal_places=3)
    reason = fields.CharField(max_length=200, null=True)
    # null for seeded opening stock, which no user caused.
    origin_user: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="warehouse_movements", null=True
    )
    at = fields.DatetimeField()

    class Meta:
        table = "warehouse_stock_movements"
        ordering = ["-at"]


class Batch(models.Model):
    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="warehouse_batches"
    )
    lot_number = fields.CharField(max_length=60, null=True)
    expiry = fields.DatetimeField(null=True)
    received_qty = fields.DecimalField(max_digits=16, decimal_places=3)

    class Meta:
        table = "warehouse_batches"
        ordering = ["expiry"]


class GRN(models.Model):
    """Goods Receipt Note — supplier stock arriving into the godown. Numbered WGRN-nnnn to keep it
    distinct from a branch's own GRN sequence."""
    id = fields.UUIDField(pk=True)
    grn_number = fields.CharField(max_length=30, unique=True)
    supplier: fields.ForeignKeyRelation["Supplier"] = fields.ForeignKeyField(
        "models.Supplier", related_name="warehouse_grns"
    )
    party_inv_no = fields.CharField(max_length=60, null=True)
    bin: fields.ForeignKeyRelation["Bin"] = fields.ForeignKeyField("models.Bin", related_name="grns")
    gst_mode = fields.CharField(max_length=20, default="normal")
    advance_tax = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    approved = fields.BooleanField(default=False)
    received_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="warehouse_grns", null=True
    )
    at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "warehouse_grns"
        ordering = ["-at"]


class GRNLine(models.Model):
    id = fields.UUIDField(pk=True)
    grn: fields.ForeignKeyRelation["GRN"] = fields.ForeignKeyField("models.GRN", related_name="lines")
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="warehouse_grn_lines"
    )
    qty = fields.DecimalField(max_digits=16, decimal_places=3)
    # Free units from the supplier: they raise stock but not what was paid, so they dilute
    # average cost rather than adding to it.
    bonus_qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=2)
    disc_percent = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    expiry = fields.DatetimeField(null=True)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        table = "warehouse_grn_lines"


class Requisition(models.Model):
    """A branch asking the godown for stock. Approving one creates a Transfer — nothing re-typed."""
    id = fields.UUIDField(pk=True)
    requisition_number = fields.CharField(max_length=30, unique=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="requisitions"
    )
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="requisitions"
    )
    qty_requested = fields.DecimalField(max_digits=16, decimal_places=3)
    status = fields.CharField(max_length=20, default="pending")
    requested_at = fields.DatetimeField()
    decided_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="decided_requisitions", null=True
    )
    decided_at = fields.DatetimeField(null=True)

    class Meta:
        table = "requisitions"
        ordering = ["-requested_at"]


class Transfer(models.Model):
    id = fields.UUIDField(pk=True)
    transfer_number = fields.CharField(max_length=30, unique=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="transfers"
    )
    # Set when a requisition produced this transfer, so the two stay linked rather than the
    # requisition simply flipping status and the connection living only in someone's memory.
    requisition: fields.ForeignKeyNullableRelation["Requisition"] = fields.ForeignKeyField(
        "models.Requisition", related_name="transfers", null=True
    )
    status = fields.CharField(max_length=20, default="approved")
    vehicle = fields.CharField(max_length=40, null=True)
    driver = fields.CharField(max_length=120, null=True)
    requested_at = fields.DatetimeField()
    approved_at = fields.DatetimeField(null=True)
    dispatched_at = fields.DatetimeField(null=True)
    received_at = fields.DatetimeField(null=True)
    # A short receipt is money, so it becomes a visible dispute with qty_sent frozen rather than
    # being quietly overwritten by whatever arrived.
    dispute_open = fields.BooleanField(default=False)
    dispute_note = fields.CharField(max_length=255, null=True)

    class Meta:
        table = "transfers"
        ordering = ["-requested_at"]


class TransferLine(models.Model):
    id = fields.UUIDField(pk=True)
    transfer: fields.ForeignKeyRelation["Transfer"] = fields.ForeignKeyField(
        "models.Transfer", related_name="lines"
    )
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="transfer_lines"
    )
    qty_sent = fields.DecimalField(max_digits=16, decimal_places=3)
    qty_received = fields.DecimalField(max_digits=16, decimal_places=3, null=True)

    class Meta:
        table = "transfer_lines"


class CycleCount(models.Model):
    """Counting one bin's stock of one item against what the ledger says. Same submit-then-approve
    split as the Branch Server's physical counts: whoever counts is not whoever signs it off."""
    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="cycle_counts"
    )
    bin: fields.ForeignKeyRelation["Bin"] = fields.ForeignKeyField("models.Bin", related_name="cycle_counts")
    # Snapshotted at submit time, so a later movement can't silently change what the variance was
    # measured against.
    system_qty = fields.DecimalField(max_digits=16, decimal_places=3)
    counted_qty = fields.DecimalField(max_digits=16, decimal_places=3)
    status = fields.CharField(max_length=20, default="pending")
    counted_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="cycle_counts", null=True
    )
    approved_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="approved_cycle_counts", null=True
    )
    at = fields.DatetimeField()

    class Meta:
        table = "cycle_counts"
        ordering = ["-at"]
