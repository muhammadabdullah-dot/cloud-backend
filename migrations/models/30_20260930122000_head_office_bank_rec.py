# Bank reconciliation at head office, and the Clear / UnClear tick their statement report prints.
#
# Book-scoped like the cheques beside it: the accountant sits at head office and reconciles whichever book's bank
# account is in front of them, exactly as the old software does with its branch dropdown.
#
# The tick is a nullable foreign key added to acc_voucher_lines. It goes on as an inline REFERENCES on ADD COLUMN,
# which SQLite accepts; the ALTER TABLE ADD CONSTRAINT form aerich writes by default it does not, and correcting
# that by hand is what branch migrations 6, 20, 30, 32 and 34 already do.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "acc_bank_reconciliations" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "book" VARCHAR(20) NOT NULL DEFAULT 'HO',
            "number" VARCHAR(20) NOT NULL UNIQUE,
            "account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
            "up_to" DATE NOT NULL,
            "statement_balance" VARCHAR(40) NOT NULL,
            "book_balance" VARCHAR(40),
            "status" VARCHAR(10) NOT NULL DEFAULT 'open',
            "closed_at" TIMESTAMP,
            "closed_by_name" VARCHAR(120),
            "note" VARCHAR(255),
            "created_by_name" VARCHAR(120),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS "idx_acc_bank_rec_book_status" ON "acc_bank_reconciliations" ("book", "status");
        ALTER TABLE "acc_voucher_lines" ADD COLUMN "reconciliation_id" CHAR(36)
            REFERENCES "acc_bank_reconciliations" ("id") ON DELETE SET NULL;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "acc_voucher_lines" DROP COLUMN "reconciliation_id";
        DROP TABLE IF EXISTS "acc_bank_reconciliations";"""
