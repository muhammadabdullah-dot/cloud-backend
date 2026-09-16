from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "acc_types" (
    "code" VARCHAR(2) NOT NULL PRIMARY KEY,
    "name" VARCHAR(60) NOT NULL,
    "nature" VARCHAR(6) NOT NULL,
    "statement" VARCHAR(10) NOT NULL
);
        CREATE TABLE IF NOT EXISTS "acc_settings" (
    "book" VARCHAR(20) NOT NULL PRIMARY KEY,
    "fiscal_start_month" INT NOT NULL,
    "books_start" DATE,
    "locked_until" DATE,
    "tender_accounts" JSON NOT NULL,
    "last_posting_at" TIMESTAMP,
    "last_posting_note" VARCHAR(255),
    "posting_problems" JSON NOT NULL,
    "last_received_at" TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL
);
        CREATE TABLE IF NOT EXISTS "acc_categories" (
    "code" VARCHAR(4) NOT NULL PRIMARY KEY,
    "name" VARCHAR(80) NOT NULL,
    "type_id" VARCHAR(2) NOT NULL REFERENCES "acc_types" ("code") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "acc_groups" (
    "id" VARCHAR(30) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "code" VARCHAR(6) NOT NULL,
    "name" VARCHAR(100) NOT NULL,
    "priority" INT NOT NULL,
    "manual_code" VARCHAR(30),
    "standard" INT NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "category_id" VARCHAR(4) NOT NULL REFERENCES "acc_categories" ("code") ON DELETE CASCADE,
    CONSTRAINT "uid_acc_groups_book_4f8af8" UNIQUE ("book", "code")
);
        CREATE TABLE IF NOT EXISTS "acc_sub_groups" (
    "id" VARCHAR(30) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "code" VARCHAR(8) NOT NULL,
    "name" VARCHAR(100) NOT NULL,
    "standard" INT NOT NULL,
    "group_id" VARCHAR(30) NOT NULL REFERENCES "acc_groups" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_acc_sub_gro_book_234617" UNIQUE ("book", "code")
);
        CREATE TABLE IF NOT EXISTS "acc_accounts" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "code" VARCHAR(12) NOT NULL,
    "name" VARCHAR(160) NOT NULL,
    "kind" VARCHAR(12) NOT NULL,
    "system_key" VARCHAR(80),
    "party_ref" VARCHAR(60),
    "active" INT NOT NULL,
    "restricted" INT NOT NULL,
    "check_limit" INT NOT NULL,
    "balance_limit" VARCHAR(40),
    "bank_name" VARCHAR(80),
    "bank_account_no" VARCHAR(40),
    "manual_code" VARCHAR(30),
    "remarks" VARCHAR(255),
    "standard" INT NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "group_id" VARCHAR(30) NOT NULL REFERENCES "acc_groups" ("id") ON DELETE CASCADE,
    "sub_group_id" VARCHAR(30) REFERENCES "acc_sub_groups" ("id") ON DELETE SET NULL,
    CONSTRAINT "uid_acc_account_book_92e4ad" UNIQUE ("book", "code"),
    CONSTRAINT "uid_acc_account_book_ffa0c2" UNIQUE ("book", "system_key")
);
        CREATE TABLE IF NOT EXISTS "acc_vouchers" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "number" VARCHAR(30) NOT NULL UNIQUE,
    "vtype" VARCHAR(4) NOT NULL,
    "date" DATE NOT NULL,
    "status" VARCHAR(10) NOT NULL,
    "auto" INT NOT NULL,
    "source" VARCHAR(160) UNIQUE,
    "source_hash" VARCHAR(64),
    "reference_no" VARCHAR(60),
    "description" VARCHAR(500),
    "cheque_no" VARCHAR(30),
    "cheque_date" DATE,
    "total" VARCHAR(40) NOT NULL,
    "created_by_name" VARCHAR(120),
    "posted_by_name" VARCHAR(120),
    "posted_at" TIMESTAMP,
    "cancelled_by_name" VARCHAR(120),
    "cancelled_at" TIMESTAMP,
    "cancel_reason" VARCHAR(255),
    "reversal_of_id" VARCHAR(36),
    "reversed" INT NOT NULL,
    "version" INT NOT NULL,
    "mirrored" INT NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "created_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL,
    "header_account_id" CHAR(36) REFERENCES "acc_accounts" ("id") ON DELETE SET NULL,
    "posted_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS "idx_acc_voucher_book_f79050" ON "acc_vouchers" ("book", "date", "status");
        CREATE TABLE IF NOT EXISTS "acc_voucher_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "line_no" INT NOT NULL,
    "debit" VARCHAR(40) NOT NULL,
    "credit" VARCHAR(40) NOT NULL,
    "description" VARCHAR(255),
    "reference_no" VARCHAR(60),
    "account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
    "voucher_id" CHAR(36) NOT NULL REFERENCES "acc_vouchers" ("id") ON DELETE CASCADE
);
        ALTER TABLE "warehouse_stock_movements" ADD "unit_cost" VARCHAR(40);
        ALTER TABLE "transfer_lines" ADD "unit_cost" VARCHAR(40);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "warehouse_stock_movements" DROP COLUMN "unit_cost";
        ALTER TABLE "transfer_lines" DROP COLUMN "unit_cost";
        DROP TABLE IF EXISTS "acc_types";
        DROP TABLE IF EXISTS "acc_voucher_lines";
        DROP TABLE IF EXISTS "acc_settings";
        DROP TABLE IF EXISTS "acc_sub_groups";
        DROP TABLE IF EXISTS "acc_categories";
        DROP TABLE IF EXISTS "acc_accounts";
        DROP TABLE IF EXISTS "acc_groups";
        DROP TABLE IF EXISTS "acc_vouchers";"""


MODELS_STATE = (
    "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"
    "6ruw=="
)
