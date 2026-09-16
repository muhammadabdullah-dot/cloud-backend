from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "product_aliases" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "code" VARCHAR(60) NOT NULL UNIQUE,
    "remarks" VARCHAR(255),
    "qty" VARCHAR(40) NOT NULL,
    "disc_percent" VARCHAR(40) NOT NULL,
    "disc_flat" VARCHAR(40) NOT NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
) /* Alternate barcodes - usually other pack sizes of the same Item (a carton barcode is 12 units). */;
        CREATE TABLE IF NOT EXISTS "product_attachments" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "file_name" VARCHAR(200) NOT NULL,
    "stored_path" VARCHAR(200) NOT NULL,
    "content_type" VARCHAR(100) NOT NULL,
    "size_bytes" INT NOT NULL,
    "note" VARCHAR(255),
    "uploaded_at" TIMESTAMP NOT NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE,
    "uploaded_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL
) /* A file kept with an Item - a spec sheet, a drug registration, a supplier certificate, a saved QR */;
        CREATE TABLE IF NOT EXISTS "product_price_changes" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "old_price" VARCHAR(40) NOT NULL,
    "new_price" VARCHAR(40) NOT NULL,
    "old_rpp" VARCHAR(40),
    "new_rpp" VARCHAR(40),
    "source" VARCHAR(20) NOT NULL,
    "at" TIMESTAMP NOT NULL,
    "changed_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
) /* Every change to an Item's sale or retail price, and where it came from. */;
        CREATE TABLE IF NOT EXISTS "product_suppliers" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "priority" INT NOT NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE,
    "supplier_id" VARCHAR(60) NOT NULL REFERENCES "suppliers" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_product_sup_product_5546c9" UNIQUE ("product_id", "supplier_id")
) /* Who supplies this Item, in order of preference. */;
        ALTER TABLE "products" ADD "barcode" VARCHAR(60);
        ALTER TABLE "products" ADD "variant" VARCHAR(60);
        ALTER TABLE "products" ADD "picture" VARCHAR(160);
        ALTER TABLE "products" ADD "brand" VARCHAR(120);
        ALTER TABLE "products" ADD "subclass" VARCHAR(80);
        ALTER TABLE "products" ADD "origin" VARCHAR(10);
        ALTER TABLE "products" ADD "disc_flat" VARCHAR(40) NOT NULL DEFAULT 0;
        ALTER TABLE "products" ADD "lock_disc" INT NOT NULL DEFAULT 0;
        ALTER TABLE "products" ADD "avg_cost" VARCHAR(40) NOT NULL DEFAULT 0;
        ALTER TABLE "products" ADD "parent_qty" VARCHAR(40);
        ALTER TABLE "products" ADD "active" INT NOT NULL DEFAULT 1;
        ALTER TABLE "products" ADD "department" VARCHAR(80);
        ALTER TABLE "products" ADD "disc_percent" VARCHAR(40) NOT NULL DEFAULT 0;
        ALTER TABLE "products" ADD "manufacturer" VARCHAR(120);
        ALTER TABLE "products" ADD "item_class" VARCHAR(80);
        ALTER TABLE "products" ADD "rpp" VARCHAR(40);
        ALTER TABLE "products" ADD "remarks" VARCHAR(255);
        ALTER TABLE "products" ADD "category" VARCHAR(80);
        ALTER TABLE "products" ADD "parent_id" VARCHAR(60) REFERENCES "products" ("id") ON DELETE SET NULL;
        CREATE UNIQUE INDEX "uid_products_barcode_d6fd9a" ON "products" ("barcode");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_products_barcode_d6fd9a";
        ALTER TABLE "products" DROP COLUMN "barcode";
        ALTER TABLE "products" DROP COLUMN "variant";
        ALTER TABLE "products" DROP COLUMN "picture";
        ALTER TABLE "products" DROP COLUMN "brand";
        ALTER TABLE "products" DROP COLUMN "subclass";
        ALTER TABLE "products" DROP COLUMN "origin";
        ALTER TABLE "products" DROP COLUMN "disc_flat";
        ALTER TABLE "products" DROP COLUMN "lock_disc";
        ALTER TABLE "products" DROP COLUMN "avg_cost";
        ALTER TABLE "products" DROP COLUMN "parent_qty";
        ALTER TABLE "products" DROP COLUMN "active";
        ALTER TABLE "products" DROP COLUMN "department";
        ALTER TABLE "products" DROP COLUMN "disc_percent";
        ALTER TABLE "products" DROP COLUMN "manufacturer";
        ALTER TABLE "products" DROP COLUMN "item_class";
        ALTER TABLE "products" DROP COLUMN "rpp";
        ALTER TABLE "products" DROP COLUMN "remarks";
        ALTER TABLE "products" DROP COLUMN "category";
        ALTER TABLE "products" DROP COLUMN "parent_id";
        DROP TABLE IF EXISTS "product_suppliers";
        DROP TABLE IF EXISTS "product_attachments";
        DROP TABLE IF EXISTS "product_aliases";
        DROP TABLE IF EXISTS "product_price_changes";"""


MODELS_STATE = (
    "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"
    "rQ1nSvU37M/v3/AZuJFmM="
)
