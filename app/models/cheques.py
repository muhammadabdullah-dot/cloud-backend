"""Cheques at head office: the post dated cheque register, and what makes a bank reconcilable.

The branch server has held cheques since the beginning. Head office has not, and that is the wrong way round: the
accountant sits at head office, on DMHOSERVER, and the old software runs its whole post dated cheque register and its
bank reconciliation statement from there with a branch dropdown. Nobody at a branch opens the books at all.

So a cheque here is book-scoped exactly as a `Voucher` is: `book` is `HO` for head office's own, or a branch's code
for one sent up. The register reads every book at once, or one at a time, the same way every other accounts report
at head office already does.

**Four dates, not one.** The old software's post dated cheque report filters on *record date*, *due date*, *finalize
date* and *cancel date*, and a shop uses all four: what was written this month, what falls due next month, what
actually cleared, what was torn up. Our branch cheque carries the first three under other names and no fourth, so
the two extra columns are here and the branch's three are kept under their own names rather than renamed, because a
renamed column is a silent mismatch the day a branch cheque syncs up.

**Posted and finalized are separate from cleared.** Their register asks "posted yes/no" and "finalize yes/no" as two
questions, because a cheque can be entered and not yet put in the books, and put in the books and not yet cleared.
Collapsing them into one status loses the middle state, which is precisely the state an accountant chases.

Nothing here is computed from a branch's figures; a cheque arrives as the branch recorded it, or is written here.
"""
from tortoise import fields, models

from app.models.accounts import HEAD_OFFICE_BOOK

# What a cheque can be at any moment. `pending` covers both "held" and "deposited, waiting"; which one it is shows in
# whether `deposited_on` is set, exactly as the branch has it.
STATUSES = ("pending", "cleared", "bounced", "cancelled")
DIRECTIONS = ("received", "issued")


class Cheque(models.Model):
    """One cheque: taken from a customer, or written to a supplier.

    `source` is how the same cheque arriving twice stays one row: a branch's is `"<book>:<its id>"`, a legacy import's
    is `"legacy:<their key>"`, and head office's own is null. Unique, so a replay updates rather than doubles.
    """

    id = fields.UUIDField(pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    number = fields.CharField(max_length=30)
    source = fields.CharField(max_length=160, unique=True, null=True)
    # received · issued
    direction = fields.CharField(max_length=10, default="received")
    # The customer it came from, or the supplier it was written to.
    party_account: fields.ForeignKeyRelation["Account"] = fields.ForeignKeyField(  # noqa: F821
        "models.Account", related_name="cheques", on_delete=fields.RESTRICT
    )
    # The bank account it was deposited into, or is drawn on. Null while a received cheque is still in the drawer.
    bank_account: fields.ForeignKeyNullableRelation["Account"] = fields.ForeignKeyField(  # noqa: F821
        "models.Account", related_name="cheques_banked", null=True, on_delete=fields.SET_NULL
    )
    cheque_no = fields.CharField(max_length=30)
    # The bank it is drawn on, as written on the cheque. Free text: it is very often a bank we hold no account with.
    drawn_on = fields.CharField(max_length=80, null=True)
    amount = fields.DecimalField(max_digits=14, decimal_places=2)

    # ── the four dates their register filters on ──────────────────────────────────────────────
    # When it was written down. Their "Record Date".
    received_on = fields.DateField()
    # The date on the face of the cheque, which is what makes it post dated. Their "DueDate".
    cheque_date = fields.DateField()
    # When it actually cleared. Their "Finalize Date".
    cleared_on = fields.DateField(null=True)
    # When it was torn up. Their "Cancel Date". The branch has no such column, so a branch cheque arrives with it null
    # and its status simply says cancelled.
    cancelled_on = fields.DateField(null=True)
    bounced_on = fields.DateField(null=True)
    deposited_on = fields.DateField(null=True)

    # pending · cleared · bounced · cancelled
    status = fields.CharField(max_length=10, default="pending")
    # In the books or not. Separate from cleared on purpose: their register asks the two questions apart.
    posted = fields.BooleanField(default=False)
    finalized = fields.BooleanField(default=False)

    # Their Ref No 1 and Ref No 2, both filterable in the register. Two, not one, because a shop uses one for the
    # deposit slip and the other for the supplier's own reference, and merging them loses which is which.
    ref_no_1 = fields.CharField(max_length=60, null=True)
    ref_no_2 = fields.CharField(max_length=60, null=True)
    note = fields.CharField(max_length=255, null=True)

    # A bounced cheque deposited again is its own row; this names the one that bounced.
    redeposit_of_id = fields.CharField(max_length=36, null=True)
    created_by_name = fields.CharField(max_length=120, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "ho_cheques"
        unique_together = (("book", "number"),)
        indexes = (("book", "status", "cheque_date"), ("book", "direction", "cheque_date"))


class ChequeLine(models.Model):
    """Which invoice a cheque pays, and how much of it.

    Kept because their data has it: 3,616 of the old software's 4,010 post dated cheques name the invoices they
    cover, one of them naming ten. A cheque with no lines is legal, as 394 of theirs are; a cheque with lines has
    lines that add up to it, on all 3,616.
    """

    id = fields.UUIDField(pk=True)
    cheque: fields.ForeignKeyRelation[Cheque] = fields.ForeignKeyField("models.Cheque", related_name="lines", on_delete=fields.CASCADE)
    line_no = fields.IntField(default=1)
    # The other side's own invoice number, as written on it. Free text: a cheque very often pays invoices from before
    # any of this, for which no delivery of ours ever existed.
    invoice_no = fields.CharField(max_length=60)
    invoice_date = fields.DateField(null=True)
    invoice_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Paid, plus returned, plus what is left, is what was outstanding. That is the arithmetic somebody reads a year
    # later, and it needs all three.
    outstanding_before = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    return_amount = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_amount = fields.DecimalField(max_digits=14, decimal_places=2)
    note = fields.CharField(max_length=255, null=True)

    class Meta:
        table = "ho_cheque_lines"
        unique_together = (("cheque", "line_no"),)
