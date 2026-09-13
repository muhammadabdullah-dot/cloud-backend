"""What a branch is actually holding, item by item.

Until now the Cloud knew a branch's stock as a single number — one `stock_value` on the latest
trading day. That is enough to say "we hold Rs 3.9 crore of stock" and nothing else. It cannot
answer "which items", and every question worth asking about stock is an item-level question: what
is dead, what is overstocked, what has not moved since March.

So this table exists, and it is why ABC, dead stock and slow-moving become possible at all.

**Snapshot generations.** A branch holds tens of thousands of lines, so a push arrives in chunks
over several requests. If the branch's link dies halfway, the naive "delete then insert" leaves the
Cloud showing half a shop — which is worse than showing yesterday's complete picture, because it
looks complete. Every row therefore carries the `snapshot_id` of the push that wrote it; the branch
calls `complete` at the end, and only then are the previous generation's rows dropped. A push that
never finishes leaves the last good picture exactly where it was.
"""
from tortoise import fields, models


class BranchProductStock(models.Model):
    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="product_stock", on_delete=fields.CASCADE)
    # Which push wrote this row. Rows from superseded pushes are deleted on completion.
    snapshot_id = fields.CharField(max_length=60)

    product_sku = fields.CharField(max_length=60)
    product_name = fields.CharField(max_length=200)
    department = fields.CharField(max_length=120, null=True)
    category = fields.CharField(max_length=120, null=True)
    brand = fields.CharField(max_length=120, null=True)

    qty = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    # Both carried: value at cost is what the money is worth to the business, value at retail is
    # what the shelf says. Dead stock is argued about in the first and noticed in the second.
    avg_cost = fields.DecimalField(max_digits=16, decimal_places=4, default=0)
    price = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    # Movement facts the branch already knows and the Cloud would otherwise have to infer. Shipping
    # them costs nothing and means "last sold 240 days ago" survives even for a product whose sales
    # predate the window of daily stats the Cloud keeps.
    last_sold_at = fields.DatetimeField(null=True)
    last_received_at = fields.DatetimeField(null=True)
    units_sold_period = fields.DecimalField(max_digits=16, decimal_places=3, default=0)
    days_with_sales = fields.IntField(default=0)

    class Meta:
        table = "branch_product_stock"
        unique_together = (("branch", "snapshot_id", "product_sku"),)
        indexes = (("branch", "snapshot_id"),)


class BranchSnapshotRun(models.Model):
    """One complete picture-taking. Kept so "how fresh is this stock list" has an answer that does
    not depend on guessing from row timestamps."""

    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="snapshot_runs", on_delete=fields.CASCADE)
    snapshot_id = fields.CharField(max_length=60)
    started_at = fields.DatetimeField(auto_now_add=True)
    completed_at = fields.DatetimeField(null=True)
    stock_rows = fields.IntField(default=0)
    trading_days = fields.IntField(default=0)
    product_days = fields.IntField(default=0)
    # 'building' while chunks are arriving, 'complete' once the branch says so.
    status = fields.CharField(max_length=20, default="building")
    source = fields.CharField(max_length=20, default="sync")

    class Meta:
        table = "branch_snapshot_runs"
        ordering = ["-started_at"]
