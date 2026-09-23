"""Sorting a list by a column the way a person reads it.

Codes go in number order: a shorter code is the smaller number, so 99999 comes before 710005 and 710005 before a
seven-digit 1000001, where plain text order would scatter them. Words ignore capitals, so "apple" sits beside "Apple"
instead of after every capitalised name.
"""
from tortoise.functions import Length, Lower


def by_column(qs, field: str, direction: str, kind: str | None = None):
    """The query and the order_by terms for sorting by `field` ("-" or "" for `direction`). `kind` is "code" for a
    code or number held as text, "text" for a name or other words, or None to sort the stored value as it is."""
    if kind == "code":
        return qs.annotate(sort_len=Length(field)), [f"{direction}sort_len", f"{direction}{field}"]
    if kind == "text":
        return qs.annotate(sort_text=Lower(field)), [f"{direction}sort_text"]
    return qs, [f"{direction}{field}"]
