-- ============================================================================
-- Postgres first-boot initialisation (runs once, on an empty data volume).
--   1. Enables the extensions the schema depends on in the primary database.
--   2. Creates the dedicated `danah_test` database used by pytest, so the test
--      suite runs against real pgvector/FTS/triggers rather than a substitute.
-- Alembic migration 0001 also creates the extensions defensively, so a
-- managed/production Postgres that skips this script still works.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

SELECT 'CREATE DATABASE danah_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'danah_test')\gexec

\connect danah_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
