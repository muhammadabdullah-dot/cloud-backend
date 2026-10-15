"""The moment a voucher was entered, so a date range over the books can carry a time.

Written by hand rather than generated. `aerich migrate` answered "No changes detected" for this column, because the
run before it had already stamped the current model state into migration 36's MODELS_STATE -- so aerich believed 36
produced a column that 36 does not create. A fresh database built from the chain came out without it, which is how
this was noticed. The model is `app/models/accounts.Voucher.at`.

SQL Server already has the column: `scripts/backfill_voucher_moment.py` adds it there and fills it, because aerich
does not migrate SQL Server in this project. This is the same column for every fresh SQLite build.
"""
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "acc_vouchers" ADD COLUMN "at" TIMESTAMP;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "acc_vouchers" DROP COLUMN "at";"""
