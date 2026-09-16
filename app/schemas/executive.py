"""Executive wire shapes.

One generic KPI shape and one generic drill-down shape, rather than a bespoke schema per KPI.
That is what lets a single drill-down screen render any of them, and what stops the frontend
growing a hardcoded formatting rule per metric.

`source` and `asOf` are on every KPI on purpose: a live godown figure and a branch's
last-reported figure are different claims, and an executive acting on one needs to know which.
"""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.types import Money


class PeriodOut(BaseModel):
    id: str
    label: str
    start: date
    end: date
    compareLabel: str


class KpiDelta(BaseModel):
    value: Money
    percent: float | None = None
    # 'up' is not the same as 'good' — a rise in out-of-stock lines is a fall in health, so the
    # client colours by `good`, not by direction.
    direction: str
    good: bool | None = None
    label: str


class KpiOut(BaseModel):
    id: str
    label: str
    group: str
    hint: str
    value: Decimal | None = None
    # Server-formatted headline, so every screen spells a figure the same way.
    display: str
    unit: str
    sub: str | None = None
    delta: KpiDelta | None = None
    source: str
    asOf: datetime | None = None
    severity: str | None = None
    ctaLabel: str


class NotBuiltOut(BaseModel):
    """A KPI the blueprint asks for that has no data behind it yet. Listed rather than omitted so
    the gap is visible instead of looking like an oversight."""
    label: str
    needs: str


class ChartPoint(BaseModel):
    """One bar. Carries its own destination, so a chart is navigable the same way a table is —
    clicking the tallest bar and clicking the top table row land in the same place."""

    label: str
    # The raw magnitude the bar is drawn from, and the text a person reads. Kept apart because
    # "Rs 93.7k" cannot be divided by a maximum, and "93726.00" is not something to put on a chart.
    value: Money
    display: str
    sub: str | None = None
    linkTo: str | None = None
    linkParam: str | None = None
    linkValue: str | None = None
    # 'accent' marks the bar worth the eye — the current hour, the branch you are standing in.
    tone: str | None = None


class ChartOut(BaseModel):
    """A chart the server has decided the shape of, for the same reason the KPI tiles are shaped
    here: a figure formatted in one place and plotted in another is how the two quietly disagree.

    `kind` is 'bar-h' for a ranking (a category axis of names, which needs room to be read) and
    'bar-v' for a profile over time (an axis that is inherently horizontal).
    """

    id: str
    title: str
    subtitle: str | None = None
    kind: str
    unit: str
    points: list[ChartPoint]
    emptyText: str
    # Where the whole chart goes when there is more than fits on it.
    ctaLabel: str | None = None
    ctaKpi: str | None = None
    note: str | None = None


class KpiListOut(BaseModel):
    period: PeriodOut
    periods: list[PeriodOut]
    businessDate: date
    asOf: datetime | None = None
    items: list[KpiOut]
    charts: list[ChartOut] = []
    notBuilt: list[NotBuiltOut]


class SeriesPoint(BaseModel):
    day: str
    value: Money
    # Set when the axis isn't a date — an hour-of-day profile, for instance. The client shows
    # this verbatim instead of trying to parse `day` as a date.
    label: str | None = None


class TableColumn(BaseModel):
    key: str
    label: str
    align: str = "left"
    format: str = "text"
    # A cell becomes a link into another KPI when these are set: open `linkTo`, passing the value
    # of `linkValueKey` (defaulting to this column's own key) as query param `linkParam`. Keeping
    # the link on the COLUMN rather than the row means rows stay pure data — no magic keys mixed
    # in with the figures.
    linkTo: str | None = None
    linkParam: str | None = None
    linkValueKey: str | None = None
    # For a table whose rows are not all the same kind — Inventory Value lists branch floors and
    # the godown side by side, and those drill into different places. Names a row field holding
    # the KPI id for THAT row; it overrides `linkTo`, and a row leaving it empty is not a link.
    linkToKey: str | None = None
    # A link that needs more than one thing to land: one person's sales of one item wants both the
    # person and the item. Maps query param -> row field; when set it replaces `linkParam`.
    linkParams: dict[str, str] | None = None


class Crumb(BaseModel):
    """One step back up the way the reader came: which view, focused on what."""
    label: str
    kpi: str
    focus: dict[str, str] = {}


class DetailSection(BaseModel):
    """A further table under the main one. A person is more than one list: what they sold, their days,
    the categories, the tills they closed, the discounts and returns that went through them."""
    title: str
    subtitle: str | None = None
    columns: list[TableColumn] = []
    rows: list[dict] = []
    emptyText: str = "Nothing to show for this period."
    # An onward link for the section as a whole ("Open the item").
    actionLabel: str | None = None
    actionKpi: str | None = None
    actionFocus: dict[str, str] = {}


class KpiDetailOut(BaseModel):
    id: str
    label: str
    group: str
    hint: str
    # Set when this view is focused on one thing — a category, a product, a cashier, a day. The
    # client shows it as a subtitle and offers a way back up.
    focusLabel: str | None = None
    parentKpi: str | None = None
    parentLabel: str | None = None
    # The whole way back up when it is more than one step (Best Salesperson > Hina Malik > this item).
    # When set, it replaces parentKpi/parentLabel in the breadcrumb.
    trail: list[Crumb] = []
    headline: KpiOut
    period: PeriodOut
    seriesLabel: str | None = None
    series: list[SeriesPoint] = []
    columns: list[TableColumn] = []
    rows: list[dict] = []
    emptyText: str = "Nothing to show for this period."
    # What the main table is, when "Breakdown" undersells it ("Who sold it", "What they sold").
    tableTitle: str | None = None
    sections: list[DetailSection] = []
    # How the figure was computed, and what it deliberately does not include.
    notes: list[str] = []
    source: str
    asOf: datetime | None = None
