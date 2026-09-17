"""The downstream queue: numbered messages a branch collects and confirms. See models/downstream.py."""
from datetime import datetime, timedelta, timezone

from tortoise.exceptions import IntegrityError

from app.models import Branch, BranchMessage

PULL_LIMIT = 200

# A message the branch couldn't apply goes again, as a new message at the back of its queue: a refusal
# is often only a matter of order (an account naming a role the branch gets later, a transfer for a
# branch it hasn't heard of yet), and the branch moves its cursor past a refused message, so nothing
# else would ever offer it again. The branch applies every kind safely more than once (whole state, by
# id and revision), so a repeat that turns out not to be needed changes nothing.
MAX_TRIES = 5
# Waited before each repeat, times the tries so far: 15 minutes, then 30, 45 and an hour.
RETRY_AFTER = timedelta(minutes=15)
# Only refusals this recent are repeated, so older history isn't sent all over again.
RETRY_WINDOW = timedelta(days=1)
# Carried inside a repeated message's payload: the seq of the message first refused, and which try this is.
RETRY_KEY = "_retry"


async def enqueue(branch_id: str, kind: str, payload: dict) -> BranchMessage:
    """Add a message to one branch's queue, numbered after the last."""
    for _ in range(5):
        last = await BranchMessage.filter(branch_id=branch_id).order_by("-seq").first()
        try:
            return await BranchMessage.create(branch_id=branch_id, seq=(last.seq if last else 0) + 1, kind=kind, payload=payload)
        except IntegrityError:
            # Two changes for the same branch numbered at the same instant; take the next number.
            continue
    raise RuntimeError(f"Couldn't number a message for branch {branch_id}")


async def pull(branch: Branch, after: int, limit: int = PULL_LIMIT) -> tuple[list[BranchMessage], int]:
    """Messages after the branch's cursor, oldest first, and the newest number in its queue."""
    limit = min(max(limit, 1), PULL_LIMIT)
    messages = await BranchMessage.filter(branch=branch, seq__gt=max(after, 0)).order_by("seq").limit(limit)
    now = datetime.now(timezone.utc)
    undelivered = [m.id for m in messages if m.delivered_at is None]
    if undelivered:
        await BranchMessage.filter(id__in=undelivered).update(delivered_at=now)
    branch.last_pulled_at = now
    await branch.save(update_fields=["last_pulled_at"])
    newest = await BranchMessage.filter(branch=branch).order_by("-seq").first()
    return messages, newest.seq if newest else 0


async def ack(branch: Branch, results: list[dict]) -> int:
    """Record what the branch did with each message. Returns how many were recorded."""
    now = datetime.now(timezone.utc)
    recorded = 0
    highest = branch.last_applied_seq
    for result in results:
        seq = int(result.get("seq") or 0)
        message = await BranchMessage.get_or_none(branch=branch, seq=seq)
        if not message:
            continue
        if result.get("ok"):
            message.applied_at = now
            message.apply_error = None
            origin = _origin(message.payload)
            if origin is not None:
                # A repeat that worked: the earlier refusals of the same change are settled too, so
                # they stop reading as refused.
                earlier = await BranchMessage.filter(
                    branch=branch, kind=message.kind, seq__gte=origin[0], seq__lt=seq, applied_at=None,
                )
                for old in earlier:
                    if old.seq == origin[0] or (_origin(old.payload) or (None,))[0] == origin[0]:
                        old.applied_at, old.apply_error = now, None
                        await old.save(update_fields=["applied_at", "apply_error"])
        else:
            message.apply_error = str(result.get("error") or "The branch couldn't apply this.")[:1000]
        await message.save(update_fields=["applied_at", "apply_error"])
        highest = max(highest, seq)
        recorded += 1
    if highest != branch.last_applied_seq:
        branch.last_applied_seq = highest
        await branch.save(update_fields=["last_applied_seq"])
    return recorded


def _origin(payload) -> tuple[int, int] | None:
    """(seq first refused, try number) for a repeated message; None for an original."""
    info = payload.get(RETRY_KEY) if isinstance(payload, dict) else None
    try:
        return (int(info["of"]), int(info["try"])) if isinstance(info, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


async def resend_refused() -> int:
    """Queue again each message a branch recently refused, spaced out and at most `MAX_TRIES` times in
    all. Run on head office's replay timer. Returns how many went back on a queue."""
    now = datetime.now(timezone.utc)
    resent = 0
    refused = await BranchMessage.filter(
        apply_error__isnull=False, applied_at=None, delivered_at__gte=now - RETRY_WINDOW,
    ).order_by("seq")
    for message in refused:
        if not isinstance(message.payload, dict):
            continue
        origin = _origin(message.payload) or (message.seq, 1)
        delivered = message.delivered_at if message.delivered_at.tzinfo else message.delivered_at.replace(tzinfo=timezone.utc)
        if origin[1] >= MAX_TRIES or now - delivered < RETRY_AFTER * origin[1]:
            continue
        later = await BranchMessage.filter(
            branch_id=message.branch_id, kind=message.kind, seq__gt=message.seq,
        ).values_list("seq", "payload")
        if any(_origin(p) == (origin[0], origin[1] + 1) for _, p in later):
            continue  # already queued again
        if message.kind == "role.template" and any(
            isinstance(p, dict) and p.get("roleId") == message.payload.get("roleId") and (_origin(p) or (s,))[0] > origin[0]
            for s, p in later
        ):
            # The one kind a branch replaces outright with no revision to compare: a newer standard for
            # the same role has gone since, and repeating this one would undo it.
            continue
        payload = {**message.payload, RETRY_KEY: {"of": origin[0], "try": origin[1] + 1}}
        await enqueue(str(message.branch_id), message.kind, payload)
        resent += 1
    return resent
