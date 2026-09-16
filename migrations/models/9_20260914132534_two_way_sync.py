from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "branch_role_templates" (
    "role_id" VARCHAR(40) NOT NULL PRIMARY KEY,
    "name" VARCHAR(80) NOT NULL,
    "resources" JSON NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "updated_by" VARCHAR(160)
) /* A branch role's standard access, as head office has set it. Starts as the branch software's own */;
        CREATE TABLE IF NOT EXISTS "branch_staff" (
    "id" VARCHAR(60) NOT NULL PRIMARY KEY,
    "name" VARCHAR(120) NOT NULL,
    "email" VARCHAR(180) NOT NULL,
    "role_id" VARCHAR(40) NOT NULL,
    "active" INT NOT NULL,
    "password_hash" VARCHAR(255) NOT NULL,
    "permissions" JSON NOT NULL,
    "rev" INT NOT NULL,
    "last_changed_at" VARCHAR(10) NOT NULL,
    "last_changed_by" VARCHAR(160),
    "updated_at" TIMESTAMP NOT NULL,
    "created_at" TIMESTAMP NOT NULL
);
        CREATE TABLE IF NOT EXISTS "branch_manifests" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "resources" JSON NOT NULL,
    "roles" JSON NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "branch_id" CHAR(36) NOT NULL UNIQUE REFERENCES "branches" ("id") ON DELETE CASCADE
) /* What a branch server's code says about itself: the screens and actions it can grant, and its */;
        CREATE TABLE IF NOT EXISTS "branch_messages" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "seq" INT NOT NULL,
    "kind" VARCHAR(40) NOT NULL,
    "payload" JSON NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "delivered_at" TIMESTAMP,
    "applied_at" TIMESTAMP,
    "apply_error" TEXT,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_mess_branch__bfd856" UNIQUE ("branch_id", "seq")
);
        CREATE TABLE IF NOT EXISTS "branch_staff_assignments" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMP NOT NULL,
    "branch_id" CHAR(36) NOT NULL REFERENCES "branches" ("id") ON DELETE CASCADE,
    "staff_id" VARCHAR(60) NOT NULL REFERENCES "branch_staff" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_branch_staf_staff_i_d49418" UNIQUE ("staff_id", "branch_id")
) /* A person works at a branch. Removing the row takes their account out of that branch - switched */;
        ALTER TABLE "branches" ADD "last_applied_seq" INT NOT NULL DEFAULT 0;
        ALTER TABLE "branches" ADD "last_pulled_at" TIMESTAMP;
        ALTER TABLE "sync_inbox_events" ADD "apply_error" TEXT;
        ALTER TABLE "transfers" ADD "notes" VARCHAR(255);
        ALTER TABLE "transfers" ADD "source_branch_id" CHAR(36) REFERENCES "branches" ("id") ON DELETE SET NULL;
        ALTER TABLE "transfers" ADD "received_by_name" VARCHAR(120);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "branches" DROP COLUMN "last_applied_seq";
        ALTER TABLE "branches" DROP COLUMN "last_pulled_at";
        ALTER TABLE "sync_inbox_events" DROP COLUMN "apply_error";
        ALTER TABLE "transfers" DROP COLUMN "notes";
        ALTER TABLE "transfers" DROP COLUMN "source_branch_id";
        ALTER TABLE "transfers" DROP COLUMN "received_by_name";
        DROP TABLE IF EXISTS "branch_staff";
        DROP TABLE IF EXISTS "branch_role_templates";
        DROP TABLE IF EXISTS "branch_messages";
        DROP TABLE IF EXISTS "branch_manifests";
        DROP TABLE IF EXISTS "branch_staff_assignments";"""


MODELS_STATE = (
    "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"
    "2oJ+sGsLatHJvfuP2X/+HzvGj30="
)
