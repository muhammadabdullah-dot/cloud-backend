"""The documents themselves, kept at head office: a bill, what was on it, how it was paid, what came back, and
what the tax authority was told.

Everything else head office keeps about a branch is a **figure**: a day's takings, an hour's invoices, a product's
quantity. Those answer "how much" and they are enough to run a company by, which is why they were built first. They
cannot answer "show me that bill", and a company that cannot open one bill cannot settle an argument with a customer,
audit a cashier, or carry its own history across a change of software.

These tables are the other half. They hold what a branch actually wrote, at the grain it wrote it.

**Two things write here, and the shape has to suit both.**

* A live branch, an event at a time, as it sells. That is the road `sync_inbox_events` already runs on: every record
  a branch makes is already at head office, and ten kinds of them sit there marked `stored` because until now
  nothing applied them (`services/projector.py` says so in its own comment).
* The legacy pipeline, in bulk, a window of days at a time, from the old software's own database.

So every table here is keyed on the branch and the branch's own document number, and writing the same document twice
updates rather than doubles. A window can be reloaded by deleting that branch's rows in that day range and writing
them again, which is exactly how `snapshot_service.apply_aggregates` already treats a window of figures.

**`day` is carried on every row** even where `at` would do. Every other table head office keeps is grouped by the
branch's own trading day, which is not the calendar day when a shop sells past midnight, and a document that could
not be filed the same way as the figures beside it would make the two disagree on screen.

**What is deliberately not here.** A till session and a cash movement are documents too, but `BranchTillClose`
already holds a session at this grain (its number, whose, opened, closed, float, counted, variance) and is fed by the
figures. Overloading it would leave a document being deleted and rewritten every time a window of figures is
replaced. Those come next, as their own tables, when the buying and stock documents do.
"""
from tortoise import fields, models

# Where a document came from. A branch wrote it as it sold, or the pipeline brought it out of the old software.
SOURCES = ("branch", "legacy")


class BranchSale(models.Model):
    """One bill, as the branch rang it.

    The money is kept as the branch worked it out, not recomputed here. Head office recomputing a bill would produce
    a second opinion about what a customer paid, and there is only one right answer to that: the one on the receipt
    in their hand.
    """

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="sales"
    )
    # The branch's own invoice number, and the only thing that identifies a bill across the two systems.
    invoice_number = fields.CharField(max_length=30)
    at = fields.DatetimeField()
    # The branch's trading day, which is not the calendar day where a shop sells past midnight.
    day = fields.DateField()
    # Names, not ids: head office does not hold a branch's users or parties, and a name is what a person reading a
    # bill needs. The branch's own id is kept beside it for the rare case of tracing one back.
    cashier_name = fields.CharField(max_length=120, null=True)
    party_name = fields.CharField(max_length=160, null=True)
    party_code = fields.CharField(max_length=40, null=True)
    till_session_number = fields.CharField(max_length=20, null=True)
    counter_name = fields.CharField(max_length=80, null=True)

    gross = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    disc_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # Charges added to the bill: the old software's MiscCharges, ours is `fare`.
    fare = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    gst = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    grand_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # What the customer was actually charged after rounding.
    net_value = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    received = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    cash_back = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # What the goods cost the shop, as the branch recorded it, so margin can be read without pricing it again.
    cogs = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    is_credit_sale = fields.BooleanField(default=False)
    voided = fields.BooleanField(default=False)
    fbr_invoice_number = fields.CharField(max_length=60, null=True)
    member_code = fields.CharField(max_length=40, null=True)
    earned_points = fields.IntField(default=0)

    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_sales"
        # One bill per number per branch: a document sent twice updates rather than doubling, which is what makes
        # both a retried event and a replayed window safe.
        unique_together = (("branch", "invoice_number"),)
        ordering = ["-at"]


