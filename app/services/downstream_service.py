"""The downstream queue: numbered messages a branch collects and confirms. See models/downstream.py."""
from datetime import datetime, timezone

from tortoise.exceptions import IntegrityError

from app.models import Branch, BranchMessage

PULL_LIMIT = 200


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
        else:
            message.apply_error = str(result.get("error") or "The branch couldn't apply this.")[:1000]
        await message.save(update_fields=["applied_at", "apply_error"])
        highest = max(highest, seq)
        recorded += 1
    if highest != branch.last_applied_seq:
        branch.last_applied_seq = highest
        await branch.save(update_fields=["last_applied_seq"])
    return recorded
