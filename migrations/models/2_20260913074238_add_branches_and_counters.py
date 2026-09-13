from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branches" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "code" VARCHAR(20) NOT NULL UNIQUE,
    "name" VARCHAR(140) NOT NULL,
    "address" VARCHAR(255),
    "city" VARCHAR(120),
    "phone" VARCHAR(40),
    "timezone" VARCHAR(60) NOT NULL,
    "sync_url" VARCHAR(255),
    "status" VARCHAR(20) NOT NULL,
    "last_seen_at" TIMESTAMP,
    "created_at" TIMESTAMP NOT NULL
);
        CREATE TABLE IF NOT EXISTS "counters" (
    "id" VARCHAR(60) NOT NULL PRIMARY KEY,
    "value" INT NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "branches";
        DROP TABLE IF EXISTS "counters";"""


MODELS_STATE = (
    "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"
    "yn74b9pOulLtD/1e+bPv4P8wW0xA=="
)
