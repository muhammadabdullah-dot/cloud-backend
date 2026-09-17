"""The Branch master — the entity frontend-baseline.md §4 names as the single biggest structural
gap on the Cloud side.

Today `Requisition.branchName` and `Transfer.toBranchName` are free text on Cloud, and the Branch
App knows its own identity only as a label. Every one of those becomes a foreign key to this row.

One table, not the two contracts.md sketched (§3.3 `BranchEndpoint` + §3.4 `Branch`). The sketch
split them because `BranchEndpoint` was reverse-engineered from `cloud-app/store/endpoints.ts`,
which already carries branchName + branchCode + url — it is a branch record with a URL attached,
not a separate concept. Splitting them would mean registering a branch in one place and its
endpoint in another with nothing keeping the two in agreement, which is exactly the drift that
made `branchName` free text in the first place. The registration/verification handshake lands on
this same row later (a shared secret and a verified flag) rather than as a second table.
"""
from tortoise import fields, models

# 'active' is the only status cloud-app models today; 'suspended' is the one real operational need
# beyond it — stop syncing/reporting a branch without deleting its history.
BRANCH_STATUSES = ("active", "suspended")


class Branch(models.Model):
    """A branch, and — once it has been claimed — the credential it syncs with.

    Pairing works in two stages on purpose. Creating the branch mints a short, human-readable
    **pairing key**: the "unique data provided from main office" that someone reads down a phone
    line or writes on the install sheet. It is one-shot. The branch server spends it exactly once,
    and in exchange gets a long **sync secret** that it keeps forever. That way the thing a human
    handles is short enough to dictate and worthless the moment it has been used, while the thing
    that actually authenticates every sync is long, random and never spoken aloud.

    The sync secret is stored here as a bcrypt hash, never in the clear — Cloud can check a secret
    a branch presents but cannot reproduce one, so a copy of this database is not a set of working
    branch credentials.
    """

    id = fields.UUIDField(pk=True)
    # The short human code staff actually say out loud — "HO", "FC". Unique, and the natural key
    # a branch server will identify itself by when registration lands.
    code = fields.CharField(max_length=20, unique=True)
    name = fields.CharField(max_length=140)
    address = fields.CharField(max_length=255, null=True)
    city = fields.CharField(max_length=120, null=True)
    phone = fields.CharField(max_length=40, null=True)
    timezone = fields.CharField(max_length=60, default="Asia/Karachi")
    # Where this branch's server can be reached. Nothing calls it yet — the push-ingest mechanism
    # that will is the "later" half of this work — but it is captured at registration time.
    sync_url = fields.CharField(max_length=255, null=True)
    status = fields.CharField(max_length=20, default="active")
    # Set by the sync ingest endpoint once it exists; null means "never heard from". Executive's
    # freshness display reads this rather than a seeded timestamp.
    last_seen_at = fields.DatetimeField(null=True)
    # Downstream: when the branch last collected its messages, and the last one it said it applied.
    last_pulled_at = fields.DatetimeField(null=True)
    last_applied_seq = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    # --- Pairing: the one-shot credential main office hands to the branch -------------------
    # Kept readable until it is spent, because a real handover is someone reading it out. It is
    # cleared the instant it is used, so a spent key in a screenshot or a chat log is inert.
    pairing_code = fields.CharField(max_length=32, null=True)
    pairing_issued_at = fields.DatetimeField(null=True)
    pairing_expires_at = fields.DatetimeField(null=True)

    # --- Claim: set once, when a branch server successfully spends its pairing key ----------
    # `verified_at` is the flag the whole handshake turns on. Non-null means this branch is
    # claimed, and a second claim attempt is refused rather than silently re-pointing the branch
    # at someone else's Cloud. Re-pairing is a deliberate admin act (revoke, then issue again).
    verified_at = fields.DatetimeField(null=True)
    sync_secret_hash = fields.CharField(max_length=200, null=True)
    # What claimed it — recorded so an unexpected re-pair is visible rather than invisible.
    claimed_from = fields.CharField(max_length=120, null=True)

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    class Meta:
        table = "branches"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.code}: {self.name}"
