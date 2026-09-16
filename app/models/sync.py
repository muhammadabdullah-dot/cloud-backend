"""What arrives from a branch, and the record of it arriving.

`SyncInboxEvent` is the landing pad for a branch's outbox. It is deliberately a *store*, not a
projection: the row holds the event exactly as the branch sent it, keyed by the branch's own event
id. Nothing here writes to Cloud's business tables yet — that projection is the next piece of work,
and doing it in the same step as the transport would mean a transport bug and a mapping bug arrive
together and look like each other.

Storing raw first is also what makes the transport replayable. If the projection turns out to be
wrong we re-run it over the inbox instead of asking every branch to send its history again.

Idempotency lives on `(branch, event_id)`. A branch that pushes, loses the connection before it
hears "ok", and pushes the same batch again on the next tick lands zero duplicates — the second
attempt matches rows that already exist and is counted as a duplicate, not an insert. That matters
because "did the push succeed" is exactly the question a flaky branch link cannot answer.
"""
from tortoise import fields, models


class SyncInboxEvent(models.Model):
    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="inbox_events", on_delete=fields.CASCADE)
    # The branch's own OutboxEvent id. Unique per branch — that pair is the idempotency key.
    event_id = fields.CharField(max_length=60)
    aggregate_type = fields.CharField(max_length=60)
    aggregate_id = fields.CharField(max_length=60)
    payload = fields.JSONField()
    origin_user_id = fields.CharField(max_length=60, null=True)
    origin_device_id = fields.CharField(max_length=80, null=True)
    # When the branch recorded it, not when we received it. Both are kept: the gap between them is
    # how long the branch was offline, which is a real operational fact worth being able to see.
    occurred_at = fields.DatetimeField(null=True)
    received_at = fields.DatetimeField(auto_now_add=True)
    # 'stored' until a projector consumes it; then 'applied', 'failed' (with apply_error), or left
    # 'stored' when no projector handles its type yet — kept, so it can be projected later.
    status = fields.CharField(max_length=20, default="stored")
    apply_error = fields.TextField(null=True)

    class Meta:
        table = "sync_inbox_events"
        unique_together = (("branch", "event_id"),)
        ordering = ["received_at"]


class SyncRun(models.Model):
    """One push from one branch. The audit trail behind "when did this branch last reach us" —
    `Branch.last_seen_at` answers that in one field, but only this says how much came, how much was
    already here, and what failed."""

    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="sync_runs", on_delete=fields.CASCADE)
    received_at = fields.DatetimeField(auto_now_add=True)
    event_count = fields.IntField(default=0)
    accepted_count = fields.IntField(default=0)
    duplicate_count = fields.IntField(default=0)
    status = fields.CharField(max_length=20, default="ok")
    note = fields.TextField(null=True)

    class Meta:
        table = "sync_runs"
        ordering = ["-received_at"]
