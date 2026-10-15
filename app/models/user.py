from tortoise import fields, models


class User(models.Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=120)
    email = fields.CharField(max_length=180, unique=True)
    password_hash = fields.CharField(max_length=255)
    role: fields.ForeignKeyRelation["Role"] = fields.ForeignKeyField(
        "models.Role", related_name="users"
    )
    active = fields.BooleanField(default=True)
    # The largest purchase order this person may approve. Null = their role's usual limit.
    po_limit = fields.DecimalField(max_digits=16, decimal_places=2, null=True)
    # THE ONE BRANCH THIS PERSON MAY BUY FOR
    # --------------------------------------
    # Null for everybody who buys for the company, which is everybody today. Set, it is a head office account that
    # may raise purchase orders for that branch and no other, and whose Purchase Orders screen shows that branch's
    # orders only.
    #
    # It exists for the pharmacy case agreed on 12 October 2026. Pharmacy moves daily, and asking the branch to
    # request, head office to buy, the godown to receive and someone to transfer is three days of road for a one
    # day item. So one person holds a head office account scoped to one branch. What moves is who holds the
    # account, not where the software runs: the branch application still has no purchasing in it at all.
    #
    # It is a scope, not a permission. What this person may DO is still their grants in `user_permissions`; this
    # only narrows who they may do it for. Both have to be set for the account to be useful, which is why the
    # screen that creates one does both at once.
    po_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="po_users", null=True, on_delete=fields.NO_ACTION
    )
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "users"
