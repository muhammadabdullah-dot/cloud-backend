"""Turning a branch's events into head office's records.

Events land in the inbox first (see models/sync.py) and are projected straight after, one by one. A
projection that fails marks that event `failed` with the reason and leaves the rest of the batch alone;
the branch still gets its acknowledgement, because the event *is* safely here — what failed is head
office's handling of it, which is ours to fix and replay, not the branch's to resend.

Event types with no projector yet stay `stored`, untouched, for a later projection to pick up.
"""
from tortoise.transactions import in_transaction

from app.core import logs
from app.models import Branch, SyncInboxEvent
from app.services import loyalty_service, staff_sync_service, transfer_sync_service

PROJECTED = (
    "Staff", "Transfer", "Member", "LoyaltyEntry", "LoyaltySettings", "Activity", "AccChart", "AccVoucher", "AccSettings",
    "Supplier", "SupplierList",
)


def _mirror_error():
    from app.services.accounts_mirror_service import MirrorError

    return MirrorError


def _supplier_error():
    from app.services.supplier_sync_service import SupplierSyncError

    return SupplierSyncError


async def project(branch: Branch, event: SyncInboxEvent) -> None:
    if event.aggregate_type not in PROJECTED:
        return
    try:
        # All or nothing: a transfer whose third line fails must not leave head office with two lines.
        async with in_transaction():
            if event.aggregate_type == "Staff":
                await staff_sync_service.apply_from_branch(branch, (event.payload or {}).get("user") or {})
            elif event.aggregate_type == "Member":
                await loyalty_service.apply_member(branch, (event.payload or {}).get("member") or {})
            elif event.aggregate_type == "LoyaltyEntry":
                await loyalty_service.apply_entry(branch, (event.payload or {}).get("entry") or {})
            elif event.aggregate_type == "Activity":
                await _store_activity(branch, (event.payload or {}).get("activity") or {})
            elif event.aggregate_type == "AccChart":
                from app.services import accounts_mirror_service

                await accounts_mirror_service.apply_chart(branch, event.payload or {})
            elif event.aggregate_type == "AccVoucher":
                from app.services import accounts_mirror_service

                await accounts_mirror_service.apply_voucher(branch, event.payload or {})
            elif event.aggregate_type == "AccSettings":
                from app.services import accounts_mirror_service

                await accounts_mirror_service.apply_settings(branch, event.payload or {})
            elif event.aggregate_type == "LoyaltySettings":
                await loyalty_service.apply_settings(branch, (event.payload or {}).get("settings") or {})
            elif event.aggregate_type == "Supplier":
                from app.services import supplier_sync_service

                await supplier_sync_service.apply_from_branch(branch, event.payload or {})
            elif event.aggregate_type == "SupplierList":
                from app.services import supplier_sync_service

                await supplier_sync_service.send_list(branch)
            else:
                payload = dict(event.payload or {})
                payload.setdefault("transferId", event.aggregate_id)
                await transfer_sync_service.project(branch, payload)
        event.status = "applied"
        event.apply_error = None
    except (
        staff_sync_service.StaffError, transfer_sync_service.TransferSyncError, loyalty_service.LoyaltyError, _mirror_error(),
        _supplier_error(),
    ) as exc:
        event.status = "failed"
        event.apply_error = exc.message
        logs.log.warning("sync: %s event %s from %s not applied: %s", event.aggregate_type, event.id, branch.code, exc.message)
    except Exception as exc:  # noqa: BLE001 — one bad event must not take the push down
        event.status = "failed"
        event.apply_error = f"{type(exc).__name__}: {exc}"[:1000]
        logs.log.error("sync: %s event %s from %s failed", event.aggregate_type, event.id, branch.code, exc_info=exc)
    await event.save(update_fields=["status", "apply_error"])


async def _store_activity(branch: Branch, data: dict) -> None:
    from datetime import datetime, timezone

    from app.models import BranchActivity

    activity_id = data.get("id")
    if not activity_id or await BranchActivity.exists(id=activity_id):
        return
    at = data.get("at")
    parsed = datetime.fromisoformat(at) if isinstance(at, str) and at else datetime.now(timezone.utc)
    await BranchActivity.create(
        id=activity_id, branch=branch, at=parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc),
        user_id=data.get("userId"), user_name=(data.get("userName") or None) and str(data["userName"])[:180],
        user_title=data.get("userTitle"), action=str(data.get("action") or "")[:120] or "Activity",
        method=str(data.get("method") or "")[:10], route=data.get("route"), path=str(data.get("path") or "")[:255],
        params=data.get("params"), status_code=int(data.get("status") or 0), detail=data.get("detail"),
        device_id=data.get("deviceId"), ip=data.get("ip"),
    )


async def replay_failed() -> tuple[int, int]:
    """Try every failed Staff and Transfer event again, oldest first — after a fix to head office's
    handling, the receipts and account changes it couldn't apply take effect without any branch
    resending. Returns (applied now, still failing)."""
    applied = failing = 0
    events = await SyncInboxEvent.filter(status="failed", aggregate_type__in=list(PROJECTED)).order_by("received_at").prefetch_related("branch")
    for event in events:
        await project(event.branch, event)
        if event.status == "applied":
            applied += 1
        else:
            failing += 1
    return applied, failing
