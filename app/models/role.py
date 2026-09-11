from tortoise import fields, models


class Role(models.Model):
    id = fields.CharField(max_length=40, pk=True)
    name = fields.CharField(max_length=80)
    landing = fields.CharField(max_length=120)

    class Meta:
        table = "roles"


class RoleDefaultPermission(models.Model):
    """Seed-time template only — copied onto a user at creation, never read at request time."""

    id = fields.UUIDField(pk=True)
    role: fields.ForeignKeyRelation[Role] = fields.ForeignKeyField(
        "models.Role", related_name="default_permissions"
    )
    resource = fields.CharField(max_length=120)
    can_read = fields.BooleanField(default=False)
    can_write = fields.BooleanField(default=False)
    can_execute = fields.BooleanField(default=False)

    class Meta:
        table = "role_default_permissions"
        unique_together = (("role", "resource"),)
