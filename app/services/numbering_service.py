"""Where each document series starts: WGRN-0001, TR-0001, REQ-0001, WPO-0001, MV-00001, HO-PV-000001.

A series keeps its counter row (models/sequence.py), moved on in the same transaction as the document it numbers, so two
documents made at the same moment never share a number. This decides the number a series hands out:

- no counter row yet: one past the highest number the documents of that series already carry, so 1 on an empty system;
- a counter row: its value, unless a document already carries that number or a higher one (the counter fell behind, say
  after documents were copied in from elsewhere), and then one past the highest there is.

No series is primed with a starting number any more (they used to follow the old demo data: WGRN-0012, TR-0045,
REQ-0032). A number is never handed out twice, and nothing already numbered changes.
"""
import re

from tortoise.models import Model

from app.models import Counter


async def highest(model: type[Model], field: str, prefix: str, **filters) -> int:
    """The highest number after `prefix` on any document of `model` (0 when there is none). Only numbers written exactly
    as the series writes them count: with prefix "TR-", "TR-0045" is 45 but "GG-TR-0001", a branch's own, is not."""
    pattern = re.compile(re.escape(prefix) + r"(\d+)")
    top = 0
    for value in await _numbers_like(model, field, prefix, **filters):
        match = pattern.fullmatch(value or "")
        if match:
            top = max(top, int(match.group(1)))
    return top


# The characters LIKE treats as patterns. A prefix is a house constant like "WGRN-" and contains none of them, but
# escaping is cheaper than the day one does.
_LIKE_SPECIAL = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_", "[": "\\["})


async def _numbers_like(model: type[Model], field: str, prefix: str, **filters) -> list[str]:
    """Every value of `field` starting with `prefix`, asked in a way the index can answer.

    THE ORM'S `startswith` CASTS THE COLUMN, AND THAT THROWS THE INDEX AWAY
    ----------------------------------------------------------------------
    `filter(number__startswith=...)` makes Tortoise emit `CAST("number" AS VARCHAR) LIKE ?`. Wrapping the column in a
    function makes the predicate unsargable, so SQL Server cannot seek the unique index on `number` and instead reads
    all 1,110,000 rows and casts every one. Measured at 2,241 ms a call against 338 ms for the same LIKE without the
    cast, and 8 ms for a seek.

    It is called once for every document numbered, so a posting run creating 19,000 vouchers spent ten hours in here.
    That is what made the books look like they would never catch up: not the volume, one cast.

    The comment this replaces said the filter "is an index seek, not a scan" because the column carries a unique
    index. The index was there; the query could not use it.
    """
    if filters:
        # Nothing calls it this way today. Left on the ORM rather than hand-building a WHERE clause, with the cast
        # and the scan that implies, because a wrong number is worse than a slow one.
        return list(await model.filter(**{f"{field}__startswith": prefix}, **filters).values_list(field, flat=True))
    table = model._meta.db_table
    pattern = prefix.translate(_LIKE_SPECIAL) + "%"
    _, rows = await model._meta.db.execute_query(
        f'SELECT "{field}" AS v FROM "{table}" WHERE "{field}" LIKE ? ESCAPE {chr(39)}\\{chr(39)}', [pattern])
    return [row["v"] for row in rows]


async def next_number(series: str, model: type[Model], field: str, prefix: str, width: int, **filters) -> str:
    """The next document number of `series`: `prefix` and the number padded to `width` digits ("TR-0001"), for a `model`
    row that keeps it in `field`. Call it inside the caller's own transaction, so the number and the document that takes
    it commit or fail together.

    THIS USED TO ASK THE DATABASE TO MEASURE A STRING, AND SQL SERVER CANNOT
    -----------------------------------------------------------------------
    There was a `_taken_from` helper here that annotated the query with Tortoise's `Length`, compared padded numbers by
    length and then by alphabet, and asked `.exists()`. `Length` emits `LENGTH(...)`, which SQLite and MySQL have and
    SQL Server does not: it spells it `LEN(...)`. On SQL Server the whole statement failed to prepare, with
    "'LENGTH' is not a recognized built-in function name" followed by two more complaints about OFFSET and FETCH NEXT
    that were only consequences of it.

    That branch runs whenever the series already has a counter row, which every series in use has, so on SQL Server
    **no purchase order, GRN, transfer, requisition or bin move could be numbered at all** and none could be created.
    It was invisible for months because the schema ran on SQLite, where it works.

    `highest()` already reads the numbers and compares them in Python, which is the only place the padding rule is
    actually written down. So the database is no longer asked to reason about strings on its own: same answer, every
    engine, less code.

    The question "is this number already taken" is now asked of the one number, which is a seek on the column's own
    unique index, rather than by reading the whole series. See the comment in the body for what that cost.
    """
    counter = await Counter.get_or_none(id=series)
    if counter is None:
        # No counter yet, so the only way to know where the series has got to is to read it.
        number = await highest(model, field, prefix, **filters) + 1
        await Counter.create(id=series, value=number + 1)
        return f"{prefix}{number:0{width}d}"

    # The counter is the answer, and it is checked rather than trusted -- but checked by asking whether this one
    # number is taken, which is a seek on the column's unique index, and not by reading every number in the series.
    #
    # It used to call `highest()` here every time, on the stated grounds that a `LIKE 'prefix%'` filter on an indexed
    # column is a seek. It is not, through the ORM: `filter(field__startswith=...)` emits
    # `CAST("field" AS VARCHAR) LIKE ?`, and a column wrapped in a function cannot be sought. On `acc_vouchers` that
    # read 1,110,000 rows and cast each one -- 2,241 ms per document numbered, which is why posting head office's
    # godown to the books was going to take ten hours. Numbering a document is now one seek whatever the table holds.
    number = max(counter.value, 1)
    candidate = f"{prefix}{number:0{width}d}"
    if await model.filter(**{field: candidate}, **filters).exists():
        # Something took it anyway: a counter reset, a restore, or a document numbered outside this helper. Fall back
        # to reading the series, which is the slow-but-certain path and should be the rare one.
        number = await highest(model, field, prefix, **filters) + 1
    counter.value = number + 1
    await counter.save(update_fields=["value"])
    return f"{prefix}{number:0{width}d}"
