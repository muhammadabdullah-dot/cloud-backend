"""A branch's books, as the branch sends them.

Every chart change and every posted voucher at a branch travels up as an event and lands here under the branch's
code, replacing what head office had for it. Nothing is worked out again at head office — the copy is the branch's
books to the paisa — so the branch and head office can never disagree about what a branch posted.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.models import Account, AccountsSettings, Branch, SyncInboxEvent, Voucher, VoucherLine
from app.services import accounts_chart_service


class MirrorError(Exception):
    def __init__(self, message: str):
        self.message = message


def _day(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _dt(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _plain_time(value) -> datetime | None:
    """A stored time made comparable with the others: parsed if it came back as text, and in UTC without a zone."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


async def _later_applied(branch: Branch, aggregate_type: str, aggregate_ids: list[str], event: SyncInboxEvent | None) -> list[dict]:
    """Changes to the same thing from this branch that head office has already applied and that the branch made after
    this one (by when the branch recorded them, then by when they arrived). A failed event tried again later must never
    undo them."""
    if event is None or not getattr(event, "id", None):
        return []
    mine = await SyncInboxEvent.filter(id=event.id).values("occurred_at", "received_at")
    if not mine:
        return []
    received = _plain_time(mine[0]["received_at"])
    my_order = (_plain_time(mine[0]["occurred_at"]) or received, received)
    rows = await SyncInboxEvent.filter(
        branch_id=branch.id, aggregate_type=aggregate_type, aggregate_id__in=aggregate_ids, status="applied",
    ).exclude(id=event.id).values("occurred_at", "received_at", *(["payload"] if aggregate_type == "AccVoucher" else []))
    later = []
    for row in rows:
        row_received = _plain_time(row["received_at"])
        if (_plain_time(row["occurred_at"]) or row_received, row_received) > my_order:
            later.append(row)
    return later


async def _touch(book: str) -> None:
    row = await AccountsSettings.get_or_none(book=book)
    if row is None:
        await AccountsSettings.create(book=book, last_received_at=datetime.now(timezone.utc))
    else:
        row.last_received_at = datetime.now(timezone.utc)
        await row.save(update_fields=["last_received_at", "updated_at"])


async def apply_chart(branch: Branch, payload: dict, event: SyncInboxEvent | None = None) -> None:
    book = branch.code
    kind = payload.get("kind")
    if event is not None:
        # "group:X" and "groupDeleted:X" are the same group, so a late add can't bring back a group deleted after it.
        kind_part, _, key = str(event.aggregate_id or "").partition(":")
        base = kind_part.removesuffix("Deleted")
        if await _later_applied(branch, "AccChart", [f"{base}:{key}", f"{base}Deleted:{key}"], event):
            return  # an older change arriving late; the branch's newer one is already in head office's copy
    try:
        if kind == "group":
            await accounts_chart_service.apply_group(book, payload.get("group") or {})
        elif kind == "subGroup":
            await accounts_chart_service.apply_sub_group(book, payload.get("subGroup") or {})
        elif kind == "account":
            await accounts_chart_service.apply_account(book, payload.get("account") or {})
        elif kind == "accountDeleted":
            await accounts_chart_service.delete_branch_account(book, (payload.get("accountDeleted") or {}).get("id"))
        elif kind == "groupDeleted":
            await accounts_chart_service.delete_branch_group(book, payload.get("groupDeleted") or {})
        elif kind == "subGroupDeleted":
            await accounts_chart_service.delete_branch_sub_group(book, payload.get("subGroupDeleted") or {})
    except accounts_chart_service.ChartError as exc:
        raise MirrorError(exc.message) from exc
    await _touch(book)


async def _account_for(book: str, line: dict) -> str:
    account_id = line.get("accountId")
    if account_id and await Account.exists(id=account_id, book=book):
        return account_id
    raise MirrorError(f"A voucher line from {book} uses account {line.get('accountCode') or account_id}, which hasn't arrived yet.")


