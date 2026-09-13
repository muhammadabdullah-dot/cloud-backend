"""The Cloud half of branch registration: issuing a pairing key, spending it, and authenticating
every sync that follows.

The rule this module exists to enforce is that **a branch is claimed exactly once**. Everything
else here is in service of that. A claim checks the key, checks it has not expired, checks nobody
has claimed this branch already, and only then writes `verified_at`. A second attempt — a
re-install, a copied key, a branch pointed at the wrong Cloud — is refused with an explanation
rather than quietly re-pointing a live branch somewhere new.

Re-pairing is possible but it is an admin act with a name: revoke, then issue. That asymmetry is
the point. Claiming must be easy enough for whoever is standing in the shop; un-claiming must be
deliberate enough that it never happens by accident.
"""
from datetime import datetime, timezone

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app.core.pairing import (
    hash_sync_secret,
    is_pairing_expired,
    new_pairing_code,
    new_sync_secret,
    pairing_codes_match,
    pairing_expiry,
    verify_sync_secret,
)
from app.models import Branch, SyncInboxEvent, SyncRun
from app.models.branch import BRANCH_STATUSES  # noqa: F401  (kept beside the status checks below)


class RegistrationError(Exception):
    """A refusal the caller should turn into a 4xx with this exact wording. The branch installer
    reads these messages on a screen in a shop, so they say what to do next, not what went wrong
    internally."""

    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------------------------

async def issue_pairing(branch: Branch) -> str:
    """Mint (or re-mint) this branch's one-time key and return it in the clear — the only moment
    Cloud ever hands it out is the moment an admin asks for it.

    Refused once the branch is claimed. If that key still worked after verification, "verified"
    would mean nothing: anyone holding the original install sheet could re-point the branch."""
    if branch.verified_at is not None:
        raise RegistrationError(
            f"{branch.code} is already verified and paired. Revoke its pairing first if this "
            "branch really does need to be set up again.",
            status=409,
        )
    code = new_pairing_code()
    now = datetime.now(timezone.utc)
    branch.pairing_code = code
    branch.pairing_issued_at = now
    branch.pairing_expires_at = pairing_expiry(now)
    await branch.save(update_fields=["pairing_code", "pairing_issued_at", "pairing_expires_at"])
    return code


async def revoke_pairing(branch: Branch) -> Branch:
    """Un-claim a branch so it can be paired again — a re-install, a replaced server box, a branch
    that was set up against the wrong Cloud.

    This clears the credential and nothing else. The branch's history, its requisitions, its
    transfers, everything reported from it, all stay exactly where they are. Revoking is about who
    may talk to us, not about what they have already said."""
    branch.verified_at = None
    branch.sync_secret_hash = None
    branch.claimed_from = None
    branch.pairing_code = None
    branch.pairing_issued_at = None
    branch.pairing_expires_at = None
    await branch.save(
        update_fields=[
            "verified_at", "sync_secret_hash", "claimed_from",
            "pairing_code", "pairing_issued_at", "pairing_expires_at",
        ]
    )
    return branch


# ---------------------------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------------------------

async def claim(code: str, pairing_key: str, sync_url: str | None, claimed_from: str | None) -> tuple[Branch, str]:
    """Spend a pairing key. Returns the branch and its brand-new sync secret in the clear — the one
    and only time that secret exists outside a hash.

    The whole thing runs in one transaction. A claim that half-succeeds — key burned but no secret
    stored — would leave a branch permanently unable to pair and an admin with no idea why."""
    wanted = (code or "").strip().upper()
    if not wanted:
        raise RegistrationError("Branch code is required.", status=422)
    if not (pairing_key or "").strip():
        raise RegistrationError("Verification key is required.", status=422)

    async with in_transaction():
        branch = await Branch.get_or_none(code=wanted)
        # Deliberately the same wording for "no such branch" and "wrong key": the failure message
        # should not tell an outsider which branch codes exist on this Cloud.
        if branch is None or not pairing_codes_match(branch.pairing_code, pairing_key):
            raise RegistrationError(
                "That branch code and verification key don't match anything on the Cloud. "
                "Check both with main office.",
                status=401,
            )
        if branch.verified_at is not None:
            raise RegistrationError(
                f"{branch.code} has already been verified from another machine. Main office must "
                "revoke its pairing before it can be set up again.",
                status=409,
            )
        if is_pairing_expired(branch.pairing_expires_at):
            raise RegistrationError(
                "This verification key has expired. Ask main office to issue a new one.",
                status=410,
            )
        if branch.status != "active":
            raise RegistrationError(
                f"{branch.code} is suspended on the Cloud and can't be set up right now.",
                status=403,
            )

        secret = new_sync_secret()
        now = datetime.now(timezone.utc)
        branch.verified_at = now
        branch.last_seen_at = now
        branch.sync_secret_hash = hash_sync_secret(secret)
        branch.claimed_from = (claimed_from or None)
        # The key is spent the moment it works. Nothing replays it.
        branch.pairing_code = None
        branch.pairing_expires_at = None
        if sync_url:
            branch.sync_url = sync_url
        await branch.save()

    return branch, secret


