"""Head office's fixed asset register: the truck, the godown racks, the office's computers and furniture, what each
cost, and how it is depreciated month by month. The same register as a branch's (see the branch server's
models/fixed_assets.py), kept in head office's own book "HO". A branch keeps its own register at the branch.

The register never writes into the books itself. Each month the software prepares one draft journal for the month's
depreciation (a DepreciationRun), and someone allowed to post checks and posts it; a disposal is a draft journal too.
Whether a run or a disposal counts is read from its voucher: posted counts, cancelled or reversed doesn't.
"""
from tortoise import fields, models

from app.models.accounts import HEAD_OFFICE_BOOK


class FixedAsset(models.Model):
    id = fields.UUIDField(pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    # FA-0001
    code = fields.CharField(max_length=20)
    name = fields.CharField(max_length=160)
    # The fixed asset account it sits in (VEHICLES, FURNITURE AND FIXTURES): its category on the register.
    category_account: fields.ForeignKeyRelation["Account"] = fields.ForeignKeyField(
        "models.Account", related_name="fixed_assets", on_delete=fields.RESTRICT
    )
    accumulated_account: fields.ForeignKeyRelation["Account"] = fields.ForeignKeyField(
        "models.Account", related_name="fixed_assets_accumulated", on_delete=fields.RESTRICT
    )
    expense_account: fields.ForeignKeyRelation["Account"] = fields.ForeignKeyField(
        "models.Account", related_name="fixed_assets_expense", on_delete=fields.RESTRICT
    )
    purchase_date = fields.DateField()
    cost = fields.DecimalField(max_digits=16, decimal_places=2)
    salvage_value = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    # Counted from the month it was bought.
    useful_life_months = fields.IntField()
    # straight · reducing
    method = fields.CharField(max_length=10, default="straight")
    # Reducing balance only: the share of the net book value charged in a year.
    rate_percent = fields.DecimalField(max_digits=6, decimal_places=2, null=True)
    # The first month the register charges (always the 1st). A full month's charge from this month.
    depreciation_start = fields.DateField()
    # Depreciation charged before depreciation_start: for something owned before the books started.
    opening_accumulated = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    location = fields.CharField(max_length=120, null=True)
    supplier_ref = fields.CharField(max_length=120, null=True)
    notes = fields.CharField(max_length=500, null=True)
    # in_use · disposed (disposed once its disposal voucher is posted)
    status = fields.CharField(max_length=10, default="in_use")
    disposal_date = fields.DateField(null=True)
    disposal_proceeds = fields.DecimalField(max_digits=16, decimal_places=2, null=True)
    # The cash or bank account the proceeds went into.
    disposal_account: fields.ForeignKeyNullableRelation["Account"] = fields.ForeignKeyField(
        "models.Account", related_name="fixed_asset_disposals", null=True, on_delete=fields.SET_NULL
    )
    disposal_voucher: fields.ForeignKeyNullableRelation["Voucher"] = fields.ForeignKeyField(
        "models.Voucher", related_name="fixed_asset_disposals", null=True, on_delete=fields.SET_NULL
    )
    created_by_name = fields.CharField(max_length=120, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "acc_fixed_assets"
        unique_together = (("book", "code"),)


class DepreciationRun(models.Model):
    """One month's depreciation, prepared as a draft journal. A month is taken while its voucher is a draft or posted."""

    id = fields.UUIDField(pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    # The 1st of the month.
    month = fields.DateField()
    voucher: fields.ForeignKeyNullableRelation["Voucher"] = fields.ForeignKeyField(
        "models.Voucher", related_name="depreciation_runs", null=True, on_delete=fields.SET_NULL
    )
    # draft · posted · cancelled · reversed, following the voucher
    status = fields.CharField(max_length=10, default="draft")
    total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    created_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="depreciation_runs", null=True, on_delete=fields.SET_NULL
    )
    created_by_name = fields.CharField(max_length=120, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "acc_depreciation_runs"
        indexes = (("book", "month", "status"),)


class DepreciationRunLine(models.Model):
    id = fields.UUIDField(pk=True)
    run: fields.ForeignKeyRelation[DepreciationRun] = fields.ForeignKeyField(
        "models.DepreciationRun", related_name="lines", on_delete=fields.CASCADE
    )
    asset: fields.ForeignKeyRelation[FixedAsset] = fields.ForeignKeyField(
        "models.FixedAsset", related_name="run_lines", on_delete=fields.RESTRICT
    )
    amount = fields.DecimalField(max_digits=16, decimal_places=2)
    # The asset's accumulated depreciation at the month's end, by its schedule.
    accumulated_after = fields.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        table = "acc_depreciation_run_lines"
