"""Head office's own lists and settings: what people keep up instead of the software fixing it.

`ItemListEntry` is one value on one of the godown Item master's lists (department, category, class, sub-class,
manufacturer, brand, unit, pack unit, GST rate), the same idea as a branch's Item Lists. The value stays written on
each Item as plain text, as it always was (the Purchase Summary, put-away suggestions and every branch report group
by that text). The entry is what lets someone see the list in one place, rename a value on every Item at once, merge
a duplicate, or switch a value off so the Item form stops offering it. `code` is the exact text on the Items.

`OfficeSetting` is one small group of head office settings per row (company details, alert timings, the usual
purchase approval limit of each role), kept as JSON so a new setting is a new key rather than a new column.
"""
from tortoise import fields, models


class ItemListEntry(models.Model):
    id = fields.UUIDField(pk=True)
    kind = fields.CharField(max_length=30)
    code = fields.CharField(max_length=160)
    active = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    updated_by_name = fields.CharField(max_length=120, null=True)

    class Meta:
        table = "item_list_entries"
        unique_together = (("kind", "code"),)


class OfficeSetting(models.Model):
    key = fields.CharField(max_length=40, pk=True)
    value = fields.JSONField(default=dict)
    updated_at = fields.DatetimeField(null=True)
    updated_by_name = fields.CharField(max_length=120, null=True)

    class Meta:
        table = "office_settings"
