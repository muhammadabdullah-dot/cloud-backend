"""Errors the browser app runs into, written to this server's log under the reference the screen showed.

Open to anyone who can reach the server, signed in or not (the sign-in screen can break too), so it keeps a lid on
itself: each field is cut to a sensible length and one device can send at most 30 reports in 10 minutes.
"""
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from app.core import logs
from app.core.pk_time import pk_time
from app.middlewares.error_handler import describe_caller

router = APIRouter(tags=["client-errors"])

WINDOW_SECONDS = 600
PER_WINDOW = 30
_recent: dict[str, deque] = defaultdict(deque)


class ClientErrorIn(BaseModel):
    reference: str = Field(pattern=r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
    kind: Literal["screen", "uncaught"] = "screen"
    message: str = ""
    stack: str | None = None
    componentStack: str | None = None
    path: str | None = None
    at: str | None = None
    userAgent: str | None = None


def _cut(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit] + " …"


def _seen(value: str | None) -> str:
    """When the browser saw it, on the Pakistan clock when it is a time this can read (a browser sends UTC)."""
    try:
        return pk_time(datetime.fromisoformat((value or "").strip())).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _cut(value, 40)


@router.post("/client-errors", status_code=status.HTTP_204_NO_CONTENT)
async def report_client_error(payload: ClientErrorIn, request: Request) -> Response:
    source = request.headers.get("x-device-id") or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    recent = _recent[source]
    while recent and now - recent[0] > WINDOW_SECONDS:
        recent.popleft()
    if len(recent) >= PER_WINDOW:
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    recent.append(now)

    what = "screen error" if payload.kind == "screen" else "browser error"
    lines = [
        f"{what} {payload.reference} on {_cut(payload.path, 300) or 'an unknown screen'}{await describe_caller(request)}",
        f"  {_cut(payload.message, 1000) or '(no message)'}",
    ]
    if payload.at or payload.userAgent:
        lines.append(f"  seen {_seen(payload.at)} in {_cut(payload.userAgent, 300)}")
    if payload.stack:
        lines.append("  stack:\n" + _cut(payload.stack, 8000))
    if payload.componentStack:
        lines.append("  on screen:\n" + _cut(payload.componentStack, 4000))
    (logs.log.error if payload.kind == "screen" else logs.log.warning)("\n".join(lines))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
