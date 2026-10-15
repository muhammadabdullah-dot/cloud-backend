"""What a branch bought, sent back, moved and owed, kept at head office at the grain the branch wrote it.

The other half of `branch_documents.py`, which says in its own closing paragraph that these come next. Same reasoning,
same conventions, and worth restating because the distinction is the whole point of these tables:

* Head office already keeps **figures** about a branch: a day's takings, a product's quantity. Those answer "how
  much" and cannot answer "show me that purchase order".
* `warehouse_purchase_orders` and `warehouse_grns` already exist and are **head office's own warehouse**. They carry
  no branch, and they must not: a shop buying stock from a supplier is not head office's warehouse receiving it, and
  filing one as the other would put a branch's purchases into head office's own stock position.

So a branch's buying documents need tables of their own, and this is them.

**Conventions, all inherited from `branch_documents.py` rather than invented here.**

* Keyed on the branch and the branch's own document number, so writing the same document twice updates rather than
  doubles, and a window can be replaced by deleting a day range and writing it again.
* **Names and codes, not foreign keys**, for products, suppliers and people. Head office does not hold a branch's
  users, and it holds a catalogue that may not be the branch's. A name is also what a person reading the document
  needs. `product_sku` is the branch's code and joins to `products` when that Item happens to exist here.
* **`day` on every row**, the branch's own trading day, because every other table head office keeps is grouped that
  way and a document filed by calendar day would disagree with the figures beside it on screen.
* The money is kept **as the branch worked it out**, never recomputed. Head office recomputing a purchase would
  produce a second opinion about what a supplier is owed.
"""
from tortoise import fields, models

SOURCES = ("branch", "legacy")


class BranchParty(models.Model):
    """A branch's own customer or supplier record, as the branch knew it.

    Head office has `suppliers`, which is the company's supplier list and is pushed down to branches, and it has
    `branch_credit_customers`, which is a list of who owes what right now and is replaced with every snapshot. It has
    nowhere for a branch's own party record: the name, the phone, the limit, the account it posts to. A purchase
    order naming a supplier head office has never heard of would otherwise name nothing.

    `side` says which it is, and a party can be both: one row per side, because their software lets one party buy and
    sell and the two relationships have different balances.
    """

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="parties"
    )
    code = fields.CharField(max_length=40)
    side = fields.CharField(max_length=10)          # customer | supplier
    name = fields.CharField(max_length=200)
    phone = fields.CharField(max_length=60, null=True)
    phone2 = fields.CharField(max_length=60, null=True)
    email = fields.CharField(max_length=160, null=True)
    address = fields.CharField(max_length=255, null=True)
    city = fields.CharField(max_length=120, null=True)
    ntn = fields.CharField(max_length=40, null=True)
    s_tax_reg_no = fields.CharField(max_length=40, null=True)
    cnic = fields.CharField(max_length=40, null=True)
    contact_person = fields.CharField(max_length=140, null=True)
    credit_limit = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    # What this branch's own unpaid documents add up to. Not their ledger balance, which lives in the books.
    owed = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    due_days = fields.IntField(null=True)
    discount_percent = fields.DecimalField(max_digits=5, decimal_places=2, default=0)
    # The account in this branch's book that this party posts to, by code, so the books and the documents agree.
    account_code = fields.CharField(max_length=40, null=True)
    active = fields.BooleanField(default=True)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_parties"
        unique_together = (("branch", "code", "side"),)
        ordering = ["name"]


class BranchPurchaseOrder(models.Model):
    """One order a branch placed with a supplier."""

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="purchase_orders_seen"
    )
    po_number = fields.CharField(max_length=30)
    at = fields.DatetimeField()
    day = fields.DateField()
    supplier_code = fields.CharField(max_length=40, null=True)
    supplier_name = fields.CharField(max_length=200, null=True)
    status = fields.CharField(max_length=20, default="closed")
    raised_by_name = fields.CharField(max_length=120, null=True)
    approved_by_name = fields.CharField(max_length=120, null=True)
    expected_at = fields.DatetimeField(null=True)
    approved_at = fields.DatetimeField(null=True)
    closed_at = fields.DatetimeField(null=True)
    notes = fields.CharField(max_length=255, null=True)
    total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_purchase_orders"
        unique_together = (("branch", "po_number"),)
        ordering = ["-at"]


