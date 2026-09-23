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

from tortoise.expressions import Q
from tortoise.functions import Length
from tortoise.models import Model

from app.models import Counter


async def highest(model: type[Model], field: str, prefix: str, **filters) -> int:
    """The highest number after `prefix` on any document of `model` (0 when there is none). Only numbers written exactly
    as the series writes them count: with prefix "TR-", "TR-0045" is 45 but "GG-TR-0001", a branch's own, is not."""
    pattern = re.compile(re.escape(prefix) + r"(\d+)")
    top = 0
    for value in await model.filter(**{f"{field}__startswith": prefix}, **filters).values_list(field, flat=True):
        match = pattern.fullmatch(value or "")
        if match:
            top = max(top, int(match.group(1)))
    return top


async def _taken_from(model: type[Model], field: str, number: str, prefix: str, **filters) -> bool:
    """Whether a document of the series already carries `number` or one after it. The numbers are padded to one width, so
    a later one is longer, or as long and later in the alphabet."""
    return await (
        model.filter(**{f"{field}__startswith": prefix}, **filters).annotate(size=Length(field))
        .filter(Q(size__gt=len(number)) | Q(Q(size=len(number)), Q(**{f"{field}__gte": number})))
        .exists()
    )


async def next_number(series: str, model: type[Model], field: str, prefix: str, width: int, **filters) -> str:
    """The next document number of `series`: `prefix` and the number padded to `width` digits ("TR-0001"), for a `model`
    row that keeps it in `field`. Call it inside the caller's own transaction, so the number and the document that takes
    it commit or fail together."""
    counter = await Counter.get_or_none(id=series)
    if counter is None:
        number = await highest(model, field, prefix, **filters) + 1
        await Counter.create(id=series, value=number + 1)
    else:
        number = max(counter.value, 1)
        if await _taken_from(model, field, f"{prefix}{number:0{width}d}", prefix, **filters):
            number = max(number, await highest(model, field, prefix, **filters) + 1)
        counter.value = number + 1
        await counter.save(update_fields=["value"])
    return f"{prefix}{number:0{width}d}"
