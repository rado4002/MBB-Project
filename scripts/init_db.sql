-- ─────────────────────────────────────────────────────────────────────────────
-- MBB ya Kin — PostgreSQL Initialization Script
-- Sprint 0.1: Schema, extensions, and roles
-- Sprint 0.2: Full table DDL via Alembic migrations
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Extensions ────────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- UUID generation
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- Trigram similarity (MAPS search)
CREATE EXTENSION IF NOT EXISTS "unaccent";     -- Accent-insensitive search (French/Lingala)

-- ── Schema ────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS mbb;

-- Set default schema for future connections from this database
-- (Application user reads/writes only in mbb schema)
ALTER DATABASE mbb SET search_path TO mbb, public;

-- ── Application Role ──────────────────────────────────────────────────────────
-- Note: In this setup POSTGRES_USER (from secret) is also the app user.
-- This block ensures the mbb schema is accessible.
DO $$
DECLARE
    v_user TEXT := current_user;
BEGIN
    -- Grant schema usage to current user (idempotent)
    EXECUTE format('GRANT USAGE ON SCHEMA mbb TO %I', v_user);
    EXECUTE format('GRANT CREATE ON SCHEMA mbb TO %I', v_user);
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA mbb GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
        v_user
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA mbb GRANT USAGE, SELECT ON SEQUENCES TO %I',
        v_user
    );
END $$;

-- ── Verification ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE 'MBB schema initialized. Extensions: uuid-ossp, pg_trgm, unaccent.';
    RAISE NOTICE 'Run Alembic migrations (make migrate) to create all 10 tables.';
END $$;
