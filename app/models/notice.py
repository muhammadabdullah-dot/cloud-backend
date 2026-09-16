"""Things people at head office should know about, and who has seen them.

A notice is "for your information": a branch agreed to a shipment, declined one, received it short, an order
was approved. What someone must *do* is not stored here — those are worked out from each record's state every
time (see alerts_service), so they go away the moment someone acts and can never go stale.

Who a notice is for is described, not listed: anyone holding one of the named abilities, plus any named
people. Someone given the ability later sees it too.
"""
from tortoise import fields, models


class Notice(models.Model):
    id = fields.UUIDField(pk=True)
    at = fields.DatetimeField(auto_now_add=True, index=True)
    # What happened: "transfer.declined", "po.approved".
    kind = fields.CharField(max_length=40)
    title = fields.CharField(max_length=200)
    body = fields.CharField(max_length=500, null=True)
    # The screen to open, as an app path.
    link = fields.CharField(max_length=200, null=True)
    # info · good · warning · bad
    tone = fields.CharField(max_length=10, default="info")
    subject_type = fields.CharField(max_length=40, null=True)
    subject_id = fields.CharField(max_length=60, null=True)
    # {"any": [[resource, action], ...], "users": [user id, ...]}
    audience = fields.JSONField(default=dict)

    class Meta:
        table = "notices"


class NoticeRead(models.Model):
    id = fields.UUIDField(pk=True)
    notice: fields.ForeignKeyRelation[Notice] = fields.ForeignKeyField("models.Notice", related_name="reads", on_delete=fields.CASCADE)
    user: fields.ForeignKeyRelation["User"] = fields.ForeignKeyField("models.User", related_name="notice_reads", on_delete=fields.CASCADE)
    read_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "notice_reads"
        unique_together = (("notice", "user"),)
