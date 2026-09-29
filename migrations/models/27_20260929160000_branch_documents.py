# The documents themselves at head office: a bill, its lines, how it was paid, what came back, and what FBR was told.
#
# Everything head office kept about a branch until now was a figure. Figures answer "how much" and cannot answer
# "show me that bill", which is what a company needs to settle an argument with a customer, audit a cashier, or carry
# its own history across a change of software.
#
# Two things write here and the shape suits both: a live branch an event at a time, and the legacy pipeline in bulk a
# window of days at a time. So every table is keyed on the branch and the branch's own document number, and writing
# the same document twice updates rather than doubles.
#
# Checked against the twenty branch tables head office already has, because two of them are close enough to matter:
#   * `branch_returns` is a day-grouped figure, one row per returned line per day, and keeps its name. The document
#     is `branch_return_records`.
#   * `branch_till_closes` already holds a till session at document grain, so none is created here. It is fed by the
#     figures, and a document living in a table that a window replace empties would not survive a reload.
#
# Every table carries `day` as well as `at`, because every other table here is grouped by the branch's own trading
# day, which is not the calendar day where a shop sells past midnight.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branch_sales" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "invoice_number" VARCHAR(30) NOT NULL,
    "at" TIMESTAMP NOT NULL,
    "day" DATE NOT NULL,
    "cashier_name" VARCHAR(120),
    "party_name" VARCHAR(160),
    "party_code" VARCHAR(40),
    "till_session_number" VARCHAR(20),
    "counter_name" VARCHAR(80),
    "gross" VARCHAR(40) NOT NULL DEFAULT 0,
    "disc_total" VARCHAR(40) NOT NULL DEFAULT 0,
    "fare" VARCHAR(40) NOT NULL DEFAULT 0,
    "gst" VARCHAR(40) NOT NULL DEFAULT 0,
    "grand_total" VARCHAR(40) NOT NULL DEFAULT 0,
    "net_value" VARCHAR(40) NOT NULL DEFAULT 0,
    "received" VARCHAR(40) NOT NULL DEFAULT 0,
    "cash_back" VARCHAR(40) NOT NULL DEFAULT 0,
    "cogs" VARCHAR(40) NOT NULL DEFAULT 0,
    "is_credit_sale" INT NOT NULL DEFAULT 0,
    "voided" INT NOT NULL DEFAULT 0,
    "fbr_invoice_number" VARCHAR(60),
    "member_code" VARCHAR(40),
    "earned_points" INT NOT NULL DEFAULT 0,
    "source" VARCHAR(10) NOT NULL DEFAULT 'branch',
    "received_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_sales_branch_invoice" UNIQUE ("branch_id", "invoice_number")
);
        CREATE INDEX IF NOT EXISTS "idx_branch_sales_day" ON "branch_sales" ("branch_id", "day");
        CREATE INDEX IF NOT EXISTS "idx_branch_sales_at" ON "branch_sales" ("at");
        CREATE TABLE IF NOT EXISTS "branch_sale_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "line_no" INT NOT NULL DEFAULT 0,
    "product_sku" VARCHAR(40) NOT NULL,
    "product_name" VARCHAR(200),
    "department" VARCHAR(80),
    "qty" VARCHAR(40) NOT NULL DEFAULT 0,
    "unit_price" VARCHAR(40) NOT NULL DEFAULT 0,
    "disc_amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "unit_cost" VARCHAR(40) NOT NULL DEFAULT 0,
    "is_return" INT NOT NULL DEFAULT 0,
    "alias_code" VARCHAR(60),
    "promotion_code" VARCHAR(20),
    "sell_level" VARCHAR(20),
    "sale_id" CHAR(36) NOT NULL REFERENCES "branch_sales" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_sale_lines_sale_line" UNIQUE ("sale_id", "line_no")
);
        CREATE INDEX IF NOT EXISTS "idx_branch_sale_lines_sku" ON "branch_sale_lines" ("product_sku");
        CREATE INDEX IF NOT EXISTS "idx_branch_sale_lines_promo" ON "branch_sale_lines" ("promotion_code");
        CREATE TABLE IF NOT EXISTS "branch_sale_tenders" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "code" VARCHAR(20) NOT NULL,
    "name" VARCHAR(80),
    "amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "reference" VARCHAR(120),
    "sale_id" CHAR(36) NOT NULL REFERENCES "branch_sales" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_sale_tenders_sale_code" UNIQUE ("sale_id", "code")
);
        CREATE TABLE IF NOT EXISTS "branch_return_records" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "number" VARCHAR(30) NOT NULL,
    "at" TIMESTAMP NOT NULL,
    "day" DATE NOT NULL,
    "against_invoice" VARCHAR(30),
    "cashier_name" VARCHAR(120),
    "refund_total" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_total" VARCHAR(40) NOT NULL DEFAULT 0,
    "refund_method" VARCHAR(20),
    "reason" VARCHAR(160),
    "note" VARCHAR(255),
    "till_session_number" VARCHAR(20),
    "source" VARCHAR(10) NOT NULL DEFAULT 'branch',
    "received_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_return_records_branch_number" UNIQUE ("branch_id", "number")
);
        CREATE INDEX IF NOT EXISTS "idx_branch_return_records_day" ON "branch_return_records" ("branch_id", "day");
        CREATE INDEX IF NOT EXISTS "idx_branch_return_records_against" ON "branch_return_records" ("against_invoice");
        CREATE TABLE IF NOT EXISTS "branch_return_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "line_no" INT NOT NULL DEFAULT 0,
    "product_sku" VARCHAR(40) NOT NULL,
    "product_name" VARCHAR(200),
    "qty" VARCHAR(40) NOT NULL DEFAULT 0,
    "unit_price" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "unit_cost" VARCHAR(40) NOT NULL DEFAULT 0,
    "return_record_id" CHAR(36) NOT NULL REFERENCES "branch_return_records" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_return_lines_rec_line" UNIQUE ("return_record_id", "line_no")
);
        CREATE TABLE IF NOT EXISTS "branch_fbr_invoices" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "invoice_number" VARCHAR(30) NOT NULL,
    "kind" VARCHAR(10) NOT NULL DEFAULT 'sale',
    "at" TIMESTAMP NOT NULL,
    "day" DATE NOT NULL,
    "fbr_invoice_number" VARCHAR(60),
    "status" VARCHAR(20) NOT NULL DEFAULT 'sent',
    "pos_id" VARCHAR(40),
    "total" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_total" VARCHAR(40) NOT NULL DEFAULT 0,
    "message" VARCHAR(255),
    "buyer_name" VARCHAR(160),
    "buyer_ntn" VARCHAR(40),
    "buyer_cnic" VARCHAR(40),
    "source" VARCHAR(10) NOT NULL DEFAULT 'branch',
    "received_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_fbr_invoices_key" UNIQUE ("branch_id", "invoice_number", "kind")
);
        CREATE INDEX IF NOT EXISTS "idx_branch_fbr_invoices_day" ON "branch_fbr_invoices" ("branch_id", "day");
        CREATE INDEX IF NOT EXISTS "idx_branch_fbr_invoices_number" ON "branch_fbr_invoices" ("fbr_invoice_number");
        CREATE TABLE IF NOT EXISTS "branch_fbr_invoice_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "line_no" INT NOT NULL DEFAULT 0,
    "product_sku" VARCHAR(40),
    "product_name" VARCHAR(200),
    "hs_code" VARCHAR(40),
    "qty" VARCHAR(40) NOT NULL DEFAULT 0,
    "unit_price" VARCHAR(40) NOT NULL DEFAULT 0,
    "disc_amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_rate" VARCHAR(40) NOT NULL DEFAULT 0,
    "tax_amount" VARCHAR(40) NOT NULL DEFAULT 0,
    "total" VARCHAR(40) NOT NULL DEFAULT 0,
    "invoice_id" CHAR(36) NOT NULL REFERENCES "branch_fbr_invoices" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_fbr_lines_inv_line" UNIQUE ("invoice_id", "line_no")
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branch_fbr_invoice_lines";
        DROP TABLE IF EXISTS "branch_fbr_invoices";
        DROP TABLE IF EXISTS "branch_return_lines";
        DROP TABLE IF EXISTS "branch_return_records";
        DROP TABLE IF EXISTS "branch_sale_tenders";
        DROP TABLE IF EXISTS "branch_sale_lines";
        DROP TABLE IF EXISTS "branch_sales";"""
