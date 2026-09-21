#!/bin/sh
# Creates the two databases used by the *demo* environment:
#   metabase   - Metabase's own application database
#   warehouse  - the upstream "source system" that the demo dashboards query
# and a read-only role that Metabase uses to query the warehouse.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE DATABASE metabase;
CREATE DATABASE warehouse;
CREATE ROLE metabase_reader LOGIN PASSWORD '${DEMO_READER_PASSWORD}';
SQL

for sql in warehouse.sql warehouse_extra.sql; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname warehouse \
        -f "/docker-entrypoint-initdb.d/sql/$sql"
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname warehouse <<SQL
GRANT CONNECT ON DATABASE warehouse TO metabase_reader;
GRANT USAGE ON SCHEMA sales, product, finance, support, marketing, ops, web, billing TO metabase_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales, product, finance, support, marketing, ops, web, billing TO metabase_reader;
SQL
