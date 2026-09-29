# The same trim at head office: a campaign is one Item, a window of days, a discount and a minimum quantity.
#
# Free units, a quantity limit and a spend limit come off. Not one of the old software's 11,364 campaigns uses any of
# them, and carrying a field because a shop may want it tomorrow is how a salesperson ended up on a bill for a day.
from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "promotions" DROP COLUMN "bonus_qty";
        ALTER TABLE "promotions" DROP COLUMN "qty_limit";
        ALTER TABLE "promotions" DROP COLUMN "amount_limit";"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "promotions" ADD COLUMN "bonus_qty" VARCHAR(40) NOT NULL DEFAULT 0;
        ALTER TABLE "promotions" ADD COLUMN "qty_limit" VARCHAR(40);
        ALTER TABLE "promotions" ADD COLUMN "amount_limit" VARCHAR(40);"""
