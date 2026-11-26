#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=app_password="$MONZO_APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE monzo_app LOGIN PASSWORD %L', :'app_password') \gexec
GRANT CONNECT ON DATABASE monzo TO monzo_app;
GRANT USAGE ON SCHEMA public TO monzo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO monzo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO monzo_app;
SQL
