"""What a branch's stock request carries beyond the one Item a `Requisition` row was first built for.

A request lists several Items, each with what the branch holds and sells a day, a reason and a needed-by date, and
who is asked to send it: the central godown, or another branch. Head office approves it with the quantities it can
send (0 leaves an Item out) or declines it with a reason. The `Requisition` row stays the request itself, so every
screen and count that reads requisitions keeps working; it holds the first line in its own Item columns.
"""
from tortoise import fields, models


class RequisitionDetail(models.Model):
    id = fields.UUIDField(pk=True)
    requisition: fields.OneToOneRelation["Requisition"] = fields.OneToOneField(
        "models.Requisition", related_name="detail", on_delete=fields.CASCADE
    )
    # Null: the central godown sends it. Otherwise the branch asked to send it, which dispatches it itself.
    source_branch: fields.ForeignKeyNullableRelation["Branch"] = fields.ForeignKeyField(
        "models.Branch", related_name="requisitions_to_send", null=True, on_delete=fields.SET_NULL
    )
    # "branch": raised at the branch and synced up · "head-office": recorded here for the branch.
    origin = fields.CharField(max_length=12, default="branch")
    branch_number = fields.CharField(max_length=30, null=True)
    reason = fields.CharField(max_length=255, null=True)
    needed_by = fields.DateField(null=True)
    requested_by_name = fields.CharField(max_length=120, null=True)
    received_at = fields.DatetimeField(null=True)
    decided_by_name = fields.CharField(max_length=120, null=True)
    # Why it was declined, what was changed on approval, or why the branch withdrew it.
    decision_note = fields.CharField(max_length=255, null=True)

    class Meta:
        table = "requisition_details"


class RequisitionLine(models.Model):
    id = fields.UUIDField(pk=True)
    requisition: fields.ForeignKeyRelation["Requisition"] = fields.ForeignKeyField(
        "models.Requisition", related_name="lines", on_delete=fields.CASCADE
    )
    product: fields.ForeignKeyRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="requisition_lines"
    )
    qty_requested = fields.DecimalField(max_digits=16, decimal_places=3)
    # Null until decided; 0 means head office left this Item out.
    qty_approved = fields.DecimalField(max_digits=16, decimal_places=3, null=True)
    # The branch's own figures when it sent the request.
    branch_on_hand = fields.DecimalField(max_digits=16, decimal_places=3, null=True)
    branch_daily_sales = fields.DecimalField(max_digits=16, decimal_places=3, null=True)
    sort_order = fields.IntField(default=0)

    class Meta:
        table = "requisition_lines"
        ordering = ["sort_order"]
