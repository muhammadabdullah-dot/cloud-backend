"""What the signed-in person must do now, and what they should know."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.middlewares.auth import get_current_user
from app.models import User
from app.services import alerts_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


class TaskOut(BaseModel):
    key: str
    group: str
    title: str
    detail: str
    # None when the reader holds no screen that opens this step — the task still shows, without Open.
    link: str | None = None
    since: str
    overdue: bool


class NoticeOut(BaseModel):
    id: str
    at: str
    kind: str
    title: str
    body: str | None = None
    link: str | None = None
    tone: str
    read: bool


class AlertsOut(BaseModel):
    tasks: list[TaskOut]
    notices: list[NoticeOut]
    unread: int


class ReadRequest(BaseModel):
    # None marks everything this person can see as read.
    ids: list[str] | None = None


@router.get("", response_model=AlertsOut)
async def my_alerts(user: User = Depends(get_current_user)) -> AlertsOut:
    return AlertsOut(**await alerts_service.summary(user))


@router.post("/read")
async def mark_read(payload: ReadRequest, user: User = Depends(get_current_user)) -> dict:
    return {"marked": await alerts_service.mark_read(user, payload.ids)}
