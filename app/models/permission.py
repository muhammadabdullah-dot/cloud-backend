from tortoise import fields, models


class UserPermission(models.Model):
    """The only table any authorization check reads (contracts.md §2.3) — per-user, not per-role."""

    id = fields.UUIDField(pk=True)
    # NO ACTION: SQL Server allows one cascade path per table, and nothing in the app deletes a user; a permission row is refused, not orphaned.
    user: fields.ForeignKeyRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="permissions"
    , on_delete=fields.NO_ACTION)
    resource = fields.CharField(max_length=120)
    can_read = fields.BooleanField(default=False)
    can_write = fields.BooleanField(default=False)
    can_execute = fields.BooleanField(default=False)
    granted_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="granted_permissions", null=True
    )
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "user_permissions"
        unique_together = (("user", "resource"),)
