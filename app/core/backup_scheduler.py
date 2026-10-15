"""The daily backup, on a timer inside the server — nobody has to remember to press Backup Now."""
import asyncio
import contextlib
import os

from app.core import logs

_task: asyncio.Task | None = None
INTERVAL = int(os.environ.get("BACKUP_CHECK_SECONDS", "600"))


async def _loop() -> None:
    from app.services import backup_service

    # Not in the rush of startup: branches reporting in comes first.
    await asyncio.sleep(int(os.environ.get("BACKUP_DELAY_SECONDS", "300")))
    while True:
        try:
            row = await backup_service.automatic_tick()
            if row:
                logs.log.info("backup: daily backup saved as %s", row["name"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive anything one run can do
            logs.log.error("backup: daily backup failed: %s", getattr(exc, "message", None) or exc, exc_info=exc)
        await asyncio.sleep(max(INTERVAL, 60))


def start() -> None:
    global _task
    if os.environ.get("BACKUP_AUTO", "on").lower() == "off":
        print("  backup: daily backup switched off (BACKUP_AUTO=off)", flush=True)
        return
    # backup_service takes its copy with SQLite's own VACUUM INTO, so on any other engine every tick would raise.
    # The loop below survives that, but it would write the same failure to the log every ten minutes for ever, which
    # buries the failures worth reading. Said once here instead, and the Maintenance screen says it too.
    from app.core.config import settings

    if not settings.db_url.startswith("sqlite://"):
        print("  backup: daily backup stood down. This database is not SQLite, and the backup is SQLite's own\n"
              "          VACUUM INTO. Back it up with the engine's own tooling until backup_service is ported.",
              flush=True)
        logs.log.warning("backup: daily backup stood down, db_url is not sqlite")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="daily-backup")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    _task = None
