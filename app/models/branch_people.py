"""A branch's own staff roster, as the branch kept it. Not a sign-in.

Head office already has two tables that look like they would do and neither does:

* `users` is head office's own logins, and `branch_staff` is a branch's logins. **Both require an email and a
  password hash**, because both exist so somebody can sign in. Nobody from the old software arrives with a password of
  theirs and nobody is given a new one, so filing them there would mean inventing 138 email addresses and 138
  credentials for people who must not have any.
* `branch_cashier_stats` and `branch_sales.cashier_name` already carry who rang what. That is the evidence, not the
  roster: it can tell you Ali sold Rs 40,000 on Tuesday and cannot tell you Ali existed, what his role was, or that
  he left in March.

So a roster is its own thing. They come across as records because 1,036,271 bills name who rang them, and a bill
whose cashier is gone is a bill nobody can answer for. Anyone who needs to sign in is set up later, by somebody who
knows they should, in `branch_staff` where a sign-in belongs.
"""
from tortoise import fields, models


class BranchUser(models.Model):
    id = fields.UUIDField(pk=True)
    branch: fields.ForeignKeyRelation["Branch"] = fields.ForeignKeyField(  # noqa: F821
        "models.Branch", related_name="roster"
    )
    # The branch's own user number, which is what its documents refer to.
    code = fields.CharField(max_length=40)
    name = fields.CharField(max_length=140)
    # Their role as they had it: ADMINISTRATOR, SALE ( POS ), ACCOUNT HEAD, OPERATION MANAGER. Kept as their words,
    # not translated into one of ours, because theirs is what explains what somebody was allowed to do.
    role_name = fields.CharField(max_length=120, null=True)
    active = fields.BooleanField(default=True)
    # When this person last did anything the branch recorded, which is what tells you whether they are still there.
    last_active_on = fields.DateField(null=True)
    # Explicit, so nothing ever reads this row as a credential.
    can_sign_in = fields.BooleanField(default=False)
    source = fields.CharField(max_length=10, default="branch")
    received_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "branch_users"
        unique_together = (("branch", "code"),)
        ordering = ["name"]
