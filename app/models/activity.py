"""Every action taken at every branch, and who took it — as each branch recorded it.

A branch writes one row for every sale, price change, count, till close, access change, sign-in and export,
and sends it here. Head office keeps them all, so any activity at any branch can be traced to a person.
"""
from tortoise import fields, models
from tortoise.expressions import Q


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


# The path a branch's browser errors arrive on (the branch's own routes/client_errors.py).
CLIENT_ERROR_PATH = "/client-errors"


def failed_at_branch() -> Q:
    """What counts as a failure at a branch, for every screen and report that lists them.

    Two shapes, because the two are genuinely different events and are marked differently:

      * **The branch server could not answer.** The row carries its own 5xx. This covers a till, a report, a
        sign-in, and the branch's own background work -- its scheduler and nightly jobs write a row with no
        user and no device and a status of 500, which is the only way work nobody was watching can be seen
        from here at all.
      * **A screen broke in the browser.** That arrives as a *successful* POST to /client-errors -- the report
        worked, the screen did not -- so the status says 204 and the path is what identifies it.

    Both carry `detail.errorId`: the reference the person at the branch was shown, and the same reference in
    that branch's own log file. It is what turns "it said 7KQ2-M9XD" on the phone into the entry that explains
    it, which matters because nobody can open that machine.
    """
    return Q(status_code__gte=500) | Q(path=CLIENT_ERROR_PATH)
