"""Admin › Technical Reports over HTTP: the errors in head office's log, each branch's sync problems, and what
has gone wrong inside the branches themselves.
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


@router.get("/branch-errors")
async def branch_errors(
    days: int = Query(reports.ERROR_DAYS, ge=1, le=reports.MAX_ERROR_DAYS), user: User = Depends(_reader),
) -> dict:
    """What has broken inside each branch, grouped by what broke, worst first: its own failed requests, the
    screens its staff saw break, and the work it does on a timer that nobody was watching. Each line carries the
    reference the person there was shown, which is the same reference in that branch's own log file.

    This is the only sight head office has of a branch's errors: the branch's log file is on a machine in a shop,
    and a branch can be unusable all morning while its sync -- the one thing head office watches -- stays green."""
    return await reports.branch_errors(days=days)
