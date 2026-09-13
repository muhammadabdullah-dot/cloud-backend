"""The grain beneath BranchDailyStat.

A daily total answers "how much"; none of these answer "why", and an executive who cannot get
from a number to its explanation will stop trusting the number. Each of these tables is the level
a drill-down lands on when a row is clicked:

    Bills Per Hour   → BranchHourlyStat   (the trading-hour profile, not invoices ÷ a guess)
    Till Variance    → BranchTillClose    (which close, whose, how far out)
    Cash Position    → BranchTenderStat   (cash vs card vs credit)
    Gross Profit     → BranchDiscountOverride (who authorised margin give-away)
    Customer Count   → BranchCreditCustomer   (who owes, against what limit)
    Returns          → BranchReturn       (what came back, off whose till)

All still populated by `scripts/import_branch_snapshot.py`. Nothing here is computed by the Cloud;
it is what a branch reported, at a finer grain.
"""
from tortoise import fields, models


class BranchHourlyStat(models.Model):
    """Invoices and sales by hour of the local trading day.

    `hour` is the branch's own local hour, converted at import. Bills-per-hour computed against
    measured trading hours is a real figure; computed against an assumed shift length it is a
    guess wearing a number's clothes.
    """
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="hourly_stats"
    )
    day = fields.DateField()
    hour = fields.IntField()
    invoices = fields.IntField(default=0)
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_hourly_stats"
        unique_together = (("branch", "day", "hour"),)
        ordering = ["day", "hour"]


class BranchTillClose(models.Model):
    """One drawer, one shift, one variance — the row a Till Variance figure is made of."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="till_closes"
    )
    day = fields.DateField()
    session_number = fields.CharField(max_length=40)
    cashier_name = fields.CharField(max_length=140)
    opened_at = fields.DatetimeField(null=True)
    closed_at = fields.DatetimeField(null=True)
    opening_float = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_cash = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    counted_cash = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    variance = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_till_closes"
        ordering = ["-closed_at"]


class BranchTenderStat(models.Model):
    """How customers actually paid. Cash, card and credit have completely different consequences
    for working capital, so a single "sales" figure hides the question that matters."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="tender_stats"
    )
    day = fields.DateField()
    code = fields.CharField(max_length=40)
    name = fields.CharField(max_length=80)
    uses = fields.IntField(default=0)
    amount = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_tender_stats"
        unique_together = (("branch", "day", "code"),)


class BranchDiscountOverride(models.Model):
    """A discount above a cashier's own authority, and the manager who approved it. Margin given
    away on someone's signature is exactly what an executive wants to be able to see by name."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="discount_overrides"
    )
    day = fields.DateField()
    invoice_number = fields.CharField(max_length=40)
    at = fields.DatetimeField(null=True)
    cashier_name = fields.CharField(max_length=140, null=True)
    approved_by = fields.CharField(max_length=140, null=True)
    gross = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    disc_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_value = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_discount_overrides"
        ordering = ["-at"]


class BranchReturn(models.Model):
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="returns"
    )
    day = fields.DateField()
    against_invoice = fields.CharField(max_length=40, null=True)
    at = fields.DatetimeField(null=True)
    cashier_name = fields.CharField(max_length=140, null=True)
    refund_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    product_name = fields.CharField(max_length=200, null=True)
    product_sku = fields.CharField(max_length=60, null=True)
    qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)

    class Meta:
        table = "branch_returns"
        ordering = ["-at"]


class BranchCreditCustomer(models.Model):
    """Point-in-time, not daily: the question is "who owes us how much right now", and a row per
    customer per day would be storage nobody reads."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="credit_customers"
    )
    code = fields.CharField(max_length=40)
    name = fields.CharField(max_length=180)
    phone = fields.CharField(max_length=40, null=True)
    tier = fields.CharField(max_length=20, null=True)
    credit_limit = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    credit_balance = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_credit_customers"
        ordering = ["-credit_balance"]
