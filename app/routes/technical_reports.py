"""Admin › Technical Reports over HTTP: the errors in head office's log, and each branch's sync problems.
Read-only, for whoever holds the Branches tick (admin.sync-endpoints), the same tick that shows the tab. See
services/technical_reports_service.py."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.middlewares.auth import get_current_user
from app.models import User
from app.services import technical_reports_service as reports
from app.services.rbac_service import has_permission

router = APIRouter(prefix="/admin/technical", tags=["technical-reports"])


async def _reader(user: User = Depends(get_current_user)) -> User:
    if not await has_permission(user, "admin.sync-endpoints", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The technical reports need the right to see branches and their sync. Ask your System Admin for it.")
    return user


@router.get("/errors")
async def errors(
    q: str | None = Query(None, max_length=200), day: date | None = None, kind: str | None = None,
    limit: int = Query(reports.LIST_LIMIT, ge=1, le=reports.MAX_LIMIT), user: User = Depends(_reader),
) -> dict:
    """Warnings and errors from the log, newest first. `q` finds a reference (any spacing or case) or words; `day` is one day."""
    return await reports.errors(q=q, day=day, kind=kind, limit=limit)


@router.get("/errors/{key}")
async def error_detail(key: str, user: User = Depends(_reader)) -> dict:
    """One entry in full, with its traceback or the browser's detail."""
    entry = await reports.error_detail(key)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That entry is no longer in the log. Older entries are cleared as the log fills up.")
    return entry


@router.get("/sync")
async def sync_problems(user: User = Depends(_reader)) -> dict:
    """Per branch: when it last reported, events head office couldn't apply, and messages to it not yet applied."""
    return await reports.sync_problems()