class BranchSaleLine(models.Model):
    """One line of a bill."""

    id = fields.UUIDField(pk=True)
    sale: fields.ForeignKeyRelation[BranchSale] = fields.ForeignKeyField(
        "models.BranchSale", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField(default=0)
    # The Item by its code, which head office and every branch share, plus the name as it stood on the day. A name
    # changes; a bill must keep saying what it said when it was printed.
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    department = fields.CharField(max_length=80, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    disc_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    # A line taken back on this bill rather than sold.
    is_return = fields.BooleanField(default=False)
    # The barcode actually scanned, where it was a pack code rather than the Item's own.
    alias_code = fields.CharField(max_length=60, null=True)
    # Which campaign gave this line its discount, by code, so what a campaign cost can be read from the bills
    # themselves and not only from the figures.
    promotion_code = fields.CharField(max_length=20, null=True)
    # Selling level, where the branch sells by piece, strip, pack or box.
    sell_level = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "branch_sale_lines"
        unique_together = (("sale", "line_no"),)
        ordering = ["line_no"]


class BranchSaleTender(models.Model):
    """How one bill was paid, a row per method.

    A row each rather than columns for cash, card and credit: the old software used columns and then stopped filling
    them, and a shop that starts taking a new method should not need a schema change to record it.
    """

    id = fields.UUIDField(pk=True)
    sale: fields.ForeignKeyRelation[BranchSale] = fields.ForeignKeyField(
        "models.BranchSale", related_name="tenders", on_delete=fields.CASCADE
    )
    code = fields.CharField(max_length=20)
    name = fields.CharField(max_length=80, null=True)
    amount = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # A card's name and number, a transfer's reference, the approval code: whatever proves the money arrived.
    reference = fields.CharField(max_length=120, null=True)

    class Meta:
        table = "branch_sale_tenders"
        unique_together = (("sale", "code"),)


class BranchReturnRecord(models.Model):
    """One return, as its own document.

    Not `branch_returns`: that name is already a day-grouped figure, one row per returned line per day, which is what
    the Returns drill-down reads. This is the document behind it.
    """

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="return_records"
    )
    number = fields.CharField(max_length=30)
    at = fields.DatetimeField()
    day = fields.DateField()
    # The bill it comes back against. A return that names no bill is one nobody can check.
    against_invoice = fields.CharField(max_length=30, null=True)
    cashier_name = fields.CharField(max_length=120, null=True)
    refund_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    tax_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    refund_method = fields.CharField(max_length=20, null=True)
    reason = fields.CharField(max_length=160, null=True)
    note = fields.CharField(max_length=255, null=True)
    till_session_number = fields.CharField(max_length=20, null=True)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_return_records"
        unique_together = (("branch", "number"),)
        ordering = ["-at"]


class BranchReturnLine(models.Model):
    id = fields.UUIDField(pk=True)
    return_record: fields.ForeignKeyRelation[BranchReturnRecord] = fields.ForeignKeyField(
        "models.BranchReturnRecord", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField(default=0)
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, default=0)

    class Meta:
        table = "branch_return_lines"
        unique_together = (("return_record", "line_no"),)
        ordering = ["line_no"]


class BranchFbrInvoice(models.Model):
    """What the tax authority was told about one bill, and what it said back.

    Kept apart from the bill because they are not the same fact and they can disagree: a bill exists whether or not
    FBR ever saw it, an invoice can be sent late, refused, or sent again, and the number FBR gives back is theirs
    rather than ours. The old software kept them apart for the same reason, in its own FBRSaleInvoiceRecord.
    """

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="fbr_invoices"
    )
    # The bill or return it belongs to, by the branch's own number.
    invoice_number = fields.CharField(max_length=30)
    # Sale or return: a return is its own kind of document to FBR.
    kind = fields.CharField(max_length=10, default="sale")
    at = fields.DatetimeField()
    day = fields.DateField()
    # What FBR gave back. Null while it is unsent or refused, which is a state worth being able to find.
    fbr_invoice_number = fields.CharField(max_length=60, null=True)
    # sent · refused · pending · offline
    status = fields.CharField(max_length=20, default="sent")
    pos_id = fields.CharField(max_length=40, null=True)
    total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    tax_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # What FBR said when it refused, kept as they said it.
    message = fields.CharField(max_length=255, null=True)
    buyer_name = fields.CharField(max_length=160, null=True)
    buyer_ntn = fields.CharField(max_length=40, null=True)
    buyer_cnic = fields.CharField(max_length=40, null=True)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_fbr_invoices"
        unique_together = (("branch", "invoice_number", "kind"),)
        ordering = ["-at"]


class BranchFbrInvoiceLine(models.Model):
    """What FBR was told line by line.

    Its own table rather than read off the bill's lines, because what was declared and what was sold are different
    facts: a line can be declared under a different HS code or tax rate from the one the till used, and when FBR
    queries a return it is the declared figures that have to be produced. The old software keeps 1,453,941 of these.
    """

    id = fields.UUIDField(pk=True)
    invoice: fields.ForeignKeyRelation[BranchFbrInvoice] = fields.ForeignKeyField(
        "models.BranchFbrInvoice", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField(default=0)
    product_sku = fields.CharField(max_length=40, null=True)
    product_name = fields.CharField(max_length=200, null=True)
    hs_code = fields.CharField(max_length=40, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    disc_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    tax_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_fbr_invoice_lines"
        unique_together = (("invoice", "line_no"),)
        ordering = ["line_no"]