async def apply_voucher(branch: Branch, payload: dict, event: SyncInboxEvent | None = None) -> None:
    book = branch.code
    data = payload.get("voucher") or {}
    voucher_id = data.get("id")
    if not voucher_id:
        raise MirrorError("A voucher from the branch had no id.")
    existing = await Voucher.get_or_none(id=voucher_id)
    if existing and existing.book != book:
        raise MirrorError(f"Voucher {voucher_id} already belongs to book {existing.book}.")
    if data.get("deleted"):
        if existing:
            await existing.delete()
        await _touch(book)
        return
    if existing and int(data.get("version") or 0) < existing.version:
        return  # an older copy arriving late
    incoming_version = int(data.get("version") or 0)
    for later in await _later_applied(branch, "AccVoucher", [str(voucher_id)], event):
        later_voucher = (later.get("payload") or {}).get("voucher") or {}
        if later_voucher.get("deleted") or int(later_voucher.get("version") or 0) > incoming_version:
            return  # the branch deleted or changed this voucher after this copy; a late copy must not bring it back
    source = f"{book}:{data['source']}"[:160] if data.get("source") else None
    other = await Voucher.filter(source=source).exclude(id=voucher_id).first() if source else None
    if other and await _later_applied(branch, "AccVoucher", [str(other.id)], event):
        return  # the branch has since posted a newer voucher for the same thing; keep that one
    account_ids =[await _account_for(book, line) for line in data.get("lines") or []]
    header_id = data.get("headerAccountId")
    if header_id and not await Account.exists(id=header_id, book=book):
        header_id = None
    number = str(data.get("number") or voucher_id)[:30]
    clash = await Voucher.filter(number=number).exclude(id=voucher_id).first()
    if clash:
        clash.number = f"{clash.number[:24]}-{str(clash.id)[:5]}"
        await clash.save(update_fields=["number"])
    fields = dict(
        book=book, number=number, vtype=str(data.get("vtype") or "JV")[:4], date=_day(data.get("date")), status=data.get("status") or "posted",
        auto=bool(data.get("auto")), source=source,
        header_account_id=header_id, reference_no=data.get("referenceNo"), description=(data.get("description") or None) and str(data["description"])[:500],
        cheque_no=data.get("chequeNo"), cheque_date=_day(data.get("chequeDate")), total=Decimal(str(data.get("total") or "0")),
        created_by_name=data.get("createdBy"), posted_by_name=data.get("postedBy"), posted_at=_dt(data.get("postedAt")),
        cancelled_by_name=data.get("cancelledBy"), cancel_reason=data.get("cancelReason"), reversal_of_id=data.get("reversalOf"),
        reversed=bool(data.get("reversed")), version=int(data.get("version") or 1), mirrored=True,
    )
    if other:
        await other.delete()  # older than this one (checked above): the branch replaced it
    if existing:
        await Voucher.filter(id=voucher_id).update(**fields)
        voucher = await Voucher.get(id=voucher_id)
    else:
        voucher = await Voucher.create(id=voucher_id, **fields)
    await VoucherLine.filter(voucher=voucher).delete()
    for index, (line, account_id) in enumerate(zip(data.get("lines") or [], account_ids)):
        await VoucherLine.create(
            voucher=voucher, line_no=index + 1, account_id=account_id, debit=Decimal(str(line.get("debit") or "0")),
            credit=Decimal(str(line.get("credit") or "0")), description=(line.get("description") or None) and str(line["description"])[:255],
            reference_no=line.get("referenceNo"),
        )
    await _touch(book)


async def apply_settings(branch: Branch, payload: dict, event: SyncInboxEvent | None = None) -> None:
    if event is not None and await _later_applied(branch, "AccSettings", [str(event.aggregate_id or "settings")], event):
        return  # older settings arriving late; the branch's newer ones are already here
    data = payload.get("settings") or {}
    row = await AccountsSettings.get_or_none(book=branch.code)
    if row is None:
        row = await AccountsSettings.create(book=branch.code)
    row.fiscal_start_month = int(data.get("fiscalStartMonth") or row.fiscal_start_month)
    row.books_start = _day(data.get("booksStart"))
    row.locked_until = _day(data.get("lockedUntil"))
    row.last_posting_at = _dt(data.get("lastPostingAt"))
    row.last_posting_note = data.get("lastPostingNote")
    row.posting_problems = data.get("postingProblems") or []
    row.last_received_at = datetime.now(timezone.utc)
    await row.save()
