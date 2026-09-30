# The party behind a customer or supplier account, kept on the account itself at head office.
#
# Head office holds no Party and no Supplier of a branch's. Its receivable and payable registers group by area,
# sub-area and party category and age against the party's own credit days, so those facts have to travel with the
# account that represents the party. The branch reads them off the party and sends them beside the account
# (branch accounts_chart_service.account_payload), so the two can never arrive out of step.
#
# Every column is nullable and every existing row keeps working: an account whose branch has not been upgraded yet
# simply has them empty, and fills them in the next time that account changes.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "acc_accounts" ADD COLUMN "party_code" VARCHAR(20);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_phone" VARCHAR(30);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_address" VARCHAR(255);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_contact" VARCHAR(120);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_city" VARCHAR(80);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_area" VARCHAR(120);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_sub_area" VARCHAR(120);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_category" VARCHAR(80);
        ALTER TABLE "acc_accounts" ADD COLUMN "party_due_days" INT NOT NULL DEFAULT 0;
        ALTER TABLE "acc_accounts" ADD COLUMN "party_credit_limit" VARCHAR(40);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "acc_accounts" DROP COLUMN "party_code";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_phone";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_address";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_contact";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_city";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_area";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_sub_area";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_category";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_due_days";
        ALTER TABLE "acc_accounts" DROP COLUMN "party_credit_limit";"""
