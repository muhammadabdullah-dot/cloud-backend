"""Head office's books, and a copy of every branch's.

The chart and vouchers are the same as a branch's (see the branch server's models/accounts.py): Account Type →
Category → Group → Sub Group → Account, and vouchers as the only way anything enters the books. Every row here
belongs to a **book**: "HO" is head office's own — the godown, supplier bills and payments made from head office,
head office's expenses — and each branch code ("MT", "FC") is that branch's books exactly as the branch posted
them, sent up by sync. Branch books are read-only here; they are corrected at the branch.

Because every book starts from the same standard chart, head office can add them together: the company's trial
balance, income statement and balance sheet are all the books at once.
"""
from tortoise import fields, models

HEAD_OFFICE_BOOK = "HO"


class AccountType(models.Model):
    code = fields.CharField(max_length=2, pk=True)
    name = fields.CharField(max_length=60)
    nature = fields.CharField(max_length=6)
    statement = fields.CharField(max_length=10)

    class Meta:
        table = "acc_types"


class AccountCategory(models.Model):
    code = fields.CharField(max_length=4, pk=True)
    name = fields.CharField(max_length=80)
    type: fields.ForeignKeyRelation[AccountType] = fields.ForeignKeyField("models.AccountType", related_name="categories")

    class Meta:
        table = "acc_categories"


class AccountGroup(models.Model):
    # "<book>:<code>" — the same code can mean a different group in another branch's books.
    id = fields.CharField(max_length=30, pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    code = fields.CharField(max_length=6)
    name = fields.CharField(max_length=100)
    category: fields.ForeignKeyRelation[AccountCategory] = fields.ForeignKeyField("models.AccountCategory", related_name="groups")
    priority = fields.IntField(default=0)
    manual_code = fields.CharField(max_length=30, null=True)
    standard = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "acc_groups"
        unique_together = (("book", "code"),)


class AccountSubGroup(models.Model):
    id = fields.CharField(max_length=30, pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    code = fields.CharField(max_length=8)
    name = fields.CharField(max_length=100)
    group: fields.ForeignKeyRelation[AccountGroup] = fields.ForeignKeyField("models.AccountGroup", related_name="sub_groups")
    standard = fields.BooleanField(default=False)

    class Meta:
        table = "acc_sub_groups"
        unique_together = (("book", "code"),)


class Account(models.Model):
    id = fields.UUIDField(pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    code = fields.CharField(max_length=12)
    name = fields.CharField(max_length=160)
    group: fields.ForeignKeyRelation[AccountGroup] = fields.ForeignKeyField("models.AccountGroup", related_name="accounts")
    sub_group: fields.ForeignKeyNullableRelation[AccountSubGroup] = fields.ForeignKeyField(
        "models.AccountSubGroup", related_name="accounts", null=True, on_delete=fields.SET_NULL
    )
    kind = fields.CharField(max_length=12, default="general")
    system_key = fields.CharField(max_length=80, null=True)
    party_ref = fields.CharField(max_length=60, null=True)
    active = fields.BooleanField(default=True)
    restricted = fields.BooleanField(default=False)
    check_limit = fields.BooleanField(default=False)
    balance_limit = fields.DecimalField(max_digits=16, decimal_places=2, null=True)
    bank_name = fields.CharField(max_length=80, null=True)
    bank_account_no = fields.CharField(max_length=40, null=True)
    manual_code = fields.CharField(max_length=30, null=True)
    remarks = fields.CharField(max_length=255, null=True)
    standard = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "acc_accounts"
        unique_together = (("book", "code"), ("book", "system_key"))


class Voucher(models.Model):
    id = fields.UUIDField(pk=True)
    book = fields.CharField(max_length=20, default=HEAD_OFFICE_BOOK)
    number = fields.CharField(max_length=30, unique=True)
    vtype = fields.CharField(max_length=4)
    date = fields.DateField()
    status = fields.CharField(max_length=10, default="draft")
    auto = fields.BooleanField(default=False)
    # Head office's own: "grn:<id>", "transfer-out:<id>". A branch's: "<book>:<its source>".
    source = fields.CharField(max_length=160, unique=True, null=True)
    source_hash = fields.CharField(max_length=64, null=True)
    header_account: fields.ForeignKeyNullableRelation[Account] = fields.ForeignKeyField(
        "models.Account", related_name="header_vouchers", null=True, on_delete=fields.SET_NULL
    )
    reference_no = fields.CharField(max_length=60, null=True)
    description = fields.CharField(max_length=500, null=True)
    cheque_no = fields.CharField(max_length=30, null=True)
    cheque_date = fields.DateField(null=True)
    total = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    created_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="vouchers_created", null=True, on_delete=fields.SET_NULL
    )
    created_by_name = fields.CharField(max_length=120, null=True)
    posted_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="vouchers_posted", null=True, on_delete=fields.SET_NULL
    )
    posted_by_name = fields.CharField(max_length=120, null=True)
    posted_at = fields.DatetimeField(null=True)
    cancelled_by_name = fields.CharField(max_length=120, null=True)
    cancelled_at = fields.DatetimeField(null=True)
    cancel_reason = fields.CharField(max_length=255, null=True)
    reversal_of_id = fields.CharField(max_length=36, null=True)
    reversed = fields.BooleanField(default=False)
    version = fields.IntField(default=1)
    # Sent up by a branch rather than made here.
    mirrored = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "acc_vouchers"
        indexes = (("book", "date", "status"),)


class VoucherLine(models.Model):
    id = fields.UUIDField(pk=True)
    voucher: fields.ForeignKeyRelation[Voucher] = fields.ForeignKeyField("models.Voucher", related_name="lines", on_delete=fields.CASCADE)
    line_no = fields.IntField(default=0)
    account: fields.ForeignKeyRelation[Account] = fields.ForeignKeyField("models.Account", related_name="voucher_lines", on_delete=fields.RESTRICT)
    debit = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    credit = fields.DecimalField(max_digits=16, decimal_places=2, default=0)
    description = fields.CharField(max_length=255, null=True)
    reference_no = fields.CharField(max_length=60, null=True)

    class Meta:
        table = "acc_voucher_lines"


class AccountsSettings(models.Model):
    """One row per book. Head office's is set here; a branch's is what the branch last reported."""

    book = fields.CharField(max_length=20, pk=True)
    fiscal_start_month = fields.IntField(default=7)
    books_start = fields.DateField(null=True)
    locked_until = fields.DateField(null=True)
    tender_accounts = fields.JSONField(default=dict)
    last_posting_at = fields.DatetimeField(null=True)
    last_posting_note = fields.CharField(max_length=255, null=True)
    posting_problems = fields.JSONField(default=list)
    # For a branch's book: when its books last arrived.
    last_received_at = fields.DatetimeField(null=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "acc_settings"
