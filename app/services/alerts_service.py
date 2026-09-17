"""What each person at head office must do now, and what they should know.

**Tasks** ("needs your action") are worked out from the records every time they're asked for: a transfer to pick
and dispatch, a branch that declined one, a dispute to settle, an order waiting for approval, stock running low.
A task shows to everyone who holds the ability to act on it and disappears the moment somebody does. A step that
waits too long is marked overdue, and the longest waits reach the Executive too.

**Notices** ("for your information") are stored when something happens and each person marks them read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import CycleCount, Notice, NoticeRead, PurchaseOrder, Requisition, Transfer, User
from app.services.rbac_service import effective_permissions

NOTICE_DAYS = 30
NOTICE_LIMIT = 60

# A branch that hasn't checked in for this long counts as offline: the Warehouse Manager may then send to it
# without its acknowledgement, with a written reason.
OFFLINE_AFTER = timedelta(hours=2)
DISPATCH_OVERDUE = timedelta(hours=4)
APPROVAL_OVERDUE = timedelta(hours=24)
# Waits long enough to reach the Executive.
UNANSWERED_ESCALATE = timedelta(hours=24)
IN_TRANSIT_CHASE = timedelta(hours=24)
IN_TRANSIT_ESCALATE = timedelta(hours=72)

READY_TO_SEND = ("acknowledged", "skipped", "overridden", None)


async def use_saved_timings() -> None:
    """The timings above are what the software does until someone saves others in Admin > Company & Settings
    (office_settings_service puts the saved ones in their place)."""
    from app.services import office_settings_service

    await office_settings_service.warm()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    hours = int((_now() - _aware(dt)).total_seconds() // 3600)
    if hours < 1:
        return "less than an hour ago"
    return f"{hours} hour{'' if hours == 1 else 's'} ago" if hours < 48 else f"{hours // 24} days ago"


def _task(key: str, group: str, title: str, detail: str, link: str, since: datetime | None, overdue_after: timedelta) -> dict:
    since = _aware(since) or _now()
    return {"key": key, "group": group, "title": title, "detail": detail, "link": link, "since": since.isoformat(), "overdue": _now() - since >= overdue_after}


def _items(n: int) -> str:
    return f"{n} Item{'' if n == 1 else 's'}"


def branch_offline(branch) -> bool:
    return branch.last_pulled_at is None or _now() - _aware(branch.last_pulled_at) >= OFFLINE_AFTER


async def grants_of(user: User) -> dict[str, set[str]]:
    return {p.resource: set(p.actions) for p in await effective_permissions(user)}


async def notify(
    kind: str, title: str, *, body: str | None = None, link: str | None = None, tone: str = "info",
    audience_any: list[tuple[str, str]] | None = None, users: list[str] | None = None, subject: tuple[str, str] | None = None,
) -> Notice:
    return await Notice.create(
        kind=kind, title=title[:200], body=(body or None) and body[:500], link=link, tone=tone,
        subject_type=subject[0] if subject else None, subject_id=subject[1] if subject else None,
        audience={"any": [list(pair) for pair in (audience_any or [])], "users": [str(u) for u in (users or [])]},
    )


def _for(user: User, grants: dict[str, set[str]], audience: dict) -> bool:
    if str(user.id) in (audience or {}).get("users", []):
        return True
    return any(action in grants.get(resource, set()) for resource, action in (audience or {}).get("any", []))


# The resource each screen an alert links to needs Read on — mirrors cloud-app/src/registry/modules.ts.
# One alert reaches several audiences at once, and the Executive holds none of `warehouse.*` except
# purchase approvals, so the link has to be read against whoever is looking at it rather than fixed
# when the alert was raised. Otherwise Open drops them back on the front page with no explanation.
SCREEN_RESOURCE = {
    "/warehouse/transfers": "warehouse.transfers",
    "/warehouse/picking": "warehouse.picking",
    "/warehouse/requisitions": "warehouse.requisitions",
    "/warehouse/counts": "warehouse.counts",
    "/warehouse/receiving": "warehouse.receiving",
    "/warehouse/purchase-orders": "warehouse.purchase-orders",
    "/executive/purchase-orders": "warehouse.purchase-orders.approve",
    "/warehouse/suppliers": "warehouse.suppliers",
    "/accounts/dashboard": "accounts.desk",
    "/accounts/vouchers": "accounts.vouchers",
    "/accounts/settings": "accounts.settings",
}

# The same work on the reader's own screen, tried when the one the alert names is out of their reach.
# There is no Executive equivalent of the Transfers board, so that reader gets the alert with no Open
# rather than a link that goes nowhere — the title and detail say what happened on their own.
INSTEAD_OF = {"/warehouse/purchase-orders": "/executive/purchase-orders"}


def _can_open(link: str, grants: dict[str, set[str]]) -> bool:
    # The table covers every screen an alert links to today; anything else keeps the link it was raised with.
    resource = SCREEN_RESOURCE.get(link.split("?")[0])
    return resource is None or "R" in grants.get(resource, set())


def _link_for(link: str | None, grants: dict[str, set[str]]) -> str | None:
    if link is None or _can_open(link, grants):
        return link
    instead = INSTEAD_OF.get(link.split("?")[0])
    return instead if instead and _can_open(instead, grants) else None


# ── tasks ──────────────────────────────────────────────────────────────────────────────────────

async def _transfers(can) -> list[dict]:
    manage, dispatch, executive = can("warehouse.transfers.manage", "X"), can("warehouse.transfers.dispatch", "X"), can("executive.dashboard", "R")
    if not (manage or dispatch or executive):
        return []
    out: list[dict] = []
    now = _now()
    rows = await Transfer.filter(status__in=["approved", "requested", "dispatched", "in_transit"]).prefetch_related("branch", "source_branch", "lines")
    for t in rows:
        dest, number = t.branch, t.transfer_number
        sender = t.source_branch.name if t.source_branch_id else "the godown"
        if t.ack_status == "awaiting":
            since = _aware(t.ack_requested_at or t.requested_at)
            if manage and branch_offline(dest):
                out.append(_task(
                    f"transfer:{t.id}:offline", "Shipments", f"{dest.name} is offline, so {number} waits for its go-ahead",
                    f"{dest.name} last checked in {_ago(dest.last_pulled_at)}. Wait, or send it without their answer with a written reason.",
                    "/warehouse/transfers", since, timedelta(0),
                ))
            elif executive and now - since >= UNANSWERED_ESCALATE:
                out.append(_task(
                    f"transfer:{t.id}:unanswered", "Shipments", f"{dest.name} hasn't answered {number} for {int((now - since).total_seconds() // 3600)} hours",
                    f"{_items(len(t.lines))} from {sender} are waiting for {dest.name} to agree.", "/warehouse/transfers", since, timedelta(0),
                ))
        elif t.ack_status == "declined" and t.status in ("approved", "requested"):
            if manage:
                out.append(_task(
                    f"transfer:{t.id}:declined", "Shipments", f"{dest.name} declined {number}",
                    f"“{t.ack_note or 'No reason given'}” ({t.ack_by_name or dest.name}). Ask again or cancel it.",
                    "/warehouse/transfers", t.ack_at, APPROVAL_OVERDUE,
                ))
        elif t.status == "approved" and t.source_branch_id is None and t.ack_status in READY_TO_SEND and dispatch:
            answered = {"acknowledged": f"{dest.name} agreed", "overridden": "Cleared to go without the branch's answer", "skipped": f"{dest.name} asked for it"}
            out.append(_task(
                f"transfer:{t.id}:dispatch", "Shipments", f"Pick and dispatch {number} to {dest.name}",
                f"{answered.get(t.ack_status, 'Approved')}. {_items(len(t.lines))} to load.", "/warehouse/picking",
                t.ack_at or t.override_at or t.approved_at or t.requested_at, DISPATCH_OVERDUE,
            ))
        elif t.status in ("dispatched", "in_transit") and t.dispatched_at:
            waited = now - _aware(t.dispatched_at)
            if manage and waited >= IN_TRANSIT_CHASE or executive and waited >= IN_TRANSIT_ESCALATE:
                out.append(_task(
                    f"transfer:{t.id}:not-received", "Shipments", f"{dest.name} hasn't received {number}",
                    f"On its way from {sender} since {_ago(t.dispatched_at)}{f' (held: {t.hold_note})' if t.hold_note else ''}. Chase {dest.name}.",
                    "/warehouse/transfers", t.dispatched_at, timedelta(0),
                ))
    if manage:
        for t in await Transfer.filter(dispute_open=True).prefetch_related("branch"):
            out.append(_task(
                f"transfer:{t.id}:dispute", "Shipments", f"Settle the dispute on {t.transfer_number}",
                f"{t.branch.name}: {t.dispute_note or 'short or wrong delivery'}", "/warehouse/transfers", t.received_at or t.dispatched_at, APPROVAL_OVERDUE,
            ))
    return out


async def _requisitions_and_counts(user: User, can) -> list[dict]:
    out: list[dict] = []
    if can("warehouse.requisitions.approve", "X"):
        pending = await Requisition.filter(status="pending").order_by("requested_at")
        if pending:
            out.append(_task(
                "requisitions:decide", "Shipments", f"{len(pending)} branch request{'' if len(pending) == 1 else 's'} for stock to decide",
                "Approve to send the stock, or decline with a reason.", "/warehouse/requisitions", pending[0].requested_at, APPROVAL_OVERDUE,
            ))
    if can("warehouse.counts.approve", "X"):
        pending = await CycleCount.filter(status="pending").exclude(counted_by_id=user.id).order_by("at")
        if pending:
            out.append(_task(
                "counts:approve", "Godown", f"{len(pending)} cycle count{'' if len(pending) == 1 else 's'} to approve",
                "Counted by someone else and waiting for a decision.", "/warehouse/counts", pending[0].at, APPROVAL_OVERDUE,
            ))
    return out


async def _purchasing(user: User, can) -> list[dict]:
    from app.services import purchasing_service

    out: list[dict] = []
    if can("warehouse.purchase-orders.approve", "X"):
        for po in await PurchaseOrder.filter(status="pending_approval").exclude(raised_by_id=user.id).prefetch_related("supplier", "raised_by"):
            if purchasing_service.covers(user, po.total):
                out.append(_task(
                    f"po:{po.id}:approve", "Purchasing", f"Approve {po.po_number} to {po.supplier.name} for {purchasing_service.rs(po.total)}",
                    f"Raised by {po.raised_by.name}{f' · {po.reason}' if po.reason else ''}. Approve or reject it.",
                    "/warehouse/purchase-orders", po.submitted_at or po.raised_at, APPROVAL_OVERDUE,
                ))
    for po in await PurchaseOrder.filter(status="rejected", raised_by_id=user.id).prefetch_related("supplier"):
        out.append(_task(
            f"po:{po.id}:rejected", "Purchasing", f"{po.po_number} to {po.supplier.name} was rejected",
            f"“{po.decision_note}”. Change it and submit again, or cancel it.", "/warehouse/purchase-orders", po.submitted_at or po.raised_at, APPROVAL_OVERDUE,
        ))
    if can("warehouse.receiving", "W"):
        for po in await PurchaseOrder.filter(status__in=["approved", "partially_received"], expected_at__lt=_now()).prefetch_related("supplier"):
            out.append(_task(
                f"po:{po.id}:late", "Purchasing", f"{po.po_number} from {po.supplier.name} is late",
                f"Expected {_aware(po.expected_at):%d %b}. Receive what came against it, or chase the supplier.", "/warehouse/receiving", po.expected_at, timedelta(0),
            ))
    if can("warehouse.purchase-orders", "W"):
        low = await purchasing_service.low_stock()
        if low:
            names = ", ".join(r["name"] for r in low[:3]) + (f" and {len(low) - 3} more" if len(low) > 3 else "")
            out.append(_task(
                "stock:low", "Purchasing", f"{_items(len(low))} running low at the godown", f"{names}: at or below their reorder level with nothing on order.",
                "/warehouse/purchase-orders", _now(), timedelta(hours=1),
            ))
    return out


async def _accounts(can) -> list[dict]:
    from app.models import HEAD_OFFICE_BOOK, AccountsSettings, Voucher

    out: list[dict] = []
    if can("accounts.vouchers.post", "X"):
        drafts = await Voucher.filter(book=HEAD_OFFICE_BOOK, status="draft", auto=False).order_by("created_at")
        if drafts:
            out.append(_task("vouchers:post", "Accounts", f"{len(drafts)} voucher{'' if len(drafts) == 1 else 's'} waiting to be posted",
                             "Saved as drafts, so they aren't in head office's books until someone posts them.", "/accounts/vouchers?status=draft",
                             drafts[0].created_at, APPROVAL_OVERDUE))
    if can("accounts.period", "X"):
        row = await AccountsSettings.get_or_none(book=HEAD_OFFICE_BOOK)
        if row and row.posting_problems:
            out.append(_task("accounts:posting", "Accounts", "Some godown records couldn't be posted to the books", str(row.posting_problems[0])[:200],
                             "/accounts/settings", row.last_posting_at, timedelta(hours=4)))
    if can("accounts.branch-books", "R"):
        from app.models import Branch

        for row in await AccountsSettings.exclude(book=HEAD_OFFICE_BOOK):
            if row.posting_problems:
                branch = await Branch.get_or_none(code=row.book)
                out.append(_task(f"accounts:posting:{row.book}", "Accounts", f"{branch.name if branch else row.book} has records it couldn't post",
                                 str(row.posting_problems[0])[:200], f"/accounts/dashboard?book={row.book}", row.last_posting_at, timedelta(hours=12)))
    return out


async def tasks_for(user: User, grants: dict[str, set[str]] | None = None) -> list[dict]:
    await use_saved_timings()
    grants = grants if grants is not None else await grants_of(user)

    def can(resource: str, action: str) -> bool:
        return action in grants.get(resource, set())

    tasks = await _transfers(can) + await _requisitions_and_counts(user, can) + await _purchasing(user, can) + await _accounts(can)
    for task in tasks:
        task["link"] = _link_for(task["link"], grants)
    return sorted(tasks, key=lambda t: (not t["overdue"], t["since"]))


# ── notices ────────────────────────────────────────────────────────────────────────────────────

async def _low_stock_notice() -> None:
    """Once a day, tell the Executive how many godown Items are running low with nothing on order."""
    from app.services import purchasing_service

    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    if await Notice.filter(kind="stock.low", at__gte=today).exists():
        return
    low = await purchasing_service.low_stock()
    if low:
        await notify(
            "stock.low", f"{_items(len(low))} running low at the godown",
            body=", ".join(r["name"] for r in low[:5]) + (" …" if len(low) > 5 else "") + ": at or below reorder level, nothing on order.",
            link="/warehouse/purchase-orders", audience_any=[("executive.dashboard", "R"), ("warehouse.purchase-orders", "W")], tone="warning",
        )


async def notices_for(user: User, grants: dict[str, set[str]] | None = None) -> list[dict]:
    await use_saved_timings()
    grants = grants if grants is not None else await grants_of(user)
    rows = await Notice.filter(at__gte=_now() - timedelta(days=NOTICE_DAYS)).order_by("-at").limit(400)
    mine = [n for n in rows if _for(user, grants, n.audience)][:NOTICE_LIMIT]
    read = set(await NoticeRead.filter(user=user, notice_id__in=[n.id for n in mine]).values_list("notice_id", flat=True))
    return [{
        "id": str(n.id), "at": _aware(n.at).isoformat(), "kind": n.kind, "title": n.title, "body": n.body,
        "link": _link_for(n.link, grants), "tone": n.tone, "read": n.id in read,
    } for n in mine]


async def summary(user: User) -> dict:
    grants = await grants_of(user)
    await _low_stock_notice()
    tasks = await tasks_for(user, grants)
    notices = await notices_for(user, grants)
    return {"tasks": tasks, "notices": notices, "unread": sum(1 for n in notices if not n["read"])}


async def mark_read(user: User, ids: list[str] | None) -> int:
    grants = await grants_of(user)
    visible = {n["id"] for n in await notices_for(user, grants) if not n["read"]}
    wanted = visible if ids is None else visible & {str(i) for i in ids}
    for notice_id in wanted:
        await NoticeRead.get_or_create(notice_id=notice_id, user=user)
    return len(wanted)

