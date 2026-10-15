"""Analytical dashboards: the list a person may open, and one page of one, filters and all."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import dashboards as registry
from app.controllers import dashboards_controller as dc
from app.middlewares.auth import get_current_user
from app.models import User, UserPermission
from app.services.dashboards_service import DIMENSIONS, Scope

router = APIRouter(tags=["dashboards"])


async def _granted(user: User) -> set[str]:
    return set(await UserPermission.filter(user_id=user.id, can_read=True).values_list("resource", flat=True))


@router.get("/dashboards")
async def list_dashboards(user: User = Depends(get_current_user)) -> dict:
    """Only the ones this person's own grants reach.

    No permission of its own: a dashboard is gated by the resource of the thing it draws, so the answer is empty
    rather than forbidden for somebody who may open none of them. An empty list is a true answer to "what may I
    see", and a 403 would be a worse one.
    """
    return {"dashboards": dc.visible(await _granted(user))}


def _window(start: datetime | None, end: datetime | None) -> tuple[date, date]:
    """The days a request covers, defaulting to the last thirty."""
    today = date.today()
    first = (start.date() if start else today - timedelta(days=29))
    last = (end.date() if end else today)
    if first > last:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The first day comes after the last day.")
    return first, last


# DECLARED BEFORE THE PAGE ROUTE BELOW, and it has to be: `/dashboards/{dashboard}/{page}` matches this path too,
# and FastAPI takes the first route that matches. `dashboards.registry()` refuses to load a module with a page
# called "preview" for the same reason, so the two can never collide the other way either.
@router.get("/dashboards/{dashboard}/preview")
async def dashboard_preview(
    dashboard: str,
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    user: User = Depends(get_current_user),
) -> dict:
    """What this dashboard currently says, small enough for a card on the board.

    One request per card rather than one for the board: the cards then appear as each answers instead of all of
    them waiting on the slowest, and a dashboard this person may not open is never fetched at all.
    """
    module = dc.find(dashboard)
    if not module:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such dashboard.")
    if registry.resource_of(dashboard) not in await _granted(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You have not been given this dashboard.")
    first, last = _window(start, end)
    return await dc.preview(dashboard, Scope(start=first, end=last))


@router.get("/dashboards/{dashboard}/{page}")
async def dashboard_page(
    dashboard: str,
    page: str,
    # A bound may carry a time, exactly as everywhere else in this application: a bare date still parses as
    # midnight, so a screen sending dates is unaffected. See core/when.py.
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
    # Whatever was clicked. Every one of these is a column named in `dashboards_service.DIMENSIONS`, so a new
    # clickable chart needs no change here at all.
    branch: str | None = None,
    department: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    sku: str | None = None,
    # One band of a classed dashboard: A, B or C on ABC Analysis, X, Y or Z on XYZ. Not a dimension: a class is
    # not a column anywhere, it is cut from the window each time, so it travels beside `picked` rather than in it.
    band: str | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    module = dc.find(dashboard)
    if not module:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such dashboard.")
    # The dashboard's OWN resource, not the resource of the screen it draws. One tick each, administered on
    # the person like everything else; see `app/core/resources.py`.
    if registry.resource_of(dashboard) not in await _granted(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You have not been given this dashboard.")

    first, last = _window(start, end)

    picked = {name: value for name, value in (
        ("branch", branch), ("department", department), ("category", category),
        ("brand", brand), ("sku", sku)) if value}
    unknown = set(picked) - set(DIMENSIONS)
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot narrow on {', '.join(sorted(unknown))}.")

    # Midnight means no time at all, the rule core/when.py applies everywhere: midnight and nothing-at-all are the
    # same value on the wire, so reading one as a time would sweep a day in or out for anyone asking to stop at it.
    midnight = datetime.min.time()
    scope = Scope(
        start=first, end=last,
        at_start=start if start and start.time() != midnight else None,
        at_end=end if end and end.time() != midnight else None,
        picked=picked,
        band=(band.strip().upper()[:1] or None) if band else None,
    )
    try:
        return await dc.page(dashboard, page, scope, user)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such page on this dashboard.") from None
