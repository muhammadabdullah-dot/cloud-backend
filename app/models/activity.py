"""Every action taken at every branch, and who took it — as each branch recorded it.

A branch writes one row for every sale, price change, count, till close, access change, sign-in and export,
and sends it here. Head office keeps them all, so any activity at any branch can be traced to a person.
"""
from tortoise import fields, models


class BranchActivity(models.Model):
    # The branch's own id for the row, which is what makes a resent row harmless.
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="activities", on_delete=fields.CASCADE
    )
    at = fields.DatetimeField(index=True)
    # The person's id at that branch, and their name and title as they were at the time.
    user_id = fields.CharField(max_length=40, null=True, index=True)
    user_name = fields.CharField(max_length=180, null=True)
    user_title = fields.CharField(max_length=80, null=True)
    action = fields.CharField(max_length=120)
    method = fields.CharField(max_length=10)
    route = fields.CharField(max_length=160, null=True)
    path = fields.CharField(max_length=255)
    params = fields.JSONField(null=True)
    status_code = fields.IntField()
    detail = fields.JSONField(null=True)
    device_id = fields.CharField(max_length=80, null=True)
    ip = fields.CharField(max_length=60, null=True)
    received_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_activities"
