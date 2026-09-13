"""Branch master — registration and upkeep. The Cloud owns this table: branches are created here,
not self-registered, until the verification handshake lands.
"""
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from app.models import Branch
from app.models.branch import BRANCH_STATUSES
from app.schemas.branches import BranchCreate, BranchUpdate


class BranchError(Exception):
    def __init__(self, message: str):
        self.message = message


_FIELD_MAP = {"syncUrl": "sync_url"}


def _to_model_fields(data: dict) -> dict:
    return {_FIELD_MAP.get(k, k): v for k, v in data.items()}


async def list_all(q: str | None, limit: int, offset: int) -> tuple[list[Branch], int]:
    qs = Branch.all()
    term = (q or "").strip()
    if term:
        qs = qs.filter(Q(name__icontains=term) | Q(code__icontains=term) | Q(city__icontains=term))
    total = await qs.count()
    items = await qs.order_by("name").offset(offset).limit(limit)
    return items, total


async def get(branch_id: str) -> Branch | None:
    return await Branch.get_or_none(id=branch_id)


async def create(data: BranchCreate) -> Branch:
    if await Branch.filter(code=data.code).exists():
        raise BranchError(f"A branch with code {data.code} already exists")
    try:
        return await Branch.create(**_to_model_fields(data.model_dump()))
    except IntegrityError as exc:  # unique race between the check above and the insert
        raise BranchError(f"A branch with code {data.code} already exists") from exc


async def update(branch_id: str, data: BranchUpdate) -> Branch | None:
    branch = await Branch.get_or_none(id=branch_id)
    if not branch:
        return None
    fields = _to_model_fields(data.model_dump(exclude_unset=True))
    status = fields.get("status")
    if status is not None and status not in BRANCH_STATUSES:
        raise BranchError(f"Status must be one of: {', '.join(BRANCH_STATUSES)}")
    for key, value in fields.items():
        setattr(branch, key, value)
    await branch.save()
    return branch


# Reverse relations that make a branch un-deletable. Resolved by name rather than imported so this
# guard is already correct for the warehouse tables as they land, instead of being a TODO that
# quietly lets the first orphaned requisition through.
_REFERENCING_RELATIONS = ("requisitions", "transfers", "snapshots", "inbox_events", "sync_runs")


async def delete(branch_id: str) -> bool:
    """Hard delete, and only while the branch has no history against it. Once requisitions or
    transfers point here, removing the row would orphan them — suspend the branch instead, which
    is what `status` is for. The caller turns this refusal into a 409."""
    branch = await Branch.get_or_none(id=branch_id)
    if not branch:
        return False
    if branch.verified_at is not None:
        # A live branch server is holding a credential that points at this row. Deleting it would
        # leave that branch running, syncing into a 401, and unable to re-pair — with nothing on
        # screen explaining why. Revoking first is one click and makes the consequence visible.
        raise BranchError(
            f"{branch.code} is verified and paired with a branch server. Revoke its pairing "
            "first if you really mean to remove it."
        )
    for relation in _REFERENCING_RELATIONS:
        manager = getattr(branch, relation, None)
        if manager is not None and await manager.all().exists():
            raise BranchError(
                "This branch has history recorded against it and can't be removed. "
                "Set its status to Suspended instead."
            )
    await branch.delete()
    return True
