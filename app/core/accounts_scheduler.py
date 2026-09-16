"""Head office's own background work, on a timer, whether or not anyone has a screen open.

Two jobs, two tasks, because they fail and are switched off independently:

* posting head office's books from the godown's records;
* trying again on branch events head office couldn't apply — a voucher whose account was still in
  flight, a transfer whose branch hadn't arrived yet. These are replayed once at startup, and that
  used to be the only time: on a server that runs for months, an event that failed by a few seconds
  would sit in `failed` until somebody happened to restart it.
"""
import asyncio
import contextlib
import os

from app.core import logs

_task: asyncio.Task | None = None
_replay_task: asyncio.Task | None = None
INTERVAL = int(os.environ.get("ACCOUNTS_POSTING_SECONDS", "300"))
# Fifteen minutes. A branch pushes every couple of minutes, so anything that failed because its
# dependency was still in flight is almost always applicable by the next replay; and a replay with
# nothing to do is one indexed query, so there is no reason to make it rarer than that.
REPLAY_INTERVAL = int(os.environ.get("SYNC_REPLAY_SECONDS", "900"))

# How many were still failing last time. A count that hasn't moved is not news — see `_replay_once`.
_still_failing = 0


async def _loop() -> None:
    from app.services import accounts_posting_service

    await asyncio.sleep(int(os.environ.get("ACCOUNTS_POSTING_DELAY_SECONDS", "45")))
    while True:
        try:
            await accounts_posting_service.run(full=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive anything one run can do
            logs.log.error("accounts posting: run failed", exc_info=exc)
        await asyncio.sleep(max(INTERVAL, 60))


async def _replay_once() -> None:
    global _still_failing

    from app.services import projector

    applied, failing = await projector.replay_failed()
    # Only when something changed. A permanently unapplicable event would otherwise write the same
    # line every quarter of an hour for as long as the server runs, and the one replay that did put
    # a receipt right would be buried in it.
    if applied or failing != _still_failing:
        logs.log.info("sync: replayed failed branch events: %s applied, %s still failing", applied, failing)
    _still_failing = failing


async def _replay_loop() -> None:
    # After the startup replay in main.py has had its go, so the first one here isn't a repeat of it.
    await asyncio.sleep(int(os.environ.get("SYNC_REPLAY_DELAY_SECONDS", "180")))
    while True:
        try:
            await _replay_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive anything one replay can do
            logs.log.error("sync: replaying failed branch events failed", exc_info=exc)
        await asyncio.sleep(max(REPLAY_INTERVAL, 60))


def start() -> None:
    global _task, _replay_task
    if os.environ.get("ACCOUNTS_POSTING", "on").lower() == "off":
        print("  accounts posting: switched off (ACCOUNTS_POSTING=off)", flush=True)
    elif _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="accounts-posting")
    # Deliberately not behind the posting switch: turning book posting off is a decision about the
    # books, not about whether branch events ever get applied.
    if _replay_task is None or _replay_task.done():
        _replay_task = asyncio.create_task(_replay_loop(), name="sync-replay-failed")


async def stop() -> None:
    global _task, _replay_task
    for task in (_task, _replay_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _task = None
    _replay_task = None
