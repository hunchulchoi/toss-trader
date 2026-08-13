-- Run with psql as a PostgreSQL superuser against the toss_trader database.
-- TOSS_MCP_POSTGRES_PASSWORD must be present in the psql process environment.
\set ON_ERROR_STOP on
\getenv mcp_password TOSS_MCP_POSTGRES_PASSWORD
\if :{?mcp_password}
\else
\echo 'TOSS_MCP_POSTGRES_PASSWORD is required'
\quit
\endif
SELECT length(:'mcp_password') > 0 AS mcp_password_present \gset
\if :mcp_password_present
\else
\echo 'TOSS_MCP_POSTGRES_PASSWORD must not be empty'
\quit
\endif

DO $migration$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'toss_mcp_reader') THEN
        CREATE ROLE toss_mcp_reader
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$migration$;

ALTER ROLE toss_mcp_reader PASSWORD :'mcp_password';
ALTER ROLE toss_mcp_reader
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
ALTER ROLE toss_mcp_reader SET default_transaction_read_only = on;
ALTER ROLE toss_mcp_reader SET statement_timeout = '5s';
ALTER ROLE toss_mcp_reader SET idle_in_transaction_session_timeout = '5s';

REVOKE ALL PRIVILEGES ON DATABASE toss_trader FROM toss_mcp_reader;
GRANT CONNECT ON DATABASE toss_trader TO toss_mcp_reader;
REVOKE CREATE ON SCHEMA public FROM toss_mcp_reader;
GRANT USAGE ON SCHEMA public TO toss_mcp_reader;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM toss_mcp_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM toss_mcp_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO toss_mcp_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO toss_mcp_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE toss_trader IN SCHEMA public
    GRANT SELECT ON TABLES TO toss_mcp_reader;
