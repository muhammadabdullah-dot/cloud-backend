"""The godown's own ledger and the documents that move stock through it.

Same discipline as the Branch Server's inventory domain: stock on hand is never stored, only
folded from `StockMovement`. A stored balance drifts from reality; a derived one cannot.

Two entities here carry a real `branch` foreign key — `Requisition` and `Transfer`. Both used to
hold a free-text branch name on the frontend, which frontend-baseline.md §4 names as the single
biggest structural gap on this side: nothing tied "Fort Colony" the requisition to "Fort Colony"
the registered branch.
"""
from decimal import Decimal

from tortoise import fields, models
from tortoise.functions import Sum
from tortoise.signals import pre_save

MOVEMENT_KINDS = ("receive", "dispatch", "count-correction", "adjust", "move")
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
    # What one unit was worth at cost when it moved. Null on older movements.
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, null=True)

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
    # The order this delivery was received against, if any.
    purchase_order: fields.ForeignKeyNullableRelation["PurchaseOrder"] = fields.ForeignKeyField(
        "models.PurchaseOrder", related_name="grns", null=True, on_delete=fields.SET_NULL
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
    # Where this line was put away. Null on GRNs from before each line had its own bin (they used the GRN's bin).
    bin: fields.ForeignKeyNullableRelation["Bin"] = fields.ForeignKeyField(
        "models.Bin", related_name="grn_lines", null=True, on_delete=fields.SET_NULL
    )

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
    """Stock on its way to a branch — from the central godown, or from another branch.

    `branch` is always where it's going. `source_branch` is null when it leaves the godown, and is the
    sending branch for a branch-to-branch transfer, which that branch dispatches itself and head office
    relays. Either way the receiving branch counts what arrived and its receipt comes back up here."""
    id = fields.UUIDField(pk=True)
    transfer_number = fields.CharField(max_length=30, unique=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="transfers"
    )
    source_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="outbound_transfers", null=True, on_delete=fields.SET_NULL
    )
    notes = fields.CharField(max_length=255, null=True)
    received_by_name = fields.CharField(max_length=120, null=True)
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
    # The receiving branch held the shipment back from being received, and why.
    hold_note = fields.CharField(max_length=255, null=True)
    held_at = fields.DatetimeField(null=True)
    held_by_name = fields.CharField(max_length=120, null=True)
    dispute_note = fields.CharField(max_length=255, null=True)
    # The receiving branch's answer before anything leaves: awaiting · acknowledged · declined, or skipped (the
    # branch asked for it, or has no server of its own) · overridden (sent after the branch stayed offline, with
    # a written reason). Null on transfers from before this step existed.
    ack_status = fields.CharField(max_length=12, null=True)
    ack_requested_at = fields.DatetimeField(null=True)
    ack_at = fields.DatetimeField(null=True)
    ack_by_name = fields.CharField(max_length=120, null=True)
    ack_note = fields.CharField(max_length=255, null=True)
    override_reason = fields.CharField(max_length=255, null=True)
    override_by_name = fields.CharField(max_length=120, null=True)
    override_at = fields.DatetimeField(null=True)
    cancelled_at = fields.DatetimeField(null=True)

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
    # What one unit cost the sender when it left (the godown's average cost, or the sending branch's).
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, null=True)

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


class BinMove(models.Model):
    """Stock moved from one bin to another — put away from where it was received, or tidied. The ledger gets a
    `move` out of one bin and a `move` into the other under the same number, so the godown total never changes."""

    id = fields.UUIDField(pk=True)
    number = fields.CharField(max_length=20, unique=True)
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField("models.Product", related_name="bin_moves")
    from_bin: fields.ForeignKeyRelation["Bin"] = fields.ForeignKeyField("models.Bin", related_name="moves_out")
    to_bin: fields.ForeignKeyRelation["Bin"] = fields.ForeignKeyField("models.Bin", related_name="moves_in")
    qty = fields.DecimalField(max_digits=16, decimal_places=3)
    note = fields.CharField(max_length=255, null=True)
    moved_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="bin_moves", null=True, on_delete=fields.SET_NULL
    )
    moved_by_name = fields.CharField(max_length=120, null=True)
    at = fields.DatetimeField()

    class Meta:
        table = "warehouse_bin_moves"
        ordering = ["-at"]


class StockBelowZero(Exception):
    """A godown movement that would leave a bin holding less than nothing. Shown to the person as it is (409)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@pre_save(StockMovement)
async def _never_below_zero(sender, instance: StockMovement, using_db, update_fields) -> None:
    """The last line: whatever path wrote this movement, no bin may hold less than nothing. Dispatch, put-away and counts
    check first with their own words; this catches anything that didn't."""
    if instance._saved_in_db or instance.qty is None or Decimal(str(instance.qty)) >= 0:
        return
    qs = StockMovement.filter(product_id=instance.product_id, bin_id=instance.bin_id)
    if using_db is not None:
        qs = qs.using_db(using_db)
    rows = await qs.annotate(total=Sum("qty")).values_list("total", flat=True)
    held = Decimal(str(rows[0])) if rows and rows[0] is not None else Decimal("0")
    if held + Decimal(str(instance.qty)) < Decimal("-0.0005"):
        from app.models.catalog import Bin, Product

        product = await Product.get_or_none(id=instance.product_id)
        bin_ = await Bin.get_or_none(id=instance.bin_id)
        name = product.name if product else "This Item"
        where = bin_.label if bin_ else "This bin"
        have = format(held.quantize(Decimal("0.001")).normalize(), "f")
        need = format((-Decimal(str(instance.qty))).quantize(Decimal("0.001")).normalize(), "f")
        raise StockBelowZero(f"{where} holds {have} of {name}, not enough to take {need}. Stock can't go below zero, so nothing was saved.")
