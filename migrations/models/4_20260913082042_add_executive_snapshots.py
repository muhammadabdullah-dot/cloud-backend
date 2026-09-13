from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branch_cashier_stats" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "cashier_name" VARCHAR(140) NOT NULL,
    "invoices" INT NOT NULL,
    "net_sales" VARCHAR(40) NOT NULL,
    "till_variance" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_cash_branch__7095e0" UNIQUE ("branch_id", "day", "cashier_name")
) /* Per cashier per day — what "Best Cashier" is ranked from, and what makes the ranking */;
        CREATE TABLE IF NOT EXISTS "branch_daily_stats" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "gross_sales" VARCHAR(40) NOT NULL,
    "disc_total" VARCHAR(40) NOT NULL,
    "gst" VARCHAR(40) NOT NULL,
    "net_sales" VARCHAR(40) NOT NULL,
    "invoices" INT NOT NULL,
    "items_sold" VARCHAR(40) NOT NULL,
    "cogs" VARCHAR(40) NOT NULL,
    "returns_value" VARCHAR(40) NOT NULL,
    "returns_count" INT NOT NULL,
    "cash_collected" VARCHAR(40) NOT NULL,
    "credit_sales" VARCHAR(40) NOT NULL,
    "cash_in" VARCHAR(40) NOT NULL,
    "cash_out" VARCHAR(40) NOT NULL,
    "till_variance" VARCHAR(40) NOT NULL,
    "tills_closed" INT NOT NULL,
    "staff_on_duty" INT NOT NULL,
    "first_sale_at" TIMESTAMP,
    "last_sale_at" TIMESTAMP,
    "named_customers" INT NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_dail_branch__753578" UNIQUE ("branch_id", "day")
);
        CREATE TABLE IF NOT EXISTS "branch_product_stats" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "product_sku" VARCHAR(60) NOT NULL,
    "product_name" VARCHAR(200) NOT NULL,
    "department" VARCHAR(120),
    "category" VARCHAR(120),
    "brand" VARCHAR(120),
    "qty" VARCHAR(40) NOT NULL,
    "net_sales" VARCHAR(40) NOT NULL,
    "cogs" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_prod_branch__883570" UNIQUE ("branch_id", "day", "product_sku")
) /* Per product per day. Top Products, Top Categories and Top Brands are all folds over this */;
        CREATE TABLE IF NOT EXISTS "branch_stock_alerts" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "kind" VARCHAR(20) NOT NULL,
    "product_sku" VARCHAR(60) NOT NULL,
    "product_name" VARCHAR(200) NOT NULL,
    "qty" VARCHAR(40) NOT NULL,
    "expiry" TIMESTAMP,
    "detail" VARCHAR(200),
    "total_of_kind" INT NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* Current stock exceptions at a branch — out of stock, running low, near expiry, expired. */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branch_cashier_stats";
        DROP TABLE IF EXISTS "branch_daily_stats";
        DROP TABLE IF EXISTS "branch_stock_alerts";
        DROP TABLE IF EXISTS "branch_product_stats";"""


MODELS_STATE = (
    "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"
    "wa"
)