# ---------------------------------------------------------------------------------------------
# Authenticating a paired branch
# ---------------------------------------------------------------------------------------------

async def authenticate(code: str | None, secret: str | None) -> Branch:
    """Who is this branch? Used by every sync call. Raises rather than returning None so no caller
    can forget to check."""
    wanted = (code or "").strip().upper()
    if not wanted or not secret:
        raise RegistrationError("Branch credentials are required.", status=401)
    branch = await Branch.get_or_none(code=wanted)
    if branch is None or branch.verified_at is None or not verify_sync_secret(secret, branch.sync_secret_hash):
        raise RegistrationError("Branch credentials are not recognised.", status=401)
    if branch.status != "active":
        raise RegistrationError(f"{branch.code} is suspended on the Cloud.", status=403)
    return branch


async def touch(branch: Branch) -> None:
    """We heard from this branch. This is the field Executive's freshness display reads."""
    branch.last_seen_at = datetime.now(timezone.utc)
    await branch.save(update_fields=["last_seen_at"])


# ---------------------------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------------------------

async def ingest(branch: Branch, events: list[dict]) -> tuple[SyncRun, list[str]]:
    """Store a pushed batch, skipping anything already here. Returns the run and the ids we can
    honestly say are now on the Cloud.

    Only acknowledged ids may be marked sent at the branch, so this list is built from what
    actually landed rather than from what was offered. An event we failed to store simply never
    appears in it, and the branch offers it again next tick — which is the behaviour that makes
    losing data impossible rather than merely unlikely.

    Idempotent by `(branch, event_id)`. A branch whose connection dies after we commit but before
    it hears "ok" will send the same batch again on its next tick; every one of those events
    matches a row that already exists and is counted as a duplicate. That is the normal, expected
    case on a bad link, not an error — which is why duplicates are reported as a number rather than
    raised as a failure.

    Events are stored, not applied. Projecting them into Cloud's business tables is the next piece
    of work; keeping the raw event means that projection can be re-run over history when it lands.
    """
    accepted = 0
    duplicates = 0
    acknowledged: list[str] = []
    rejected = 0

    # Per-event, not one big transaction: a single malformed event in a batch of five hundred
    # should cost us that one event, not the other four hundred and ninety nine.
    for raw in events:
        event_id = str(raw.get("id") or "").strip()
        if not event_id:
            rejected += 1
            continue
        try:
            await SyncInboxEvent.create(
                branch=branch,
                event_id=event_id,
                aggregate_type=str(raw.get("aggregateType") or "unknown")[:60],
                aggregate_id=str(raw.get("aggregateId") or "")[:60],
                payload=raw.get("payload") or {},
                origin_user_id=(str(raw["originUserId"])[:60] if raw.get("originUserId") else None),
                origin_device_id=(str(raw["originDeviceId"])[:80] if raw.get("originDeviceId") else None),
                occurred_at=raw.get("createdAt"),
            )
            accepted += 1
            acknowledged.append(event_id)
        except IntegrityError:
            # The unique_together did its job. This event is already stored — which, from the
            # branch's point of view, is a success: it is on the Cloud.
            duplicates += 1
            acknowledged.append(event_id)
        except Exception as exc:  # noqa: BLE001 — one bad event must not fail the batch
            rejected += 1
            note = f"{type(exc).__name__} on event {event_id}"
            print(f"  sync: rejected event from {branch.code}: {note}", flush=True)

    run = await SyncRun.create(
        branch=branch,
        event_count=len(events),
        accepted_count=accepted,
        duplicate_count=duplicates,
        status="ok" if not rejected else "partial",
        note=None if not rejected else f"{rejected} event(s) could not be stored and will be resent",
    )
    await touch(branch)
    return run, acknowledged
