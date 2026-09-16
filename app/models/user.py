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
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "users"
