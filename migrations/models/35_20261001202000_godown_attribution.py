from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    """Room for the person named on an imported godown document, and a marker for where it came from.

    Multi-Tec's head office raised 811 purchase orders for its own godown and received 5,968 deliveries into it. The
    people on those documents are eight and twenty of their logins (ZAIN, AWISGT, ASIFPOMT and the rest), and `users`
    holds six rows: the sign-ins made for this system. Their 90 people are records in `branch_users` with no sign-in,
    which the migration plan decided on purpose, so none of them can be a foreign key. `raised_by` therefore becomes
    nullable and a name column carries the truth instead, which is the convention `branch_purchasing.py` already
    states for the branch side.

    SQLite cannot alter a column's nullability, so `raised_by_id` is not relaxed here. A fresh SQLite database is
    built from the models, which say null=True, so it arrives correct. SQL Server is changed in place by
    `scripts/add_godown_attribution.py`, because `sqlserver_schema.py --build` drops every table and this database
    holds 4,241,357 stock movements.
    """
    return """
        ALTER TABLE "warehouse_purchase_orders" ADD COLUMN "raised_by_name" VARCHAR(120);
        ALTER TABLE "warehouse_purchase_orders" ADD COLUMN "source" VARCHAR(10) NOT NULL DEFAULT 'godown';
        ALTER TABLE "warehouse_grns" ADD COLUMN "received_by_name" VARCHAR(120);
        ALTER TABLE "warehouse_grns" ADD COLUMN "source" VARCHAR(10) NOT NULL DEFAULT 'godown';
        ALTER TABLE "transfers" ADD COLUMN "dispatched_by_name" VARCHAR(120);
        ALTER TABLE "transfers" ADD COLUMN "source" VARCHAR(10) NOT NULL DEFAULT 'godown';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "warehouse_purchase_orders" DROP COLUMN "raised_by_name";
        ALTER TABLE "warehouse_purchase_orders" DROP COLUMN "source";
        ALTER TABLE "warehouse_grns" DROP COLUMN "received_by_name";
        ALTER TABLE "warehouse_grns" DROP COLUMN "source";
        ALTER TABLE "transfers" DROP COLUMN "dispatched_by_name";
        ALTER TABLE "transfers" DROP COLUMN "source";"""
