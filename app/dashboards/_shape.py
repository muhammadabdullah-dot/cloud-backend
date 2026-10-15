"""The shapes every dashboard answers in, so each one does not invent its own.

A visual is `{id, kind, title, dimension, rows}`. `kind` is what the app draws -- `bars`, `line` or `table`. And
`dimension` is the cross-filter: it names what clicking a row narrows by, the app sends that straight back as a
query parameter, and every visual on the page redraws because they all read the same scope.

A visual with `dimension: None` is not clickable, which is the right answer when a chart cannot be narrowed any
further or its rows are not a dimension at all -- an hour of the day, a day on a trend line.
"""
from __future__ import annotations

from decimal import Decimal

from app.services import dashboards_service as ds


def money(value) -> str:
    """Two decimals, as a string. The app formats for the reader; this never loses a paisa on the way."""
    return f"{ds._d(value):.2f}"


def card(label: str, value: str, kind: str = "money", sub: str | None = None) -> dict:
    return {"label": label, "value": value, "kind": kind, "sub": sub}


def bars(id_: str, title: str, dimension: str | None, rows: list[dict],
         note: str | None = None, value_key: str = "net") -> dict:
    """A clickable bar chart from any fold that returns `key_`, `label` and a value.

    `key` is what goes back as the filter and `label` is what a person reads -- they differ for a branch (an id
    and a name) and for an Item (a code and a name), and a chip that reads a uuid is a chip nobody can use.
    """
    return {
        "id": id_, "kind": "bars", "title": title, "dimension": dimension, "note": note,
        "rows": [{
            "key": str(r.get("key_") or ""),
            "label": str(r.get("label") or r.get("key_") or "--"),
            "value": money(r.get(value_key)),
            "qty": money(r.get("qty")),
            "profit": money(ds._d(r.get("net")) - ds._d(r.get("cogs"))),
        } for r in rows],
    }


def donut(id_: str, title: str, dimension: str | None, rows: list[dict],
          note: str | None = None, value_key: str = "net") -> dict:
    """A share of the whole, as a ring. Same rows as `bars`, and clickable the same way.

    USE IT ONLY WHEN THE SHARE IS THE QUESTION AND THERE ARE FEW SLICES. Eight is about the limit: past that the
    slices stop being distinguishable and a reader is left matching colours to a legend, which a bar chart with
    the names written down the side never asks anyone to do. "Which of three ABC classes earns the money" and
    "how is the stock split between four branches" are ring questions; "the top fourteen categories" is not, and
    stays as bars.

    A NEGATIVE VALUE CANNOT BE A SLICE of a whole, so the app drops them from the ring and says how many it
    dropped. That is a real case here -- an Item whose returns outran its sales has negative revenue -- and
    drawing it as a slice of a circle would quietly add it to the total instead of subtracting it.
    """
    return {**bars(id_, title, dimension, rows, note, value_key), "kind": "donut"}


def line(id_: str, title: str, rows: list[dict], label_key: str = "label", value_key: str = "net") -> dict:
    return {
        "id": id_, "kind": "line", "title": title, "dimension": None,
        "rows": [{"label": str(r.get(label_key))[:10], "value": money(r.get(value_key))} for r in rows],
    }


def table(id_: str, title: str, dimension: str | None, columns: list[dict], rows: list[dict],
          note: str | None = None) -> dict:
    return {"id": id_, "kind": "table", "title": title, "dimension": dimension, "note": note,
            "columns": columns, "rows": rows}


def pct(part: Decimal, whole: Decimal) -> str:
    return f"{part / whole * 100:.1f}" if whole else "0.0"
