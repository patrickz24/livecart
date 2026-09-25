-- LiveCart login roles, created once per PostgreSQL cluster by init-roles.sh.
--
--   livecart_migrator  owns the databases and every schema object; runs the
--                      migrations, the seed and the test fixtures.
--   livecart_app       runtime role of the API and the worker; its only
--                      privileges are the grants of grants.sql (SELECT,
--                      INSERT and UPDATE on tables, never DELETE).
--
-- The passwords are read from the environment with \getenv (psql 15 and
-- later), so they never appear in a command line or in the SQL text. They are
-- quoted by format(%L), and \gexec runs each generated statement.
-- Re-running the script is safe and resets both passwords.

\set ON_ERROR_STOP on
\getenv migrator_password LIVECART_MIGRATOR_PASSWORD
\getenv app_password LIVECART_APP_PASSWORD

SELECT 'CREATE ROLE livecart_migrator'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'livecart_migrator')
\gexec

SELECT 'CREATE ROLE livecart_app'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'livecart_app')
\gexec

-- LOGIN only: neither role may create databases or roles, replicate or
-- bypass row-level security.
SELECT format(
    'ALTER ROLE livecart_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migrator_password'
)
\gexec

SELECT format(
    'ALTER ROLE livecart_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
\gexec
