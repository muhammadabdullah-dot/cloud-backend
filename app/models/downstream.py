"""What head office has to tell a branch — the downstream half of sync.

A branch server sits behind a shop's router: head office can't call it. So everything bound for a
branch waits here, numbered per branch, and the branch collects it on its own schedule (every couple
of minutes while it's online) with `GET /sync/pull?after=<last seq it applied>`.

Numbering is what makes this safe on a bad link. A branch that loses the connection after applying
message 41 but before telling us simply asks for "after 40" or "after 41" next time; every message is
an idempotent upsert of whole state (a transfer as it now stands, a staff account as it now stands),
so seeing one twice changes nothing.

Messages are never deleted. `applied_at` / `apply_error` record what the branch said happened, which
is what an admin looks at when a change "didn't reach" a branch.
"""
from tortoise import fields, models


class BranchMessage(models.Model):
    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="messages", on_delete=fields.CASCADE)
    # 1, 2, 3 … per branch. The branch's cursor.
    seq = fields.IntField()
    # transfer.inbound · transfer.outbound · staff.upsert · staff.remove · role.template
    kind = fields.CharField(max_length=40)
    payload = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)
    delivered_at = fields.DatetimeField(null=True)
    applied_at = fields.DatetimeField(null=True)
    apply_error = fields.TextField(null=True)

    class Meta:
        table = "branch_messages"
        unique_together = (("branch", "seq"),)
        ordering = ["seq"]


class BranchManifest(models.Model):
    """What a branch server's code says about itself: the screens and actions it can grant, and its
    roles with their standard access. Head office edits branch staff access against this list, so it
    can never offer a permission the branch software doesn't have."""

    id = fields.UUIDField(pk=True)
    branch = fields.OneToOneField("models.Branch", related_name="manifest", on_delete=fields.CASCADE)
    resources = fields.JSONField()
    roles = fields.JSONField()
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_manifests"
