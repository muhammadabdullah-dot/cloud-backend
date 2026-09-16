"""Every action at a branch, traced to the person who took it."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from tortoise.expressions import Q

from app.middlewares.auth import require_any_permission
from app.models import Branch, BranchActivity, User

router = APIRouter(prefix="/branches", tags=["activity"])
_read = require_any_permission(("admin.sync-endpoints", "R"), ("executive.branches", "R"))


class ActivityOut(BaseModel):
    id: str
    at: datetime
    userId: str | None = None
    userName: str | None = None
    userTitle: str | None = None
    action: str
    method: str
    route: str | None = None
    path: str
    params: dict | None = None
    status: int
    detail: dict | list | None = None
    deviceId: str | None = None


class ActivityListOut(BaseModel):
    items: list[ActivityOut]
    total: int


class ActivityPersonOut(BaseModel):
    userId: str
    name: str
    title: str | None = None
    actions: int


@router.get("/{branch_id}/activity", response_model=ActivityListOut)
async def branch_activity(
    branch_id: str,
    user: str | None = None,
    q: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    caller: User = Depends(_read),
) -> ActivityListOut:
    """Newest first. `user` is the person's id at that branch; `q` searches the action, the person and the path."""
    if not await Branch.exists(id=branch_id):
        raise HTTPException(404, "No such branch")
    qs = BranchActivity.filter(branch_id=branch_id)
    if user:
        qs = qs.filter(user_id=user)
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(user_name__icontains=q) | Q(path__icontains=q))
    if from_:
        qs = qs.filter(at__gte=from_)
    if to:
        qs = qs.filter(at__lte=to)
    total = await qs.count()
    rows = await qs.order_by("-at").offset(max(offset, 0)).limit(min(max(limit, 1), 500))
    return ActivityListOut(total=total, items=[
        ActivityOut(
            id=str(r.id), at=r.at, userId=r.user_id, userName=r.user_name, userTitle=r.user_title, action=r.action,
            method=r.method, route=r.route, path=r.path, params=r.params, status=r.status_code, detail=r.detail, deviceId=r.device_id,
        )
        for r in rows
    ])


@router.get("/{branch_id}/activity/people", response_model=list[ActivityPersonOut])
async def branch_activity_people(branch_id: str, caller: User = Depends(_read)) -> list[ActivityPersonOut]:
    """Everyone with something on the record at this branch, under the name and title they last used."""
    if not await Branch.exists(id=branch_id):
        raise HTTPException(404, "No such branch")
    rows = await (
        BranchActivity.filter(branch_id=branch_id, user_id__isnull=False)
        .distinct().values_list("user_id", "user_name", "user_title")
    )
    people: dict[str, ActivityPersonOut] = {}
    for user_id, name, title in rows:
        people.setdefault(user_id, ActivityPersonOut(userId=user_id, name=name or user_id, title=title, actions=0))
    for person in people.values():
        latest = await BranchActivity.filter(branch_id=branch_id, user_id=person.userId).order_by("-at").first()
        if latest:
            person.name, person.title = latest.user_name or person.name, latest.user_title
        person.actions = await BranchActivity.filter(branch_id=branch_id, user_id=person.userId).count()
    return sorted(people.values(), key=lambda p: p.name.lower())
