"""Registers process-wide exception handling. Grows here, not per-route, as real errors are added."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import logs


async def describe_caller(request: Request) -> str:
    """Who was asking and from which device, for the log. Never fails: the error being logged may be the database's."""
    parts = []
    try:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            from app.core.security import decode_access_token
            from app.models import User

            user_id = decode_access_token(auth.removeprefix("Bearer ").strip()).get("sub")
            user = await User.get_or_none(id=user_id) if user_id else None
            parts.append(f"by {user.name} <{user.email}>" if user else f"by user {user_id}")
    except Exception:  # noqa: BLE001 — an expired token or an unreachable database still gets the error logged
        parts.append("by an unidentified user")
    device = request.headers.get("x-device-id")
    if device:
        parts.append(f"device {device[:80]}")
    return (" " + " ".join(parts)) if parts else ""


def _single(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _the_error_itself(exc: BaseException) -> BaseException:
    """The middleware layers wrap a request's error in a group of one, and re-raise it "from" that group, so the
    traceback would show the same error twice. The log wants the error itself, once."""
    exc = _single(exc)
    if isinstance(exc.__cause__, BaseExceptionGroup) and _single(exc.__cause__) is exc:
        exc.__cause__ = None
        exc.__suppress_context__ = True
    return exc


def register_error_handlers(app: FastAPI) -> None:
    from app.services.import_service import ImportFormatError

    @app.exception_handler(ImportFormatError)
    async def import_format_handler(request: Request, exc: ImportFormatError) -> JSONResponse:
        # A file that can't be read is the person's to fix (wrong type, damaged), not a server fault.
        return JSONResponse(status_code=400, content={"detail": exc.message})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # The person gets a reference to quote; the log keeps what happened under the same reference.
        reference = logs.new_reference()
        exc = _the_error_itself(exc)
        where = request.url.path + (f"?{request.url.query[:300]}" if request.url.query else "")
        logs.log.error("error %s on %s %s%s", reference, request.method, where, await describe_caller(request), exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(
            status_code=500,
            content={"detail": f"Something went wrong on the server. Reference {reference}.", "errorId": reference},
            headers={"X-Error-Id": reference},
        )
