from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "acc_depreciation_runs" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "month" DATE NOT NULL,
    "status" VARCHAR(10) NOT NULL,
    "total" VARCHAR(40) NOT NULL,
    "created_by_name" VARCHAR(120),
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "created_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE SET NULL,
    "voucher_id" CHAR(36) REFERENCES "acc_vouchers" ("id") ON DELETE SET NULL
) /* One month's depreciation, prepared as a draft journal. A month is taken while its voucher is a draft or posted. */;
CREATE INDEX IF NOT EXISTS "idx_acc_depreci_book_7bf367" ON "acc_depreciation_runs" ("book", "month", "status");
        CREATE TABLE IF NOT EXISTS "acc_fixed_assets" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "book" VARCHAR(20) NOT NULL,
    "code" VARCHAR(20) NOT NULL,
    "name" VARCHAR(160) NOT NULL,
    "purchase_date" DATE NOT NULL,
    "cost" VARCHAR(40) NOT NULL,
    "salvage_value" VARCHAR(40) NOT NULL,
    "useful_life_months" INT NOT NULL,
    "method" VARCHAR(10) NOT NULL,
    "rate_percent" VARCHAR(40),
    "depreciation_start" DATE NOT NULL,
    "opening_accumulated" VARCHAR(40) NOT NULL,
    "location" VARCHAR(120),
    "supplier_ref" VARCHAR(120),
    "notes" VARCHAR(500),
    "status" VARCHAR(10) NOT NULL,
    "disposal_date" DATE,
    "disposal_proceeds" VARCHAR(40),
    "created_by_name" VARCHAR(120),
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "accumulated_account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
    "category_account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
    "disposal_account_id" CHAR(36) REFERENCES "acc_accounts" ("id") ON DELETE SET NULL,
    "disposal_voucher_id" CHAR(36) REFERENCES "acc_vouchers" ("id") ON DELETE SET NULL,
    "expense_account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
    CONSTRAINT "uid_acc_fixed_a_book_00f994" UNIQUE ("book", "code")
);
        CREATE TABLE IF NOT EXISTS "acc_depreciation_run_lines" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "amount" VARCHAR(40) NOT NULL,
    "accumulated_after" VARCHAR(40) NOT NULL,
    "asset_id" CHAR(36) NOT NULL REFERENCES "acc_fixed_assets" ("id") ON DELETE RESTRICT,
    "run_id" CHAR(36) NOT NULL REFERENCES "acc_depreciation_runs" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "supplier_branch_links" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "branch_supplier_id" VARCHAR(60) NOT NULL,
    "how" VARCHAR(20) NOT NULL,
    "linked_at" TIMESTAMP NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    "supplier_id" VARCHAR(60) NOT NULL REFERENCES "suppliers" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_supplier_br_branch__8e3f7b" UNIQUE ("branch_id", "branch_supplier_id")
);
        CREATE TABLE IF NOT EXISTS "supplier_questions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "branch_supplier_id" VARCHAR(60) NOT NULL,
    "branch_supplier" JSON NOT NULL,
    "candidates" JSON NOT NULL,
    "held" JSON NOT NULL,
    "reason" VARCHAR(255) NOT NULL,
    "status" VARCHAR(12) NOT NULL,
    "decided_by" VARCHAR(180),
    "decided_at" TIMESTAMP,
    "created_at" TIMESTAMP NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "answer_supplier_id" VARCHAR(60) REFERENCES "suppliers" ("id") ON DELETE SET NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_supplier_qu_branch__e1c4a7" UNIQUE ("branch_id", "branch_supplier_id")
);
        ALTER TABLE "suppliers" ADD "due_days" INT NOT NULL DEFAULT 0;
        ALTER TABLE "suppliers" ADD "updated_by" VARCHAR(180);
        ALTER TABLE "suppliers" ADD "rev" INT NOT NULL DEFAULT 1;
        ALTER TABLE "suppliers" ADD "city" VARCHAR(80);
        ALTER TABLE "suppliers" ADD "address" VARCHAR(255);
        ALTER TABLE "suppliers" ADD "updated_at" TIMESTAMP;
        ALTER TABLE "suppliers" ADD "remarks" VARCHAR(255);
        ALTER TABLE "suppliers" ADD "active" INT NOT NULL DEFAULT 1;
        ALTER TABLE "suppliers" ADD "company_id" VARCHAR(40);
        ALTER TABLE "suppliers" ADD "email" VARCHAR(180);
        ALTER TABLE "suppliers" ADD "ntn" VARCHAR(40);
        ALTER TABLE "suppliers" ADD "created_at" TIMESTAMP;
        ALTER TABLE "suppliers" ADD "origin" VARCHAR(20) NOT NULL DEFAULT 'HO';
        ALTER TABLE "suppliers" ADD "s_tax_reg_no" VARCHAR(40);
        ALTER TABLE "suppliers" ADD "phone2" VARCHAR(30);
        ALTER TABLE "suppliers" ADD "discount_percent" VARCHAR(40) NOT NULL DEFAULT 0;
        ALTER TABLE "suppliers" ADD "cnic" VARCHAR(40);
        CREATE UNIQUE INDEX "uid_suppliers_company_ca74a3" ON "suppliers" ("company_id");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "uid_suppliers_company_ca74a3";
        ALTER TABLE "suppliers" DROP COLUMN "due_days";
        ALTER TABLE "suppliers" DROP COLUMN "updated_by";
        ALTER TABLE "suppliers" DROP COLUMN "rev";
        ALTER TABLE "suppliers" DROP COLUMN "city";
        ALTER TABLE "suppliers" DROP COLUMN "address";
        ALTER TABLE "suppliers" DROP COLUMN "updated_at";
        ALTER TABLE "suppliers" DROP COLUMN "remarks";
        ALTER TABLE "suppliers" DROP COLUMN "active";
        ALTER TABLE "suppliers" DROP COLUMN "company_id";
        ALTER TABLE "suppliers" DROP COLUMN "email";
        ALTER TABLE "suppliers" DROP COLUMN "ntn";
        ALTER TABLE "suppliers" DROP COLUMN "created_at";
        ALTER TABLE "suppliers" DROP COLUMN "origin";
        ALTER TABLE "suppliers" DROP COLUMN "s_tax_reg_no";
        ALTER TABLE "suppliers" DROP COLUMN "phone2";
        ALTER TABLE "suppliers" DROP COLUMN "discount_percent";
        ALTER TABLE "suppliers" DROP COLUMN "cnic";
        DROP TABLE IF EXISTS "supplier_branch_links";
        DROP TABLE IF EXISTS "acc_depreciation_runs";
        DROP TABLE IF EXISTS "acc_depreciation_run_lines";
        DROP TABLE IF EXISTS "supplier_questions";
        DROP TABLE IF EXISTS "acc_fixed_assets";"""


MODELS_STATE = (
    "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"
    "gx3zMvvP/weKRhcm"
)
