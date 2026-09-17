"""Technical reports for head office's Admin desk: the problems this server wrote in its log, and the sync problems per branch.

Errors come from logs/server.log and the rotated files beside it (server.log.1 is the next oldest, and so on; see
core/logs.py). An entry starts with a line "2026-09-17 14:05:09 ERROR   message"; a traceback or a browser's detail
follows on lines of its own. Only warnings and errors are listed. The files are read from their newest end with a
cap on the bytes read, so the full 55 MB the log may keep never lands in memory for one look, and a line the server
is still writing, or one cut in half by the cap, is simply left out.

Sync problems are what the sync tables already hold, nothing worked out beyond them:
- events a branch sent that head office has but couldn't apply (`SyncInboxEvent` 'failed', with the reason), or of a
  kind head office applies that are still 'stored' (waiting);
- pushes where some events couldn't be stored at all (`SyncRun` not 'ok'; the branch sends those again);
- a full picture of the branch that was started and never finished (`BranchSnapshotRun` left 'building');
- messages head office queued for the branch (`BranchMessage`) not yet confirmed as applied, or refused by the branch.
Events of kinds head office only keeps on record (sales, till sessions and so on) stay 'stored' by design and are
not problems.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tortoise.expressions import Q

from app.core import logs
from app.core.pk_time import PKT
from app.models import Branch, BranchMessage, BranchSnapshotRun, SyncInboxEvent, SyncRun

# ---------------------------------------------------------------------------------------------
# Errors from the log
# ---------------------------------------------------------------------------------------------

LIST_LIMIT = 300
MAX_LIMIT = 500
# A plain look reads the newest 16 MB; a search, a day or one entry's detail may read everything the log keeps.
LOOK_BYTES = 16 * 1024 * 1024
SEARCH_BYTES = (logs.KEEP + 1) * logs.MAX_BYTES + 1024 * 1024
DETAIL_CHARS = 60_000

_HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)$")
_REFERENCE = re.compile(r"\b([2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4})\b")
# error_handler.py: "error 7KQ2-M9XD on GET /accounts/vouchers?x=1 by Name <email> device abc"
_SERVER = re.compile(r"^error ([2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}) on ([A-Z]+) (\S+)(.*)$")
# client_errors.py: "screen error 7KQ2-M9XD on /admin by Name <email> device abc"
_CLIENT = re.compile(r"^(screen|browser) error ([2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}) on (an unknown screen|\S+)(.*)$")
_BY = re.compile(r"\bby (.+?)(?: device \S+)?$")
_AREAS = {"sync": "Branch sync", "backup": "Backups", "accounts": "Accounts", "put-away": "Put-away", "restore": "Backups"}
_WANTED_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
KINDS = ("server", "screen", "browser", "sync", "other")


@dataclass
class _Entry:
    at: str
    level: str
    header: str
    more: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.at} {self.level} {self.header}".encode("utf-8", "replace")).hexdigest()[:16]

    def text(self) -> str:
        return "\n".join([f"{self.at.replace('T', ' ')} {self.level} {self.header}", *self.more])


def log_files() -> list[Path]:
    """server.log first, then server.log.1, .2 … : newest to oldest."""
    main = logs.log_file()
    return [main] + [main.with_name(f"{main.name}.{n}") for n in range(1, logs.KEEP + 1)]


def _tail_lines(path: Path, budget: int) -> tuple[list[str], int]:
    """The last `budget` bytes of a file as lines, and how many bytes were read. A first line cut by the cap is dropped."""
    try:
        size = path.stat().st_size
        start = max(0, size - budget)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(size - start)
    except OSError:
        # Not there, or rotated away between looking and reading: nothing to read from it.
        return [], 0
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines, len(data)


def _entries(lines: list[str]) -> list[_Entry]:
    """Oldest first, as written. Lines before the first dated one belong to an entry the cap cut off, so are left out."""
    found: list[_Entry] = []
    current: _Entry | None = None
    for line in lines:
        m = _HEADER.match(line)
        if m:
            current = _Entry(at=f"{m.group(1)}T{m.group(2)}", level=m.group(3), header=m.group(4).rstrip())
            found.append(current)
        elif current is not None:
            current.more.append(line.rstrip("\r"))
    return found


def _exception_line(more: list[str]) -> str | None:
    """The last line of a traceback, which is the error itself ("ValueError: ..."), if there is a traceback."""
    if not any(line.startswith("Traceback (most recent call last)") or line.lstrip("+| ").startswith("Traceback") for line in more):
        return None
    for line in reversed(more):
        text = line.lstrip("+| ").rstrip()
        if not text or line.startswith((" ", "\t")) or set(text) <= set("^~-=+|"):
            continue
        if text.startswith(("Traceback", "The above exception", "During handling")):
            continue
        return text[:500]
    return None


def _person(rest: str) -> str | None:
    m = _BY.search(rest.strip())
    if not m:
        return None
    who = m.group(1).strip()
    if who == "an unidentified user":
        return None
    return who.split(" <", 1)[0] or who


def _describe(entry: _Entry) -> dict:
    header = entry.header
    kind, reference, where, person, method = "other", None, "Server", None, None
    summary = header
    m = _SERVER.match(header)
    c = _CLIENT.match(header) if not m else None
    if m:
        kind, reference, method, where, person = "server", m.group(1), m.group(2), m.group(3), _person(m.group(4))
        summary = _exception_line(entry.more) or "No further detail was written."
    elif c:
        kind, reference, where, person = c.group(1), c.group(2), c.group(3), _person(c.group(4))
        if where == "an unknown screen":
            where = "Unknown screen"
        first = next((line.strip() for line in entry.more if line.strip()), "")
        summary = first or "No message was sent."
    else:
        prefix = re.match(r"^([a-z][a-z-]*)(?: \[[^\]]*\])?:\s*", header)
        if prefix:
            where = _AREAS.get(prefix.group(1), "Server")
            if prefix.group(1) == "sync":
                kind = "sync"
            if prefix.group(1) in _AREAS:
                # "Branch sync" is already in Where; the line reads better without "sync:" in front.
                summary = header[prefix.end():] or header
        ref = _REFERENCE.search(header)
        reference = ref.group(1) if ref else None
        error = _exception_line(entry.more)
        if error:
            summary = f"{summary} ({error})"
    return {
        "key": entry.key, "at": entry.at, "level": entry.level.lower(), "kind": kind, "reference": reference,
        "where": where[:300], "method": method, "person": person, "summary": summary[:600], "lines": 1 + len(entry.more),
    }


def _reference_query(q: str) -> str | None:
    """"7kq2m9xd", "7KQ2 M9XD" or "7KQ2-M9XD" all mean reference 7KQ2-M9XD."""
    raw = re.sub(r"[\s-]", "", q).upper()
    if re.fullmatch(r"[2-9A-HJ-NP-Z]{8}", raw):
        return f"{raw[:4]}-{raw[4:]}"
    return None


def _scan(*, q: str | None, day: date | None, kind: str | None, limit: int, key: str | None = None) -> dict:
    """Newest first: files newest to oldest, entries in each newest to oldest, until `limit` matches or the byte cap."""
    needle = (q or "").strip()
    reference = _reference_query(needle) if needle else None
    lowered = needle.lower()
    day_text = day.isoformat() if day else None
    budget = SEARCH_BYTES if (needle or day or key) else LOOK_BYTES
    items: list[dict] = []
    oldest_seen: str | None = None
    cut = False
    for path in log_files():
        if budget <= 0:
            cut = cut or path.exists()
            break
        if day is not None:
            try:
                # A file last written before the day holds nothing from it, and every older file is older still.
                if datetime.fromtimestamp(path.stat().st_mtime, PKT).date() < day:
                    break
            except OSError:
                continue
        lines, read = _tail_lines(path, budget)
        budget -= read
        try:
            if read and read < path.stat().st_size:
                cut = True
        except OSError:
            pass
        for entry in reversed(_entries(lines)):
            oldest_seen = entry.at
            if entry.level not in _WANTED_LEVELS:
                continue
            if key is not None:
                if entry.key == key:
                    return {"entry": {**_describe(entry), "detail": entry.text()[:DETAIL_CHARS]}}
                continue
            if day_text and not entry.at.startswith(day_text):
                if entry.at[:10] < day_text:
                    return {"items": items, "more": False, "oldestSeen": oldest_seen, "cut": False}
                continue
            if needle:
                # A reference typed any way round, or the words themselves (an 8-letter word can look like a reference).
                by_reference = reference is not None and (reference in entry.header or any(reference in line for line in entry.more[:3]))
                if not by_reference and lowered not in entry.header.lower() and not any(lowered in line.lower() for line in entry.more):
                    continue
            described = _describe(entry)
            if kind and described["kind"] != kind:
                continue
            if len(items) >= limit:
                return {"items": items, "more": True, "oldestSeen": oldest_seen, "cut": cut}
            items.append(described)
    if key is not None:
        return {"entry": None}
    return {"items": items, "more": False, "oldestSeen": oldest_seen, "cut": cut}


async def errors(*, q: str | None = None, day: date | None = None, kind: str | None = None, limit: int = LIST_LIMIT) -> dict:
    limit = min(max(limit, 1), MAX_LIMIT)
    kind = kind if kind in KINDS else None
    # Reading and parsing up to 55 MB is file work, kept off the server's request loop.
    result = await asyncio.to_thread(_scan, q=q, day=day, kind=kind, limit=limit)
    folder = logs.log_dir()
    return {**result, "folder": str(folder), "files": sum(1 for p in log_files() if p.exists())}


async def error_detail(key: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{16}", key or ""):
        return None
    result = await asyncio.to_thread(_scan, q=None, day=None, kind=None, limit=1, key=key)
    return result.get("entry")


# ---------------------------------------------------------------------------------------------
# Sync problems per branch
# ---------------------------------------------------------------------------------------------

ROWS_PER_LIST = 100
# The same line the alerts draw: a branch quiet for two hours has missed its full picture too.
QUIET_AFTER = timedelta(hours=2)
# A healthy branch collects and confirms within a few minutes; older than this is worth a look.
SLOW_MESSAGE_AFTER = timedelta(minutes=15)
# Full pictures arrive in pieces over a few minutes; one still open after an hour was abandoned.
PICTURE_STUCK_AFTER = timedelta(hours=1)

# Event kinds head office applies (services/projector.py PROJECTED). Every other kind is kept on record only.
APPLIED_KINDS = (
    "Staff", "Transfer", "Member", "LoyaltyEntry", "LoyaltySettings", "Activity", "AccChart", "AccVoucher", "AccSettings",
    "Supplier", "SupplierList",
)
EVENT_LABELS = {
    "Staff": "Staff account", "Transfer": "Transfer", "Member": "Loyalty member", "LoyaltyEntry": "Loyalty points",
    "LoyaltySettings": "Loyalty settings", "Activity": "Activity record", "AccChart": "Chart of accounts",
    "AccVoucher": "Accounts voucher", "AccSettings": "Accounts settings", "Supplier": "Supplier", "SupplierList": "Supplier list request",
}
MESSAGE_LABELS = {
    "transfer.inbound": "Transfer coming in", "transfer.outbound": "Transfer or request going out", "staff.upsert": "Staff account change",
    "staff.remove": "Staff account removed", "role.template": "Role standard access", "member.upsert": "Loyalty member",
    "loyalty.settings": "Loyalty settings", "supplier.upsert": "Supplier change", "supplier.list": "Supplier list",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _event_about(kind: str, payload: dict | None) -> str | None:
    """A name or number a person recognises, where the event carries one."""
    p = payload if isinstance(payload, dict) else {}
    try:
        if kind == "Staff":
            user = p.get("user") or {}
            return user.get("name") or user.get("email")
        if kind == "Member":
            member = p.get("member") or {}
            return " ".join(x for x in [member.get("code"), member.get("name")] if x) or None
        if kind == "LoyaltyEntry":
            entry = p.get("entry") or {}
            return " ".join(x for x in [entry.get("memberCode"), entry.get("invoiceNumber")] if x) or None
        if kind == "AccVoucher":
            return (p.get("voucher") or {}).get("number")
        if kind == "AccChart":
            inner = p.get(p.get("kind") or "") or {}
            return " ".join(str(x) for x in [inner.get("code"), inner.get("name")] if x) or None
        if kind == "Transfer":
            return (p.get("transfer") or {}).get("number") or (f"{p['event']}" if p.get("event") else None)
        if kind == "Supplier":
            return (p.get("supplier") or {}).get("name") or p.get("name")
        if kind == "Activity":
            return (p.get("activity") or {}).get("action")
    except (AttributeError, TypeError):
        return None
    return None


def _message_about(kind: str, payload: dict | None) -> str | None:
    p = payload if isinstance(payload, dict) else {}
    try:
        if kind.startswith("transfer."):
            t = p.get("transfer") or {}
            return t.get("number") or ((t.get("requisition") or {}).get("number"))
        if kind == "staff.upsert":
            user = p.get("user") or {}
            return user.get("name") or user.get("email")
        if kind == "member.upsert":
            member = p.get("member") or {}
            return " ".join(x for x in [member.get("code"), member.get("name")] if x) or None
        if kind == "supplier.upsert":
            return p.get("name") or (p.get("supplier") or {}).get("name")
        if kind == "supplier.list":
            return f"{len(p.get('suppliers') or [])} suppliers"
    except (AttributeError, TypeError):
        return None
    return None


async def _branch_problems(branch: Branch, now: datetime) -> dict:
    last_run = await SyncRun.filter(branch_id=branch.id).order_by("-received_at").first()
    short_runs = await SyncRun.filter(branch_id=branch.id).exclude(status="ok").order_by("-received_at").limit(20)

    stuck_q = SyncInboxEvent.filter(branch_id=branch.id).filter(Q(status="failed") | Q(status="stored", aggregate_type__in=APPLIED_KINDS))
    stuck_total = await stuck_q.count()
    stuck = await stuck_q.order_by("-received_at").limit(ROWS_PER_LIST)
    events = [{
        "id": str(e.id), "state": "refused" if e.status == "failed" else "waiting", "kind": e.aggregate_type,
        "label": EVENT_LABELS.get(e.aggregate_type, e.aggregate_type), "about": _event_about(e.aggregate_type, e.payload),
        "happenedAt": _iso(e.occurred_at), "receivedAt": _iso(e.received_at), "reason": e.apply_error,
    } for e in stuck]

    picture = await BranchSnapshotRun.filter(branch_id=branch.id, status="complete").order_by("-completed_at").first()
    building = await BranchSnapshotRun.filter(branch_id=branch.id, status="building").order_by("-started_at").first()
    picture_stuck = None
    if building and now - _aware(building.started_at) > PICTURE_STUCK_AFTER and (
        picture is None or _aware(building.started_at) > _aware(picture.completed_at or picture.started_at)
    ):
        picture_stuck = {"startedAt": _iso(building.started_at), "rows": building.stock_rows}

    open_q = BranchMessage.filter(branch_id=branch.id).filter(Q(applied_at=None) | Q(apply_error__isnull=False))
    open_total = await open_q.count()
    open_messages = await open_q.order_by("seq").limit(ROWS_PER_LIST)
    messages = []
    for m in open_messages:
        state = "refused" if m.apply_error else ("collected" if m.delivered_at else "waiting")
        messages.append({
            "seq": m.seq, "state": state, "kind": m.kind, "label": MESSAGE_LABELS.get(m.kind, m.kind),
            "about": _message_about(m.kind, m.payload), "queuedAt": _iso(m.created_at), "collectedAt": _iso(m.delivered_at),
            "error": m.apply_error, "slow": now - _aware(m.created_at) > SLOW_MESSAGE_AFTER,
        })
    newest = await BranchMessage.filter(branch_id=branch.id).order_by("-seq").first()

    last_seen = _aware(branch.last_seen_at)
    refused = sum(1 for e in events if e.get("state") == "refused") + sum(1 for m in messages if m["state"] == "refused")
    if not branch.verified_at and not last_seen:
        state = "not-connected"
    elif refused or picture_stuck or short_runs:
        state = "problem"
    elif (
        (last_seen is None or now - last_seen > QUIET_AFTER)
        or any(e["state"] == "waiting" for e in events) or any(m["slow"] for m in messages)
    ):
        state = "watch"
    else:
        state = "clear"

    return {
        "id": str(branch.id), "code": branch.code, "name": branch.name, "active": branch.status == "active",
        "connected": branch.verified_at is not None, "state": state,
        "lastReportAt": _iso(branch.last_seen_at),
        "lastPush": {
            "at": _iso(last_run.received_at), "events": last_run.event_count, "accepted": last_run.accepted_count,
            "alreadyHere": last_run.duplicate_count,
        } if last_run else None,
        "lastPictureAt": _iso(picture.completed_at) if picture else None,
        "pictureStuck": picture_stuck,
        "lastCollectedAt": _iso(branch.last_pulled_at),
        "lastConfirmedMessage": branch.last_applied_seq, "newestMessage": newest.seq if newest else 0,
        "shortPushes": [{"at": _iso(r.received_at), "events": r.event_count, "accepted": r.accepted_count, "note": r.note} for r in short_runs],
        "events": events, "eventsTotal": stuck_total,
        "messages": messages, "messagesTotal": open_total,
    }


async def sync_problems() -> dict:
    now = datetime.now(timezone.utc)
    branches = await Branch.all().order_by("name")
    return {"asOf": now.isoformat(), "branches": [await _branch_problems(b, now) for b in branches]}
