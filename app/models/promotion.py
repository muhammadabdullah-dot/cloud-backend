"""A campaign, as head office writes it.

One campaign is one decision for the whole company, and that is not an opinion: the old software's campaign tables
have **no branch column at all**, seven people wrote all 11,364 of them, and every one of its five shops sold under
them (6,017 lines at Garden Town, 2,068, 1,506, 1,296 and 221 at the rest). Its shops share one database, so one list
reaches every till by itself. Ours cannot do that, so head office writes a campaign here and it travels down the way
a supplier does, as a `promotion.upsert` message.

An approval, because theirs has one and used it: 10,893 of their 11,364 campaigns are approved, 441 are not and 30
are marked not applicable. A campaign nobody approved is not sent to a single till.

What a campaign gives away comes back the other way, per branch per day, in the figures a branch already sends
(`BranchPromotionStat`). Head office can then say what a campaign cost and where.
"""
from tortoise import fields, models

KINDS = ("percent", "flat", "price")
# draft: being written · approved: may be sent · live: sent to the branches · stopped: withdrawn
STATES = ("draft", "approved", "live", "stopped")


class Promotion(models.Model):
    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=20, unique=True)
    name = fields.CharField(max_length=160)
    # The Item by its own code, which is what a branch knows it by: head office and every branch share the Item list,
    # and `Product.id` is its sku there and here.
    product_sku = fields.CharField(max_length=40)
    product_name = fields.CharField(max_length=200, null=True)
    starts_on = fields.DateField()
    ends_on = fields.DateField()
    kind = fields.CharField(max_length=10, default="percent")
    disc_percent = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    disc_flat = fields.DecimalField(max_digits=12, decimal_places=2, default=0)
    promo_price = fields.DecimalField(max_digits=12, decimal_places=2, null=True)
    # 1 means every one, which is what all 11,364 of theirs are. Kept because "three for the price of two" is the
    # first thing a shop asks for, not because they used it.
    min_qty = fields.DecimalField(max_digits=12, decimal_places=3, default=1)
    state = fields.CharField(max_length=10, default="draft")
    remarks = fields.CharField(max_length=255, null=True)
    # Every send bumps this, and a branch ignores a message older than the one it already applied, so a message that
    # arrives late or twice can never undo a newer one. The same guard the company supplier list uses.
    rev = fields.IntField(default=0)
    created_by_name = fields.CharField(max_length=120, null=True)
    approved_by_name = fields.CharField(max_length=120, null=True)
    approved_at = fields.DatetimeField(null=True)
    published_at = fields.DatetimeField(null=True)
    stopped_by_name = fields.CharField(max_length=120, null=True)
    stopped_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "promotions"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class BranchPromotionStat(models.Model):
    """What a campaign gave away at one branch on one day, as that branch reports it.

    A day-grouped figure like every other the branches send, so it is replaced when a day is sent again rather than
    added to, and a branch that has no campaigns sends nothing at all.
    """

    id = fields.IntField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="promotion_stats", on_delete=fields.CASCADE)
    day = fields.DateField()
    promotion_code = fields.CharField(max_length=20)
    promotion_name = fields.CharField(max_length=160, null=True)
    product_sku = fields.CharField(max_length=40, null=True)
    # Lines sold under it, the quantity on them, and what it took off.
    lines = fields.IntField(default=0)
    qty = fields.DecimalField(max_digits=14, decimal_places=3, default=0)
    given = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    # What those lines still came to after the campaign, so its cost can be read against what it sold.
    net_sales = fields.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        table = "branch_promotion_stats"
        unique_together = (("branch", "day", "promotion_code"),)
