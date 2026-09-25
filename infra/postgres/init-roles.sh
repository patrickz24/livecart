#!/usr/bin/env bash
# Creates the LiveCart roles and the livecart and livecart_test databases, with
# the same roles and default privileges in both.
#
# The official postgres image runs it once, on the first start with an empty
# data directory, from /docker-entrypoint-initdb.d. It is idempotent, so it can
# also be re-run by hand as the superuser. The SQL files must not sit directly
# in /docker-entrypoint-initdb.d (the image would run them on their own): mount
# this directory elsewhere, for example /opt/livecart/postgres, and only this
# script into /docker-entrypoint-initdb.d.
#
# Environment:
#   LIVECART_MIGRATOR_PASSWORD  password of livecart_migrator (required)
#   LIVECART_APP_PASSWORD       password of livecart_app (required)
#   LIVECART_SQL_DIR            directory of roles.sql and grants.sql
#                               (default: the directory of this script)
#   POSTGRES_USER               superuser, set by the postgres image
set -Eeo pipefail

: "${LIVECART_MIGRATOR_PASSWORD:?LIVECART_MIGRATOR_PASSWORD is required}"
: "${LIVECART_APP_PASSWORD:?LIVECART_APP_PASSWORD is required}"

sql_dir="${LIVECART_SQL_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
databases=(livecart livecart_test)

run_psql() {
  psql --no-psqlrc --set ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" "$@"
}

# roles.sql reads both passwords from the environment (\getenv): they never
# appear on a command line, where the process list would show them.
run_psql --dbname postgres --file "$sql_dir/roles.sql"

# By default PUBLIC may connect to the maintenance databases, and create
# temporary tables in postgres. Only the superuser needs them, so neither
# LiveCart role can connect anywhere but livecart and livecart_test.
# (A database created later does not copy these privileges from template1.)
for maintenance_db in postgres template1; do
  run_psql --dbname postgres --command "REVOKE ALL ON DATABASE $maintenance_db FROM PUBLIC"
done

for db in "${databases[@]}"; do
  # CREATE DATABASE cannot run in a transaction or a DO block: \gexec runs it
  # only when the database does not exist yet.
  run_psql --dbname postgres --set db="$db" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER livecart_migrator', :'db')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')
\gexec
SQL
  run_psql --dbname "$db" --set db="$db" --file "$sql_dir/grants.sql"
done

echo "init-roles: livecart_migrator and livecart_app ready in: ${databases[*]}"
