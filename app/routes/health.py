from fastapi import APIRouter

from app.controllers import health_controller
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await health_controller.get_health()


@router.get("/meta/date-filters")
async def date_filters() -> dict:
    """Which date ranges in this app honour a time, and which are by day, so the app only offers a time where it
    changes the answer.

    A time control on a report built from voucher dates would look like it worked and do nothing, which is worse
    than not offering one. `core/when.GRAIN` is the single place that says which is which, and it carries the
    reason for each so this answer explains itself rather than asserting.

    It needs no permission: it describes the shape of the filters, not anybody's figures.
    """
    from app.core.when import GRAIN

    return {
        "filters": [
            {"path": path, "grain": grain, "honoursTime": grain in ("instant", "mixed"),
             # Only a "mixed" filter has figures that cannot follow a time; the app can offer the picker and warn
             # on those alone rather than withholding it from the whole screen.
             "partly": grain == "mixed", "why": why}
            for path, (grain, why) in sorted(GRAIN.items())
        ],
        "note": ("A filter with honoursTime false is built from a column that holds a date and nothing finer. "
                 "Sending a time to one is accepted and ignored, and the report says so. A filter with `partly` "
                 "honours a time for most of its figures and names the ones it could not, on the answer itself."),
    }
