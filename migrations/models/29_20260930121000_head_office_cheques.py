# Cheques at head office: the post dated cheque register, and what makes a bank reconcilable.
#
# Book-scoped exactly as vouchers are, so the register reads one branch or all of them the way every other accounts
# report here already does. Four dates rather than one (recorded, due, cleared, cancelled) because the old software's
# register filters on all four, and `posted` and `finalized` as separate flags because it asks those as two questions.
#
# The foreign keys are inline REFERENCES on CREATE TABLE. SQLite accepts them there and rejects the
# ALTER TABLE ADD CONSTRAINT form aerich would otherwise emit, which is the same correction migrations 6, 20, 30, 32
# and 34 on the branch already carry.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "ho_cheques" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "book" VARCHAR(20) NOT NULL DEFAULT 'HO',
            "number" VARCHAR(30) NOT NULL,
            "source" VARCHAR(160) UNIQUE,
            "direction" VARCHAR(10) NOT NULL DEFAULT 'received',
            "party_account_id" CHAR(36) NOT NULL REFERENCES "acc_accounts" ("id") ON DELETE RESTRICT,
            "bank_account_id" CHAR(36) REFERENCES "acc_accounts" ("id") ON DELETE SET NULL,
            "cheque_no" VARCHAR(30) NOT NULL,
            "drawn_on" VARCHAR(80),
            "amount" VARCHAR(40) NOT NULL,
            "received_on" DATE NOT NULL,
            "cheque_date" DATE NOT NULL,
            "cleared_on" DATE,
            "cancelled_on" DATE,
            "bounced_on" DATE,
            "deposited_on" DATE,
            "status" VARCHAR(10) NOT NULL DEFAULT 'pending',
            "posted" INT NOT NULL DEFAULT 0,
            "finalized" INT NOT NULL DEFAULT 0,
            "ref_no_1" VARCHAR(60),
            "ref_no_2" VARCHAR(60),
            "note" VARCHAR(255),
            "redeposit_of_id" VARCHAR(36),
            "created_by_name" VARCHAR(120),
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "uid_ho_cheques_book_number" UNIQUE ("book", "number")
        );
        CREATE INDEX IF NOT EXISTS "idx_ho_cheques_book_status_date" ON "ho_cheques" ("book", "status", "cheque_date");
        CREATE INDEX IF NOT EXISTS "idx_ho_cheques_book_dir_date" ON "ho_cheques" ("book", "direction", "cheque_date");
        CREATE TABLE IF NOT EXISTS "ho_cheque_lines" (
            "id" CHAR(36) NOT NULL PRIMARY KEY,
            "cheque_id" CHAR(36) NOT NULL REFERENCES "ho_cheques" ("id") ON DELETE CASCADE,
            "line_no" INT NOT NULL DEFAULT 1,
            "invoice_no" VARCHAR(60) NOT NULL,
            "invoice_date" DATE,
            "invoice_amount" VARCHAR(40) NOT NULL DEFAULT '0',
            "outstanding_before" VARCHAR(40) NOT NULL DEFAULT '0',
            "return_amount" VARCHAR(40) NOT NULL DEFAULT '0',
            "paid_amount" VARCHAR(40) NOT NULL,
            "note" VARCHAR(255),
            CONSTRAINT "uid_ho_cheque_lines_cheque_line" UNIQUE ("cheque_id", "line_no")
        );"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "ho_cheque_lines";
        DROP TABLE IF EXISTS "ho_cheques";"""
