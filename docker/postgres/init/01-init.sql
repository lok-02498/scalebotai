-- =============================================================================
-- ScaleBotAI - PostgreSQL first-boot initialisation
-- -----------------------------------------------------------------------------
-- WHEN does this run?
--   The official postgres image executes every *.sql / *.sh file in
--   /docker-entrypoint-initdb.d/ (bind-mounted from docker/postgres/init/) ONCE,
--   only when the data directory is EMPTY, i.e. the first time the named volume
--   "postgres-data" is used. It does NOT run again on restarts or upgrades.
--   To re-run it locally you must wipe the volume:  docker compose down -v
--   (this DELETES all data).
--
-- HOW does it run?
--   Via psql, connected as POSTGRES_USER to POSTGRES_DB, with ON_ERROR_STOP=1
--   (any error aborts container initialisation). psql meta-commands such as
--   \getenv and \gexec are therefore available.
--
-- WHAT it does NOT do:
--   It does NOT create the application tables (users, conversations, chats,
--   feedback). The FastAPI app creates them at startup (AUTO_CREATE_TABLES=true),
--   guarded by a Postgres advisory lock so concurrent replicas do not race
--   (see docs/04-database-design.md). Keeping schema ownership in one place (the app)
--   avoids drift between this file and the SQLAlchemy models.
-- =============================================================================

\set ON_ERROR_STOP on

-- -----------------------------------------------------------------------------
-- 1. pg_stat_statements: per-query execution statistics (calls, total/mean time,
--    rows, cache hits). Used by postgres-exporter (--collector.stat_statements)
--    and handy for finding slow queries during load tests:
--
--      SELECT calls, round(mean_exec_time::numeric, 2) AS mean_ms, query
--      FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;
--
--    The library itself is preloaded by the server command line in
--    docker-compose.yml (-c shared_preload_libraries=pg_stat_statements);
--    the extension only creates the view/functions in this database.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

COMMENT ON EXTENSION pg_stat_statements IS
    'Query statistics for ScaleBotAI performance analysis (scraped by postgres-exporter)';

-- -----------------------------------------------------------------------------
-- 2. Read-only "monitoring" role for prometheuscommunity/postgres-exporter.
--
--    Password: taken from the environment variable POSTGRES_MONITORING_PASSWORD
--    of the postgres container (docker-compose.yml passes it through). If that
--    variable is not set, the documented PLACEHOLDER below is used:
--
--        change-me-monitoring-password
--
--    The SAME value must be given to postgres-exporter (DATA_SOURCE_PASS).
--    Change it for any shared or long-lived environment.
--
--    \getenv only overwrites the psql variable when the environment variable
--    exists, so the \set line acts as the default. (psql >= 15.)
-- -----------------------------------------------------------------------------
\set monitoring_password 'change-me-monitoring-password'
\getenv monitoring_password POSTGRES_MONITORING_PASSWORD

-- CREATE ROLE has no IF NOT EXISTS, so build the statement conditionally and
-- execute it with \gexec (runs each result row as a SQL statement). format(%L)
-- quotes the password safely as a literal.
SELECT format(
           'CREATE ROLE monitoring WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
           'NOREPLICATION NOBYPASSRLS INHERIT CONNECTION LIMIT 5 PASSWORD %L',
           :'monitoring_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitoring')
\gexec

\unset monitoring_password

COMMENT ON ROLE monitoring IS
    'Read-only role used by postgres-exporter (Prometheus). Member of pg_monitor.';

-- pg_monitor is a built-in role bundling pg_read_all_settings, pg_read_all_stats
-- and pg_stat_scan_tables: it can read every statistics view (pg_stat_activity,
-- pg_stat_database, pg_stat_statements, replication stats, ...) but NOT the data
-- in application tables.
GRANT pg_monitor TO monitoring;

-- Allow it to connect to the application database (name taken from POSTGRES_DB).
SELECT format('GRANT CONNECT ON DATABASE %I TO monitoring', current_database())
\gexec

-- Defence in depth: every session of this role is read-only and short-lived.
ALTER ROLE monitoring SET default_transaction_read_only = on;
ALTER ROLE monitoring SET statement_timeout = '15s';
ALTER ROLE monitoring SET idle_in_transaction_session_timeout = '30s';

-- PostgreSQL 15+ already revokes CREATE on schema public from PUBLIC; we keep the
-- explicit statement so the intent is visible (and correct on older servers).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- 3. Database-level defaults for the application database.
--    UTC everywhere: all timestamps are TIMESTAMPTZ and logs use UTC.
-- -----------------------------------------------------------------------------
SELECT format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC')
\gexec

-- Verification (printed in `docker compose logs postgres` on first boot).
SELECT rolname, rolcanlogin, rolconnlimit
FROM pg_roles
WHERE rolname = 'monitoring';
