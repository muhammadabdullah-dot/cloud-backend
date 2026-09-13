from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "sync_inbox_events" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "event_id" VARCHAR(60) NOT NULL,
    "aggregate_type" VARCHAR(60) NOT NULL,
    "aggregate_id" VARCHAR(60) NOT NULL,
    "payload" JSON NOT NULL,
    "origin_user_id" VARCHAR(60),
    "origin_device_id" VARCHAR(80),
    "occurred_at" TIMESTAMP,
    "received_at" TIMESTAMP NOT NULL,
    "status" VARCHAR(20) NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_sync_inbox__branch__af7603" UNIQUE ("branch_id", "event_id")
);
        CREATE TABLE IF NOT EXISTS "sync_runs" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "received_at" TIMESTAMP NOT NULL,
    "event_count" INT NOT NULL,
    "accepted_count" INT NOT NULL,
    "duplicate_count" INT NOT NULL,
    "status" VARCHAR(20) NOT NULL,
    "note" TEXT,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE
) /* One push from one branch. The audit trail behind "when did this branch last reach us" — */;
        ALTER TABLE "branches" ADD "claimed_from" VARCHAR(120);
        ALTER TABLE "branches" ADD "pairing_code" VARCHAR(32);
        ALTER TABLE "branches" ADD "verified_at" TIMESTAMP;
        ALTER TABLE "branches" ADD "pairing_expires_at" TIMESTAMP;
        ALTER TABLE "branches" ADD "pairing_issued_at" TIMESTAMP;
        ALTER TABLE "branches" ADD "sync_secret_hash" VARCHAR(200);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "branches" DROP COLUMN "claimed_from";
        ALTER TABLE "branches" DROP COLUMN "pairing_code";
        ALTER TABLE "branches" DROP COLUMN "verified_at";
        ALTER TABLE "branches" DROP COLUMN "pairing_expires_at";
        ALTER TABLE "branches" DROP COLUMN "pairing_issued_at";
        ALTER TABLE "branches" DROP COLUMN "sync_secret_hash";
        DROP TABLE IF EXISTS "sync_runs";
        DROP TABLE IF EXISTS "sync_inbox_events";"""


MODELS_STATE = (
    "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"
)
