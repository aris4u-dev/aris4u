-- WS3 Phase 1: Schema Extensions for Per-Client Scoping
-- Date: 2026-05-31
-- Status: EXECUTED (idempotent, backward-compatible)
-- Impact: Adds client_id column to remaining memory tables

-- sessions.db:
-- ALTER TABLE v16_events ADD COLUMN client_id TEXT DEFAULT NULL;
-- ALTER TABLE digests ADD COLUMN client_id TEXT DEFAULT NULL;

-- ~/.claude-mem/claude-mem.db:
-- ALTER TABLE observations ADD COLUMN client_id TEXT DEFAULT NULL;
-- ALTER TABLE sdk_sessions ADD COLUMN client_id TEXT DEFAULT NULL;

-- Verification:
-- All tables now have client_id column (NULL for pre-migration rows)
-- Backward compatible: no breaking changes, idempotent

-- Row counts post-migration:
-- sessions.db: decisions (1205), guards (1168), v16_events (173872), digests (0)
-- claude-mem.db: observations (8726), sdk_sessions (4704)
