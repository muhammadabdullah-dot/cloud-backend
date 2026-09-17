"""The server's own log: what went wrong, kept in a file so it can be looked at afterwards.

Written to a `logs` folder beside the database (logs/server.log), 5 MB a file with the last 10 files kept, so it
never grows past about 55 MB. Every unexpected error gets a short reference such as 7KQ2-M9XD: the person who hit
it sees the reference on screen, and the log entry with the full detail carries the same reference. Errors a
browser runs into are sent here too (routes/client_errors.py) and logged under the reference that screen showed.

The console keeps printing one line per problem, as before; the file is what outlives the window.
"""
import logging
import logging.handlers
import secrets
import time
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.core.pk_time import PKT

LOGGER_NAME = "dmarina"
MAX_BYTES = 5 * 1024 * 1024
KEEP = 10
# No 0/O or 1/I: a reference is read out over the phone.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

log = logging.getLogger(LOGGER_NAME)
_file_handler: logging.Handler | None = None


def log_dir() -> Path:
    if settings.log_dir:
        return Path(settings.log_dir).expanduser().resolve()
    if settings.db_url.startswith("sqlite://"):
        return Path(settings.db_url.removeprefix("sqlite://")).expanduser().resolve().parent / "logs"
    return Path("logs").resolve()


def log_file() -> Path:
    return log_dir() / "server.log"


def new_reference() -> str:
    raw = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


class _OneCopyOfEachError(logging.Filter):
    """The server prints its own copy of a request's error after we have logged it with a reference; the file keeps ours."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Exception in ASGI application")


def _pakistan_clock(seconds: float | None) -> time.struct_time:
    """Log times on the Pakistan clock, whatever zone the server machine's own clock is set to."""
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, PKT).timetuple()


class _ConsoleFormat(logging.Formatter):
    """One line on the console, the way the server always printed; the traceback goes to the file only."""

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage().split("\n", 1)[0]
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            text = f"{text} ({type(exc).__name__}: {exc})"
        return f"  {text}"


def setup() -> Path:
    """Safe to call more than once: the server's own logging set-up clears handlers it manages, so this puts ours back."""
    global _file_handler
    if _file_handler is None:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.handlers.RotatingFileHandler(log_file(), maxBytes=MAX_BYTES, backupCount=KEEP, encoding="utf-8", delay=True)
        file_format = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        file_format.converter = _pakistan_clock
        _file_handler.setFormatter(file_format)
        _file_handler.addFilter(_OneCopyOfEachError())
        console = logging.StreamHandler()
        console.setFormatter(_ConsoleFormat())
        log.addHandler(_file_handler)
        log.addHandler(console)
        log.setLevel(logging.INFO)
        log.propagate = False
    server_log = logging.getLogger("uvicorn.error")
    if _file_handler not in server_log.handlers:
        server_log.addHandler(_file_handler)
    return log_file()
