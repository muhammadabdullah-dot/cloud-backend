# Campaigns at head office, and what each one gave away at each branch.
#
# One campaign is one decision for the whole company. That is not a preference: the old software's campaign tables
# have no branch column at all, seven people wrote all 11,364 of them, and every one of its five shops sold under
# them. Its shops share one database so one list reaches every till by itself; ours sends them down as a
# `promotion.upsert` message, the way a supplier already travels.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "promotions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "code" VARCHAR(20) NOT NULL UNIQUE,
    "name" VARCHAR(160) NOT NULL,
    "product_sku" VARCHAR(40) NOT NULL,
    "product_name" VARCHAR(200),
    "starts_on" DATE NOT NULL,
    "ends_on" DATE NOT NULL,
    "kind" VARCHAR(10) NOT NULL DEFAULT 'percent',
    "disc_percent" VARCHAR(40) NOT NULL DEFAULT 0,
    "disc_flat" VARCHAR(40) NOT NULL DEFAULT 0,
    "promo_price" VARCHAR(40),
    "min_qty" VARCHAR(40) NOT NULL DEFAULT 1,
    "bonus_qty" VARCHAR(40) NOT NULL DEFAULT 0,
    "qty_limit" VARCHAR(40),
    "amount_limit" VARCHAR(40),
    "state" VARCHAR(10) NOT NULL DEFAULT 'draft',
    "remarks" VARCHAR(255),
    "rev" INT NOT NULL DEFAULT 0,
    "created_by_name" VARCHAR(120),
    "approved_by_name" VARCHAR(120),
    "approved_at" TIMESTAMP,
    "published_at" TIMESTAMP,
    "stopped_by_name" VARCHAR(120),
    "stopped_at" TIMESTAMP,
    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
        CREATE INDEX IF NOT EXISTS "idx_promotions_state_days" ON "promotions" ("state", "starts_on", "ends_on");
        CREATE TABLE IF NOT EXISTS "branch_promotion_stats" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "day" DATE NOT NULL,
    "promotion_code" VARCHAR(20) NOT NULL,
    "promotion_name" VARCHAR(160),
    "product_sku" VARCHAR(40),
    "lines" INT NOT NULL DEFAULT 0,
    "qty" VARCHAR(40) NOT NULL DEFAULT 0,
    "given" VARCHAR(40) NOT NULL DEFAULT 0,
    "net_sales" VARCHAR(40) NOT NULL DEFAULT 0,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_promotion_stats" UNIQUE ("branch_id", "day", "promotion_code")
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branch_promotion_stats";
        DROP TABLE IF EXISTS "promotions";"""
