from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branch_credit_customers" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "code" VARCHAR(40) NOT NULL,
    "name" VARCHAR(180) NOT NULL,
    "phone" VARCHAR(40),
    "tier" VARCHAR(20),
    "credit_limit" VARCHAR(40) NOT NULL,
    "credit_balance" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* Point-in-time, not daily: the question is "who owes us how much right now", and a row per */;
        CREATE TABLE IF NOT EXISTS "branch_discount_overrides" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "invoice_number" VARCHAR(40) NOT NULL,
    "at" TIMESTAMP,
    "cashier_name" VARCHAR(140),
    "approved_by" VARCHAR(140),
    "gross" VARCHAR(40) NOT NULL,
    "disc_total" VARCHAR(40) NOT NULL,
    "net_value" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* A discount above a cashier's own authority, and the manager who approved it. Margin given */;
        CREATE TABLE IF NOT EXISTS "branch_hourly_stats" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "hour" INT NOT NULL,
    "invoices" INT NOT NULL,
    "net_sales" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_hour_branch__855da8" UNIQUE ("branch_id", "day", "hour")
) /* Invoices and sales by hour of the local trading day. */;
        CREATE TABLE IF NOT EXISTS "branch_returns" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "against_invoice" VARCHAR(40),
    "at" TIMESTAMP,
    "cashier_name" VARCHAR(140),
    "refund_total" VARCHAR(40) NOT NULL,
    "product_name" VARCHAR(200),
    "product_sku" VARCHAR(60),
    "qty" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "branch_tender_stats" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "code" VARCHAR(40) NOT NULL,
    "name" VARCHAR(80) NOT NULL,
    "uses" INT NOT NULL,
    "amount" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_tend_branch__ae0901" UNIQUE ("branch_id", "day", "code")
) /* How customers actually paid. Cash, card and credit have completely different consequences */;
        CREATE TABLE IF NOT EXISTS "branch_till_closes" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "day" DATE NOT NULL,
    "session_number" VARCHAR(40) NOT NULL,
    "cashier_name" VARCHAR(140) NOT NULL,
    "opened_at" TIMESTAMP,
    "closed_at" TIMESTAMP,
    "opening_float" VARCHAR(40) NOT NULL,
    "net_cash" VARCHAR(40) NOT NULL,
    "counted_cash" VARCHAR(40) NOT NULL,
    "variance" VARCHAR(40) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* One drawer, one shift, one variance — the row a Till Variance figure is made of. */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branch_till_closes";
        DROP TABLE IF EXISTS "branch_discount_overrides";
        DROP TABLE IF EXISTS "branch_hourly_stats";
        DROP TABLE IF EXISTS "branch_credit_customers";
        DROP TABLE IF EXISTS "branch_tender_stats";
        DROP TABLE IF EXISTS "branch_returns";"""


MODELS_STATE = (
    "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"
)
