"""Two derived tables that exist only so the dashboard does not read a year of Item-days to answer one question.

`branch_product_stats` holds one row per branch per Item per day: 1,818,232 of them after the Multi-Tec history
landed, and growing by about 2,500 a day. Every stock question the Company Dashboard asks is a sum over a range of
those, and for a year view that is the whole table, several times over, on every page load.

Neither table holds anything new. Both are sums of `branch_product_stats` and can be thrown away and rebuilt at any
time, which is what makes them safe: if they are ever wrong, the fix is to rebuild them, not to reconcile them.

  branch_product_months     one row per branch per Item per calendar month. About 413,000 rows against 1.8 million.
                            A range that covers whole months is answered from here; the partial month at each end
                            still comes from the day-level table, because a month cut in half is not a month.
  branch_product_first_sale one row per branch per Item: the first day it ever sold. The query this replaces has no
                            lower bound at all, so it scans the entire history on every render regardless of the
                            period asked for, twice, at about 2.5 seconds each.

WHAT THEY MUST NOT BE USED FOR
------------------------------
**The XYZ demand buckets.** `analytics_service.bucket_index` counts whole months BACK FROM the period's last day:
17 August to 16 September is one bucket. Calendar months are a different set of boundaries, and swapping one for
the other moves Items between demand classes, which drive buying decisions. The rollup is calendar-month grained
and cannot answer that question, so it is not asked.
"""
from tortoise import fields, models


class BranchProductMonth(models.Model):
    """One branch, one Item, one calendar month: what it sold, what it made, what it cost."""

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="product_months"
    )
    # 'YYYY-MM'. A string because that is what the day column is sliced from, so the two always agree.
    ym = fields.CharField(max_length=7)
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    department = fields.CharField(max_length=80, null=True)
    category = fields.CharField(max_length=80, null=True)
    brand = fields.CharField(max_length=80, null=True)
    qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    cogs = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # How many days in the month it sold on at all. Kept because a monthly total cannot be taken apart afterwards.
    days_sold = fields.IntField(default=0)
    built_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_product_months"
        unique_together = (("branch", "ym", "product_sku"),)
        indexes = (("ym",), ("branch", "ym"))


class BranchProductFirstSale(models.Model):
    """The first day a branch ever sold an Item, so nothing has to scan all of history to find out."""

    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="product_first_sales"
    )
    product_sku = fields.CharField(max_length=40)
    first_day = fields.DateField()
    built_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_product_first_sale"
        unique_together = (("branch", "product_sku"),)
        indexes = (("product_sku",),)
