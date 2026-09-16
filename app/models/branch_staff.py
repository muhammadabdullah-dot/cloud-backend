"""Branch staff as head office sees them: every account on every branch, in one directory.

These are not Cloud App users — a salesperson never signs in to head office. They are the accounts
that live in each branch's own database, mirrored here so head office can see who works where and
change it: add someone, give them a second branch, change their role or access, reset a password,
switch them off. Every such change goes down to the branches the person is assigned to.

The id is the branch account's own id, the same on every branch that has the person, so a person
who works at two branches is one row here and one account at each branch.

**Who wins when both sides change the same person.** Each account carries a revision number. Every
edit — at a branch or here — raises it by one. The higher revision wins wherever it arrives; on an
exact tie head office's version wins. Nothing is merged field by field: an account is taken whole,
so a half-applied mix of two people's edits can't happen.
"""
from tortoise import fields, models


class BranchStaff(models.Model):
    id = fields.CharField(max_length=60, pk=True)
    name = fields.CharField(max_length=120)
    email = fields.CharField(max_length=180)
    role_id = fields.CharField(max_length=40)
    active = fields.BooleanField(default=True)
    # bcrypt, the same scheme both servers use, so the hash signs the person in wherever it lands.
    password_hash = fields.CharField(max_length=255)
    # [{"resource": "store.billing", "actions": ["R", "W", "X"]}, …]
    permissions = fields.JSONField(default=list)
    rev = fields.IntField(default=1)
    # 'branch' when last changed at a branch, 'cloud' when last changed here.
    last_changed_at = fields.CharField(max_length=10, default="branch")
    last_changed_by = fields.CharField(max_length=160, null=True)
    updated_at = fields.DatetimeField(auto_now=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_staff"
        ordering = ["name"]


class BranchRoleTemplate(models.Model):
    """A branch role's standard access, as head office has set it. Starts as the branch software's own
    template (from the branch's manifest); once head office edits it, every branch uses this version
    and stops adding the software's defaults back on its own."""

    role_id = fields.CharField(max_length=40, pk=True)
    name = fields.CharField(max_length=80)
    resources = fields.JSONField(default=list)
    updated_at = fields.DatetimeField(auto_now=True)
    updated_by = fields.CharField(max_length=160, null=True)

    class Meta:
        table = "branch_role_templates"
        ordering = ["name"]


class BranchStaffAssignment(models.Model):
    """A person works at a branch. Removing the row takes their account out of that branch — switched
    off there, never deleted, because their sales and counts still name them."""

    id = fields.UUIDField(pk=True)
    staff = fields.ForeignKeyField("models.BranchStaff", related_name="assignments", on_delete=fields.CASCADE)
    branch = fields.ForeignKeyField("models.Branch", related_name="staff_assignments", on_delete=fields.CASCADE)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "branch_staff_assignments"
        unique_together = (("staff", "branch"),)
