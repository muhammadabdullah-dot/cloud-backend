from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "bins" (
    "id" VARCHAR(60) NOT NULL PRIMARY KEY,
    "rack" VARCHAR(20) NOT NULL,
    "bin" VARCHAR(20) NOT NULL,
    "priority" INT NOT NULL,
    "capacity_units" INT NOT NULL
) /* A storage location in the godown. `priority` decides dispatch order when more than one bin */;
        CREATE TABLE IF NOT EXISTS "products" (
    "id" VARCHAR(60) NOT NULL PRIMARY KEY,
    "sku" VARCHAR(60) NOT NULL UNIQUE,
    "name" VARCHAR(200) NOT NULL,
    "price" VARCHAR(40) NOT NULL,
    "tax_rate" VARCHAR(40) NOT NULL,
    "is_weighed" INT NOT NULL,
    "unit" VARCHAR(30) NOT NULL,
    "pack_unit" VARCHAR(30),
    "pack_size" INT
);
        CREATE TABLE IF NOT EXISTS "suppliers" (
    "id" VARCHAR(60) NOT NULL PRIMARY KEY,
    "code" VARCHAR(40) NOT NULL UNIQUE,
    "name" VARCHAR(180) NOT NULL,
    "contact_person" VARCHAR(120),
    "phone" VARCHAR(40)
);
        CREATE TABLE IF NOT EXISTS "warehouse_batches" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "lot_number" VARCHAR(60),
    "expiry" TIMESTAMP,
    "received_qty" VARCHAR(40) NOT NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "cycle_counts" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "system_qty" VARCHAR(40) NOT NULL,
    "counted_qty" VARCHAR(40) NOT NULL,
    "status" VARCHAR(20) NOT NULL,
    "at" TIMESTAMP NOT NULL,
    "approved_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "bin_id" VARCHAR(60) NOT NULL REFERENCES "bins" ("id") ON DELETE CASCADE,
    "counted_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
) /* Counting one bin's stock of one item against what the ledger says. Same submit-then-approve */;
        CREATE TABLE IF NOT EXISTS "warehouse_grns" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "grn_number" VARCHAR(30) NOT NULL UNIQUE,
    "party_inv_no" VARCHAR(60),
    "gst_mode" VARCHAR(20) NOT NULL,
    "advance_tax" VARCHAR(40) NOT NULL,
    "approved" INT NOT NULL,
    "at" TIMESTAMP NOT NULL,
    "bin_id" VARCHAR(60) NOT NULL REFERENCES "bins" ("id") ON DELETE CASCADE,
    "received_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "supplier_id" VARCHAR(60) NOT NULL REFERENCES "suppliers" ("id") ON DELETE CASCADE
) /* Goods Receipt Note — supplier stock arriving into the godown. Numbered WGRN-nnnn to keep it */;
        CREATE TABLE IF NOT EXISTS "warehouse_grn_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "qty" VARCHAR(40) NOT NULL,
    "bonus_qty" VARCHAR(40) NOT NULL,
    "unit_price" VARCHAR(40) NOT NULL,
    "disc_percent" VARCHAR(40) NOT NULL,
    "expiry" TIMESTAMP,
    "tax_rate" VARCHAR(40) NOT NULL,
    "grn_id" CHAR(36) NOT NULL REFERENCES "warehouse_grns" ("id") ON DELETE CASCADE,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "requisitions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "requisition_number" VARCHAR(30) NOT NULL UNIQUE,
    "qty_requested" VARCHAR(40) NOT NULL,
    "status" VARCHAR(20) NOT NULL,
    "requested_at" TIMESTAMP NOT NULL,
    "decided_at" TIMESTAMP,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    "decided_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
) /* A branch asking the godown for stock. Approving one creates a Transfer — nothing re-typed. */;
        CREATE TABLE IF NOT EXISTS "warehouse_stock_movements" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "kind" VARCHAR(30) NOT NULL,
    "qty" VARCHAR(40) NOT NULL,
    "reason" VARCHAR(200),
    "at" TIMESTAMP NOT NULL,
    "bin_id" VARCHAR(60) NOT NULL REFERENCES "bins" ("id") ON DELETE CASCADE,
    "origin_user_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE
) /* One line of the godown ledger. Signed: positive is stock in, negative is stock out. */;
        CREATE TABLE IF NOT EXISTS "transfers" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "transfer_number" VARCHAR(30) NOT NULL UNIQUE,
    "status" VARCHAR(20) NOT NULL,
    "vehicle" VARCHAR(40),
    "driver" VARCHAR(120),
    "requested_at" TIMESTAMP NOT NULL,
    "approved_at" TIMESTAMP,
    "dispatched_at" TIMESTAMP,
    "received_at" TIMESTAMP,
    "dispute_open" INT NOT NULL,
    "dispute_note" VARCHAR(255),
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    "requisition_id" CHAR(36) REFERENCES "requisitions" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "transfer_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "qty_sent" VARCHAR(40) NOT NULL,
    "qty_received" VARCHAR(40),
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE,
    "transfer_id" CHAR(36) NOT NULL REFERENCES "transfers" ("id") ON DELETE CASCADE
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "products";
        DROP TABLE IF EXISTS "transfers";
        DROP TABLE IF EXISTS "requisitions";
        DROP TABLE IF EXISTS "warehouse_grn_lines";
        DROP TABLE IF EXISTS "warehouse_batches";
        DROP TABLE IF EXISTS "transfer_lines";
        DROP TABLE IF EXISTS "warehouse_stock_movements";
        DROP TABLE IF EXISTS "warehouse_grns";
        DROP TABLE IF EXISTS "cycle_counts";
        DROP TABLE IF EXISTS "suppliers";
        DROP TABLE IF EXISTS "bins";"""


MODELS_STATE = (
    "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"
    "9P/puH++zT1Wne9ZIvONVLENn8Yvb3/wHjRu76"
)
