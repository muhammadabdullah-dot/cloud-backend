"""Master data the godown works against: the item list, the vendor list, and the bin map.

These are Cloud-local tables. frontend-baseline.md §4 point 4 flags that a *shared* product
catalog between Branch and Cloud is the real end state — today each service keeps its own, and
cloud-app kept its copy as a hand-maintained TypeScript const. Giving Cloud a real table is the
prerequisite for that eventual share; it is not the share itself.
"""
from tortoise import fields, models


class Product(models.Model):
    """The godown's Item master — the same fields as the branch's Item form (legacy Item screen)."""

    id = fields.CharField(max_length=60, pk=True)
    # Legacy alias code: what people type. One namespace with `barcode` and alternate barcodes.
    sku = fields.CharField(max_length=60, unique=True)
    name = fields.CharField(max_length=200)
    price = fields.DecimalField(max_digits=14, decimal_places=2)
    tax_rate = fields.DecimalField(max_digits=6, decimal_places=2, default=0)
    is_weighed = fields.BooleanField(default=False)
    unit = fields.CharField(max_length=30, default="pc")
    pack_unit = fields.CharField(max_length=30, null=True)
    pack_size = fields.IntField(null=True)

    # Added 2026-09-14: the rest of the legacy Item form.
    barcode = fields.CharField(max_length=60, null=True, unique=True)
    # Weighted-average cost of godown stock, updated by every warehouse GRN; never typed in.
    avg_cost = fields.DecimalField(max_digits=14, decimal_places=4, default=0)
    rpp = fields.DecimalField(max_digits=14, decimal_places=2, null=True)
    department = fields.CharField(max_length=80, null=True)
    category = fields.CharField(max_length=80, null=True)
    item_class = fields.CharField(max_length=80, null=True)
    subclass = fields.CharField(max_length=80, null=True)
    manufacturer = fields.CharField(max_length=120, null=True)
    brand = fields.CharField(max_length=120, null=True)
    active = fields.BooleanField(default=True)
    disc_percent = fields.DecimalField(max_digits=5, decimal_places=2, default=0)
    disc_flat = fields.DecimalField(max_digits=14, decimal_places=2, default=0)
    lock_disc = fields.BooleanField(default=False)
    variant = fields.CharField(max_length=60, null=True)
    # Legacy IMP/LOCAL: "local" or "imported".
    origin = fields.CharField(max_length=10, null=True)
    remarks = fields.CharField(max_length=255, null=True)
    picture = fields.CharField(max_length=160, null=True)
    # Legacy Child And Parent: `parent_qty` of this Item make one `parent`.
    parent: fields.ForeignKeyNullableRelation["Product"] = fields.ForeignKeyField(
        "models.Product", related_name="children", null=True, on_delete=fields.SET_NULL
    )
    parent_qty = fields.DecimalField(max_digits=12, decimal_places=3, null=True)
    # At or below this much in the godown, with nothing on order, the Item shows as running low.
    reorder_level = fields.DecimalField(max_digits=16, decimal_places=3, null=True)
    # Where the Item lives in the godown: receiving puts it here, put-away brings strays back, picking starts here.
    home_bin: fields.ForeignKeyNullableRelation["Bin"] = fields.ForeignKeyField(
        "models.Bin", related_name="home_items", null=True, on_delete=fields.SET_NULL
    )

    class Meta:
        table = "products"
        ordering = ["name"]


class ProductAlias(models.Model):
    """Alternate barcodes — usually other pack sizes of the same Item (a carton barcode is 12 units)."""

    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation[Product] = fields.ForeignKeyField(
        "models.Product", related_name="aliases", on_delete=fields.CASCADE
    )
    code = fields.CharField(max_length=60, unique=True)
    remarks = fields.CharField(max_length=255, null=True)
    qty = fields.DecimalField(max_digits=12, decimal_places=3, default=1)
    disc_percent = fields.DecimalField(max_digits=5, decimal_places=2, default=0)
    disc_flat = fields.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        table = "product_aliases"


