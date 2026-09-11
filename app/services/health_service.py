"""Services are where ORM/procedure calls happen — no repository layer in between."""
from datetime import datetime, timezone

from app.core.config import settings
from app.schemas.health import HealthResponse


async def check_health() -> HealthResponse:
    return HealthResponse(status="ok", service=settings.app_name, time=datetime.now(timezone.utc))
