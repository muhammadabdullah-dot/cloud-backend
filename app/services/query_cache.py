"""A short-lived memo for read queries that a single screen asks for more than once.

Drawing the Company Dashboard for a year issues **128 SQL statements, of which only 57 are distinct**. The other 71
are the same statement with the same parameters, asked again by a different tile: the Item names by sku are fetched
three times, each Item's first sale twice, the bucketed demand twice. Against their full history that is **12.1 of
the 39.8 seconds**, spent computing answers already in hand.

Why here rather than in each caller: the repeats are not one function calling itself, they are eleven tiles built by
different builders that happen to need the same underlying rows. Caching at the query is the only place that sees
all of them, and it is the only place where the correctness argument is trivial: **the same SQL with the same
parameters against an unchanged database returns the same rows.** No business logic is involved and none can be got
wrong by it.

WHAT KEEPS IT HONEST
--------------------
* **Reads only.** `remember()` is called from the two read helpers. Nothing that writes goes near it.
* **Seconds, not minutes.** `TTL_SECONDS` is deliberately a little longer than one slow render and no longer. A
  dashboard that is a few seconds behind is a dashboard; one that is minutes behind is a lie with a timestamp on it.
* **Cleared when anything lands.** `clear()` is called by the services that write branch figures and documents, so a
  sync arriving is visible immediately rather than after the clock runs out.
* **Bounded.** `MAX_ENTRIES` caps it, oldest first, so a year of distinct date ranges cannot grow it without limit.

It is not a substitute for the queries being fast. The folds this dashboard used to do in Python were moved into SQL
first, and the tables it scans were indexed; this removes what is left, which is the same question asked twice.
"""
from __future__ import annotations

import time
from typing import Any

# A little longer than one slow render of the heaviest view, and no longer.
TTL_SECONDS = 45.0
MAX_ENTRIES = 512

_entries: dict[tuple, tuple[float, Any]] = {}
_hits = 0
_misses = 0


def _key(sql: str, args) -> tuple:
    # The parameters are part of the question. A key that ignored them would answer January with December.
    return (sql, tuple(str(a) for a in (args or ())))


async def remember(sql: str, args, run):
    """`run()`'s answer, or the one it gave a moment ago for exactly this question."""
    global _hits, _misses
    key = _key(sql, args)
    now = time.monotonic()
    found = _entries.get(key)
    if found is not None and now - found[0] < TTL_SECONDS:
        _hits += 1
        return found[1]
    _misses += 1
    value = await run()
    if len(_entries) >= MAX_ENTRIES:
        # Oldest out first. Cheap because this only happens at the ceiling.
        for old in sorted(_entries, key=lambda k: _entries[k][0])[: MAX_ENTRIES // 4]:
            _entries.pop(old, None)
    _entries[key] = (now, value)
    return value


def clear() -> int:
    """Forget everything. Called when figures or documents land, so a sync shows at once."""
    count = len(_entries)
    _entries.clear()
    return count


def stats() -> dict:
    total = _hits + _misses
    return {"entries": len(_entries), "hits": _hits, "misses": _misses,
            "hitRate": round(_hits / total * 100, 1) if total else 0.0,
            "ttlSeconds": TTL_SECONDS}