class ProductSupplier(models.Model):
    """Who supplies this Item, in order of preference."""

    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation[Product] = fields.ForeignKeyField(
        "models.Product", related_name="supplier_links", on_delete=fields.CASCADE
    )
    supplier: fields.ForeignKeyRelation["Supplier"] = fields.ForeignKeyField(
        "models.Supplier", related_name="product_links", on_delete=fields.CASCADE
    )
    priority = fields.IntField(default=1)

    class Meta:
        table = "product_suppliers"
        unique_together = (("product", "supplier"),)


class ProductPriceChange(models.Model):
    """Every change to an Item's sale or retail price, and where it came from."""

    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation[Product] = fields.ForeignKeyField(
        "models.Product", related_name="price_changes", on_delete=fields.CASCADE
    )
    old_price = fields.DecimalField(max_digits=14, decimal_places=2)
    new_price = fields.DecimalField(max_digits=14, decimal_places=2)
    old_rpp = fields.DecimalField(max_digits=14, decimal_places=2, null=True)
    new_rpp = fields.DecimalField(max_digits=14, decimal_places=2, null=True)
    # "new-item", "form" or "import"
    source = fields.CharField(max_length=20)
    changed_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="price_changes", null=True, on_delete=fields.SET_NULL
    )
    at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "product_price_changes"


class ProductAttachment(models.Model):
    """A file kept with an Item — a spec sheet, a drug registration, a supplier certificate, a saved QR
    code. Stored under the media folder; only the path is in the database, and files are served back
    only to signed-in users."""

    id = fields.UUIDField(pk=True)
    product: fields.ForeignKeyRelation[Product] = fields.ForeignKeyField(
        "models.Product", related_name="attachments", on_delete=fields.CASCADE
    )
    file_name = fields.CharField(max_length=200)
    stored_path = fields.CharField(max_length=200)
    content_type = fields.CharField(max_length=100)
    size_bytes = fields.IntField()
    note = fields.CharField(max_length=255, null=True)
    uploaded_by: fields.ForeignKeyNullableRelation["User"] = fields.ForeignKeyField(
        "models.User", related_name="item_attachments", null=True, on_delete=fields.SET_NULL
    )
    uploaded_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "product_attachments"
        ordering = ["-uploaded_at"]


class Supplier(models.Model):
    id = fields.CharField(max_length=60, pk=True)
    code = fields.CharField(max_length=40, unique=True)
    name = fields.CharField(max_length=180)
    contact_person = fields.CharField(max_length=120, null=True)
    phone = fields.CharField(max_length=40, null=True)

    class Meta:
        table = "suppliers"
        ordering = ["name"]


class Rack(models.Model):
    """A rack in the godown: `levels` shelves high, `positions` bins along each shelf. Its bins are
    generated from that size, so the layout on screen is the layout on the floor."""

    id = fields.UUIDField(pk=True)
    # What's painted on the rack end: "A", "C2", "COLD-1". Bins carry it, so it never changes.
    code = fields.CharField(max_length=10, unique=True)
    name = fields.CharField(max_length=80, null=True)
    # A part of the godown — "Dry goods", "Cold room", "Pharmacy cage" — to filter a large floor by.
    zone = fields.CharField(max_length=60, null=True)
    levels = fields.IntField(default=1)
    positions = fields.IntField(default=1)
    active = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "racks"
        ordering = ["code"]


class Bin(models.Model):
    """A storage location in the godown. `priority` decides dispatch order when more than one bin
    holds the same item — the legacy system's own notion, kept."""
    id = fields.CharField(max_length=60, pk=True)
    rack = fields.CharField(max_length=20)
    bin = fields.CharField(max_length=20)
    priority = fields.IntField(default=1)
    capacity_units = fields.IntField(default=0)
    # Where it sits on its rack: shelf `level` (1 = floor) and `position` along the shelf.
    level = fields.IntField(null=True)
    position = fields.IntField(null=True)
    # A switched-off bin takes no new stock and leaves the pickers; its history stays.
    active = fields.BooleanField(default=True)

    class Meta:
        table = "bins"
        ordering = ["rack", "bin"]

    @property
    def label(self) -> str:
        return f"Rack {self.rack} · Bin {self.bin}"
