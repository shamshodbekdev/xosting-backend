#!/usr/bin/env bash
# Creates one database and one least-privileged role per service
# (database-per-service pattern). Runs once, on first container start.
set -euo pipefail

create_database() {
  local db="$1" user="$2" password="$3"
  echo "Creating database '${db}' owned by '${user}'"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
       -v db="$db" -v user="$user" -v password="$password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'user', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
}

create_database auth_db    auth_user    "${AUTH_DB_PASSWORD:?}"
create_database billing_db billing_user "${BILLING_DB_PASSWORD:?}"
create_database hosting_db hosting_user "${HOSTING_DB_PASSWORD:?}"
create_database domain_db  domain_user  "${DOMAIN_DB_PASSWORD:?}"
create_database support_db support_user "${SUPPORT_DB_PASSWORD:?}"
