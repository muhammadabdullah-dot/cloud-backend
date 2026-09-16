from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "notices" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "at" TIMESTAMP NOT NULL,
    "kind" VARCHAR(40) NOT NULL,
    "title" VARCHAR(200) NOT NULL,
    "body" VARCHAR(500),
    "link" VARCHAR(200),
    "tone" VARCHAR(10) NOT NULL,
    "subject_type" VARCHAR(40),
    "subject_id" VARCHAR(60),
    "audience" JSON NOT NULL
);
CREATE INDEX IF NOT EXISTS "idx_notices_at_428bef" ON "notices" ("at");
        CREATE TABLE IF NOT EXISTS "notice_reads" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "read_at" TIMESTAMP NOT NULL,
    "notice_id" CHAR(36) NOT NULL REFERENCES "notices" ("id") ON DELETE CASCADE,
    "user_id" CHAR(36) NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_notice_read_notice__40ddd6" UNIQUE ("notice_id", "user_id")
);
        CREATE TABLE IF NOT EXISTS "warehouse_purchase_orders" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "po_number" VARCHAR(20) NOT NULL UNIQUE,
    "status" VARCHAR(20) NOT NULL,
    "reason" VARCHAR(120),
    "expected_at" TIMESTAMP,
    "notes" VARCHAR(255),
    "total" VARCHAR(40) NOT NULL,
    "raised_at" TIMESTAMP NOT NULL,
    "submitted_at" TIMESTAMP,
    "approved_at" TIMESTAMP,
    "auto_approved" INT NOT NULL,
    "decision_note" VARCHAR(255),
    "closed_at" TIMESTAMP,
    "approved_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL,
    "raised_by_id" CHAR(36) NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "supplier_id" VARCHAR(60) NOT NULL REFERENCES "suppliers" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "warehouse_purchase_order_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "qty" VARCHAR(40) NOT NULL,
    "unit_cost" VARCHAR(40) NOT NULL,
    "received_qty" VARCHAR(40) NOT NULL,
    "position" INT NOT NULL,
    "product_id" VARCHAR(60) NOT NULL REFERENCES "products" ("id") ON DELETE CASCADE,
    "purchase_order_id" CHAR(36) NOT NULL REFERENCES "warehouse_purchase_orders" ("id") ON DELETE CASCADE
);
        ALTER TABLE "warehouse_grns" ADD "purchase_order_id" CHAR(36) REFERENCES "warehouse_purchase_orders" ("id") ON DELETE SET NULL;
        ALTER TABLE "products" ADD "reorder_level" VARCHAR(40);
        ALTER TABLE "transfers" ADD "override_by_name" VARCHAR(120);
        ALTER TABLE "transfers" ADD "ack_at" TIMESTAMP;
        ALTER TABLE "transfers" ADD "override_reason" VARCHAR(255);
        ALTER TABLE "transfers" ADD "ack_requested_at" TIMESTAMP;
        ALTER TABLE "transfers" ADD "override_at" TIMESTAMP;
        ALTER TABLE "transfers" ADD "ack_note" VARCHAR(255);
        ALTER TABLE "transfers" ADD "ack_status" VARCHAR(12);
        ALTER TABLE "transfers" ADD "cancelled_at" TIMESTAMP;
        ALTER TABLE "transfers" ADD "ack_by_name" VARCHAR(120);
        ALTER TABLE "users" ADD "po_limit" VARCHAR(40);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "warehouse_grns" DROP COLUMN "purchase_order_id";
        ALTER TABLE "products" DROP COLUMN "reorder_level";
        ALTER TABLE "transfers" DROP COLUMN "override_by_name";
        ALTER TABLE "transfers" DROP COLUMN "ack_at";
        ALTER TABLE "transfers" DROP COLUMN "override_reason";
        ALTER TABLE "transfers" DROP COLUMN "ack_requested_at";
        ALTER TABLE "transfers" DROP COLUMN "override_at";
        ALTER TABLE "transfers" DROP COLUMN "ack_note";
        ALTER TABLE "transfers" DROP COLUMN "ack_status";
        ALTER TABLE "transfers" DROP COLUMN "cancelled_at";
        ALTER TABLE "transfers" DROP COLUMN "ack_by_name";
        ALTER TABLE "users" DROP COLUMN "po_limit";
        DROP TABLE IF EXISTS "warehouse_purchase_orders";
        DROP TABLE IF EXISTS "notice_reads";
        DROP TABLE IF EXISTS "notices";
        DROP TABLE IF EXISTS "warehouse_purchase_order_lines";"""


MODELS_STATE = (
    "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"
)
