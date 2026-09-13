"""Shared field types for schemas. Money exists because Tortoise's SQLite decimal round-trip can
return a normalized Decimal (e.g. 500000 -> Decimal('5E+5')), which Pydantic would otherwise
serialize as scientific notation — never what an API consumer wants for a currency amount.

Deliberately duplicated from the Branch Server rather than shared: the two services have no
common runtime package (contracts.md §1), so each carries its own copy.
"""
from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda v: format(Decimal(v), "f"), return_type=str)]
# Same fix, same root cause — any DecimalField (qty included) can round-trip through SQLite
# normalized, not just money. Separate alias purely for readability at the call site.
Qty = Money


def money_str(value: Decimal) -> str:
    """Same fixed-point fix as the Money/Qty serializers, for a Decimal interpolated directly
    into an f-string (error messages) rather than returned through a Pydantic schema."""
    return format(Decimal(value), "f")
