"""Pakistan time, the only clock a person here reads.

The business runs in Pakistan: UTC+5, with no daylight saving. An instant kept in the database is just a moment and
stays as it is. Every calendar day, hour, month and year worked out from one (today, a report's day, a time shown on a
screen, a backup's file name) is Pakistan's, whatever zone the server machine's own clock is set to. Use these rather
than `date.today()`, `datetime.now()` or `.date()` on a UTC time.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

PKT = timezone(timedelta(hours=5))

# SQLite's modifier that turns a stored instant into Pakistan wall time: date(at, '+5 hours') is the Pakistan day,
# strftime('%H', at, '+5 hours') the Pakistan hour.
SQL_SHIFT = "'+5 hours'"


def now_pk() -> datetime:
    """This moment on the Pakistan clock (an aware datetime, so it compares with any other instant)."""
    return datetime.now(PKT)


def today_pk() -> date:
    return now_pk().date()


def pk_time(at: datetime) -> datetime:
    """An instant on the Pakistan clock. A time with no zone is taken as UTC, which is how the database keeps them."""
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(PKT)


def pk_day(at: datetime | date | None = None) -> date:
    """The Pakistan day an instant falls on (today when none is given). A plain date is already a day."""
    if at is None:
        return today_pk()
    if not isinstance(at, datetime):
        return at
    return pk_time(at).date()


def day_start(day: date) -> datetime:
    """The instant a Pakistan day begins (as UTC), for filtering stored times by day: at >= day_start(d) and
    at < day_start(d + 1 day)."""
    return datetime.combine(day, time.min, tzinfo=PKT).astimezone(timezone.utc)
