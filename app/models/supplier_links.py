"""Which branch supplier is which company supplier, and the ones a person has to decide.

A branch keeps its own supplier ids (its GRNs and ledger accounts point at them). Head office remembers the pairing
here when it matched a branch's supplier to the company list or added it from that branch, so a change the branch
sends before it has heard the company identity still lands on the right supplier.

When a branch's supplier could be more than one supplier on the company list (the same name with a different phone,
two with the same NTN), head office doesn't guess: it keeps a question for the Warehouse Manager and holds the
candidates back from that branch until someone answers.
"""
from tortoise import fields, models


class SupplierBranchLink(models.Model):
    id = fields.UUIDField(pk=True)
    supplier: fields.ForeignKeyRelation["Supplier"] = fields.ForeignKeyField(
        "models.Supplier", related_name="branch_links", on_delete=fields.CASCADE
    )
    branch = fields.ForeignKeyField("models.Branch", related_name="supplier_links", on_delete=fields.CASCADE)
    # The branch's own supplier id.
    branch_supplier_id = fields.CharField(max_length=60)
    # "matched" (already on the list), "added" (joined the list from this branch), "decided" (a person said which) or
    # "copy" (the branch's copy of a supplier head office sent, first heard of when the branch changed it)
    how = fields.CharField(max_length=20)
    linked_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "supplier_branch_links"
        unique_together = (("branch", "branch_supplier_id"),)


class SupplierQuestion(models.Model):
    id = fields.UUIDField(pk=True)
    branch = fields.ForeignKeyField("models.Branch", related_name="supplier_questions", on_delete=fields.CASCADE)
    branch_supplier_id = fields.CharField(max_length=60)
    # The branch's supplier as it last sent it.
    branch_supplier = fields.JSONField()
    # Company supplier ids it could be, and those of them the branch hasn't been sent: held back until someone answers.
    candidates = fields.JSONField()
    held = fields.JSONField(default=list)
    reason = fields.CharField(max_length=255)
    # "open", then "same" (it is one of the candidates), "different" (added to the list) or "branch-only"
    status = fields.CharField(max_length=12, default="open")
    answer_supplier: fields.ForeignKeyNullableRelation["Supplier"] = fields.ForeignKeyField(
        "models.Supplier", related_name="answered_questions", null=True, on_delete=fields.SET_NULL
    )
    decided_by = fields.CharField(max_length=180, null=True)
    decided_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "supplier_questions"
        unique_together = (("branch", "branch_supplier_id"),)
