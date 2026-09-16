"""D.Marina members and their points, as head office knows them across every branch.

Branches sign members up and record points; each change arrives here as an event and is passed on to
the other branches, so a member who joined at Model Town has their points at Fort Colony too. A member
keeps the code their branch gave them; a points entry keeps the id its branch gave it, which is what
stops the same earning from being counted twice anywhere.
"""
from tortoise import fields, models


class Member(models.Model):
    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=20, unique=True)
    name = fields.CharField(max_length=120)
    # Not unique here: two branches offline can each sign up the same number before hearing of the other.
    phone = fields.CharField(max_length=20, index=True)
    joined_via = fields.CharField(max_length=20)
    joined_method = fields.CharField(max_length=20, null=True)
    home_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="members", null=True, on_delete=fields.SET_NULL
    )
    home_branch_code = fields.CharField(max_length=10)
    # The branch Party the member is linked to, as that branch named it.
    party_code = fields.CharField(max_length=20, null=True)
    party_name = fields.CharField(max_length=160, null=True)
    points_balance = fields.IntField(default=0)
    active = fields.BooleanField(default=True)
    created_by_name = fields.CharField(max_length=120, null=True)
    created_at = fields.DatetimeField(null=True)
    # The branch's own clock for the last change — the later change wins.
    updated_at = fields.DatetimeField(null=True)
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "members"


class LoyaltyEntry(models.Model):
    id = fields.UUIDField(pk=True)
    member: fields.ForeignKeyRelation[Member] = fields.ForeignKeyField(
        "models.Member", related_name="entries", on_delete=fields.CASCADE
    )
    kind = fields.CharField(max_length=10)
    points = fields.IntField()
    invoice_number = fields.CharField(max_length=30, null=True)
    branch_code = fields.CharField(max_length=10)
    note = fields.CharField(max_length=255, null=True)
    by_name = fields.CharField(max_length=120, null=True)
    at = fields.DatetimeField()

    class Meta:
        table = "loyalty_entries"


class LoyaltySettings(models.Model):
    id = fields.IntField(pk=True)
    enabled = fields.BooleanField(default=True)
    rupees_per_point = fields.DecimalField(max_digits=10, decimal_places=2, default=100)
    point_value = fields.DecimalField(max_digits=10, decimal_places=2, default=1)
    min_redeem_points = fields.IntField(default=100)
    max_redeem_percent = fields.DecimalField(max_digits=5, decimal_places=2, default=50)
    updated_at = fields.DatetimeField(null=True)
    updated_by_name = fields.CharField(max_length=120, null=True)
    # HO for head office, or the branch code that set them.
    updated_from = fields.CharField(max_length=10, null=True)

    class Meta:
        table = "loyalty_settings"