class BranchPurchaseOrderLine(models.Model):
    id = fields.UUIDField(pk=True)
    purchase_order: fields.ForeignKeyRelation[BranchPurchaseOrder] = fields.ForeignKeyField(
        "models.BranchPurchaseOrder", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField()
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    received_qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    disc_percent = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    # What the branch had on the shelf when it ordered. A fact about that moment, which is why it is on the line.
    stock_at_order = fields.DecimalField(max_digits=14, decimal_places=3, null=True)

    class Meta:
        table = "branch_purchase_order_lines"
        unique_together = (("purchase_order", "line_no"),)
        ordering = ["line_no"]


class BranchGrn(models.Model):
    """One delivery a branch received and priced. Their software calls it a purchase."""

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="grns_seen"
    )
    grn_number = fields.CharField(max_length=30)
    at = fields.DatetimeField()
    day = fields.DateField()
    supplier_code = fields.CharField(max_length=40, null=True)
    supplier_name = fields.CharField(max_length=200, null=True)
    # The supplier's own invoice number and date, which is what a payment is matched against.
    party_inv_no = fields.CharField(max_length=60, null=True)
    party_inv_date = fields.DateField(null=True)
    po_number = fields.CharField(max_length=30, null=True)
    gst_mode = fields.CharField(max_length=20, null=True)
    received_by_name = fields.CharField(max_length=120, null=True)
    posted_by_name = fields.CharField(max_length=120, null=True)
    approved = fields.BooleanField(default=True)
    gross_total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    disc_total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    advance_tax = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    outstanding = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    due_date = fields.DateField(null=True)
    payment_mode = fields.CharField(max_length=20, null=True)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_grns"
        unique_together = (("branch", "grn_number"),)
        ordering = ["-at"]


class BranchGrnLine(models.Model):
    id = fields.UUIDField(pk=True)
    grn: fields.ForeignKeyRelation[BranchGrn] = fields.ForeignKeyField(
        "models.BranchGrn", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField()
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    bonus_qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    disc_percent = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    flat_disc = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    tax_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    batch_no = fields.CharField(max_length=60, null=True)
    expires_on = fields.DateField(null=True)
    # What the delivery changed the selling price to, which is the audit trail for a price rise.
    new_sale_price = fields.DecimalField(max_digits=14, decimal_places=2, null=True)
    new_retail_price = fields.DecimalField(max_digits=14, decimal_places=2, null=True)
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, null=True)

    class Meta:
        table = "branch_grn_lines"
        unique_together = (("grn", "line_no"),)
        ordering = ["line_no"]


class BranchPurchaseReturn(models.Model):
    """What a branch sent back to a supplier."""

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="purchase_returns_seen"
    )
    return_number = fields.CharField(max_length=30)
    at = fields.DatetimeField()
    day = fields.DateField()
    supplier_code = fields.CharField(max_length=40, null=True)
    supplier_name = fields.CharField(max_length=200, null=True)
    against_grn = fields.CharField(max_length=30, null=True)
    party_inv_no = fields.CharField(max_length=60, null=True)
    reason = fields.CharField(max_length=60, null=True)
    notes = fields.CharField(max_length=255, null=True)
    submitted_by_name = fields.CharField(max_length=120, null=True)
    total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_purchase_returns"
        unique_together = (("branch", "return_number"),)
        ordering = ["-at"]


class BranchPurchaseReturnLine(models.Model):
    id = fields.UUIDField(pk=True)
    purchase_return: fields.ForeignKeyRelation[BranchPurchaseReturn] = fields.ForeignKeyField(
        "models.BranchPurchaseReturn", related_name="lines", on_delete=fields.CASCADE
    )
    line_no = fields.IntField()
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    unit_price = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    tax_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    unit_cost = fields.DecimalField(max_digits=14, decimal_places=4, null=True)
    batch_no = fields.CharField(max_length=60, null=True)
    expires_on = fields.DateField(null=True)

    class Meta:
        table = "branch_purchase_return_lines"
        unique_together = (("purchase_return", "line_no"),)
        ordering = ["line_no"]


class BranchStockMovement(models.Model):
    """Every movement of stock a branch recorded, which is what makes a stock figure answerable.

    `branch_product_stock` says what is on the shelf now and `branch_product_stats` says what sold on a day. Neither
    can answer "why is this Item's stock what it is", and a stock figure nobody can trace is a stock figure nobody
    will trust. This is the branch's own ledger: one row per movement, with the balance the branch itself calculated
    after it.

    The largest table head office keeps by some distance, so it carries only what a movement is: no denormalised
    supplier, no prices beyond the rate on the movement.
    """

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="stock_movements_seen"
    )
    at = fields.DatetimeField()
    day = fields.DateField()
    # Their document kind: SV a sale, PV a purchase, TR a transfer, SRV a sale return, PRV a purchase return.
    kind = fields.CharField(max_length=10)
    document_number = fields.CharField(max_length=40, null=True)
    product_sku = fields.CharField(max_length=40)
    qty_in = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    qty_out = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    # The balance the branch worked out after this movement, kept as it stood rather than recomputed here.
    balance_after = fields.DecimalField(max_digits=14, decimal_places=3, null=True)
    rate = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    avg_cost_after = fields.DecimalField(max_digits=14, decimal_places=4, null=True)
    party_code = fields.CharField(max_length=40, null=True)
    user_name = fields.CharField(max_length=120, null=True)
    batch_no = fields.CharField(max_length=60, null=True)
    expires_on = fields.DateField(null=True)
    remarks = fields.CharField(max_length=255, null=True)
    source = fields.CharField(max_length=10, default="branch")

    class Meta:
        table = "branch_stock_movements"
        # Not unique on a document number: their ledger has several rows per document, one per Item, and no key of
        # its own that survives a reload. A window is replaced by day range, which is how the figures work too.
        indexes = (("branch", "day"), ("branch", "product_sku"))
        ordering = ["-at"]
