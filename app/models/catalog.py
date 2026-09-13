"""Master data the godown works against: the item list, the vendor list, and the bin map.

These are Cloud-local tables. frontend-baseline.md §4 point 4 flags that a *shared* product
catalog between Branch and Cloud is the real end state — today each service keeps its own, and
cloud-app kept its copy as a hand-maintained TypeScript const. Giving Cloud a real table is the
prerequisite for that eventual share; it is not the share itself.
"""
from tortoise import fields, models


class Product(models.Model):
    id = fields.CharField(max_length=60, pk=True)
    sku = fields.CharField(max_length=60, unique=True)
    name = fields.CharField(max_length=200)
    price = fields.DecimalField(max_digits=14, decimal_places=2)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    is_weighed = fields.BooleanField(default=False)
    unit = fields.CharField(max_length=30, default="pc")
    pack_unit = fields.CharField(max_length=30, null=True)
    pack_size = fields.IntField(null=True)

    class Meta:
        table = "products"
        ordering = ["name"]


class Supplier(models.Model):
    id = fields.CharField(max_length=60, pk=True)
    code = fields.CharField(max_length=40, unique=True)
    name = fields.CharField(max_length=180)
    contact_person = fields.CharField(max_length=120, null=True)
    phone = fields.CharField(max_length=40, null=True)

    class Meta:
        table = "suppliers"
        ordering = ["name"]


class Bin(models.Model):
    """A storage location in the godown. `priority` decides dispatch order when more than one bin
    holds the same item — the legacy system's own notion, kept."""
    id = fields.CharField(max_length=60, pk=True)
    rack = fields.CharField(max_length=20)
    bin = fields.CharField(max_length=20)
    priority = fields.IntField(default=1)
    capacity_units = fields.IntField(default=0)

    class Meta:
        table = "bins"
        ordering = ["rack", "bin"]

    @property
    def label(self) -> str:
        return f"Rack {self.rack} · Bin {self.bin}"
