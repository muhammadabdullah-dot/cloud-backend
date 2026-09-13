from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branch_product_stock" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "snapshot_id" VARCHAR(60) NOT NULL,
    "product_sku" VARCHAR(60) NOT NULL,
    "product_name" VARCHAR(200) NOT NULL,
    "department" VARCHAR(120),
    "category" VARCHAR(120),
    "brand" VARCHAR(120),
    "qty" VARCHAR(40) NOT NULL,
    "avg_cost" VARCHAR(40) NOT NULL,
    "price" VARCHAR(40) NOT NULL,
    "last_sold_at" TIMESTAMP,
    "last_received_at" TIMESTAMP,
    "units_sold_period" VARCHAR(40) NOT NULL,
    "days_with_sales" INT NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_prod_branch__de043b" UNIQUE ("branch_id", "snapshot_id", "product_sku")
);
CREATE INDEX IF NOT EXISTS "idx_branch_prod_branch__db7bab" ON "branch_product_stock" ("branch_id", "snapshot_id");
        CREATE TABLE IF NOT EXISTS "branch_snapshot_runs" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "snapshot_id" VARCHAR(60) NOT NULL,
    "started_at" TIMESTAMP NOT NULL,
    "completed_at" TIMESTAMP,
    "stock_rows" INT NOT NULL,
    "trading_days" INT NOT NULL,
    "product_days" INT NOT NULL,
    "status" VARCHAR(20) NOT NULL,
    "source" VARCHAR(20) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* One complete picture-taking. Kept so "how fresh is this stock list" has an answer that does */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branch_product_stock";
        DROP TABLE IF EXISTS "branch_snapshot_runs";"""


MODELS_STATE = (
    "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"
    "T5j9mf/w+kbRP3"
)
