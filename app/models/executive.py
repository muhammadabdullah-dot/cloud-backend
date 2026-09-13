"""Branch performance as the Cloud knows it.

These tables are the Cloud's *view* of what each branch reported. They are not the branch's own
records — those live in that branch's own database, which this service never reads at runtime
(contracts.md §1: two services, no shared database). Until the sync protocol lands, they are
populated by `scripts/import_branch_snapshot.py`, a one-shot importer that does by hand what the
sync worker will do continuously.

`BranchDailyStat.day` is the grain everything else folds from: today, yesterday, this week, this
month, this year are all sums over a date range of these rows, so no KPI needs its own storage.

Every read of these carries `asOf` to the client, because a figure that was true at the last sync
and a figure that is true now are different claims and an executive screen must not blur them.
"""
from tortoise import fields, models

ALERT_KINDS = ("out-of-stock", "low-stock", "near-expiry", "expired")


class BranchDailyStat(models.Model):
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="daily_stats"
    )
    day = fields.DateField()

    # Trading
    gross_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    disc_total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    gst = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    invoices = fields.IntField(default=0)
    items_sold = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    # Cost of what was sold, from each product's weighted-average cost. Gross profit is
    # net_sales - cogs; net profit needs operating expenses, which no module records yet.
    cogs = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    returns_value = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    returns_count = fields.IntField(default=0)

    # Money
    cash_collected = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    credit_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    cash_in = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    cash_out = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    till_variance = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    tills_closed = fields.IntField(default=0)

    # People and pace. The two timestamps are what make "bills per hour" a measured figure
    # rather than invoices divided by an assumed shift length.
    staff_on_duty = fields.IntField(default=0)
    first_sale_at = fields.DatetimeField(null=True)
    last_sale_at = fields.DatetimeField(null=True)
    named_customers = fields.IntField(default=0)

    # Stock on the branch floor. A right-now figure, not a daily one — the importer writes it
    # only onto each branch's most recent reported day, so summing a date range can't multiply
    # it by the number of days in the range.
    stock_value = fields.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        table = "branch_daily_stats"
        unique_together = (("branch", "day"),)
        ordering = ["-day"]


class BranchCashierStat(models.Model):
    """Per cashier per day — what "Best Cashier" is ranked from, and what makes the ranking
    auditable rather than a single name with no workings."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="cashier_stats"
    )
    day = fields.DateField()
    cashier_name = fields.CharField(max_length=140)
    invoices = fields.IntField(default=0)
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    till_variance = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_cashier_stats"
        unique_together = (("branch", "day", "cashier_name"),)


class BranchProductStat(models.Model):
    """Per product per day. Top Products, Top Categories and Top Brands are all folds over this
    one table — the taxonomy travels with the row so the Cloud can group by it without needing
    the branch's catalog."""
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="product_stats"
    )
    day = fields.DateField()
    product_sku = fields.CharField(max_length=60)
    product_name = fields.CharField(max_length=200)
    department = fields.CharField(max_length=120, null=True)
    category = fields.CharField(max_length=120, null=True)
    brand = fields.CharField(max_length=120, null=True)
    qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    cogs = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_product_stats"
        unique_together = (("branch", "day", "product_sku"),)


class BranchStockAlert(models.Model):
    """Current stock exceptions at a branch — out of stock, running low, near expiry, expired.

    Point-in-time, not daily: an executive asks "what is wrong right now", and keeping a row per
    day per item would be a lot of storage to answer a question nobody asks of the past. Capped
    per branch per kind by the importer, since a real catalog can put thousands of items under a
    low-stock line and a dashboard is a glance, not a work queue.
    """
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="stock_alerts"
    )
    kind = fields.CharField(max_length=20)
    product_sku = fields.CharField(max_length=60)
    product_name = fields.CharField(max_length=200)
    qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    expiry = fields.DatetimeField(null=True)
    detail = fields.CharField(max_length=200, null=True)
    # How many of this kind exist in total, even though only the most urgent rows are stored.
    # Without it a capped list silently understates the problem.
    total_of_kind = fields.IntField(default=0)

    class Meta:
        table = "branch_stock_alerts"
        ordering = ["kind", "qty"]
