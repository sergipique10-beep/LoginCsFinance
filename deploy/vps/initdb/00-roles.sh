#!/bin/sh
# Primer arranque de Timescale (docker-entrypoint-initdb.d): rol `worker` dueño de su
# esquema y de nada más. El superusuario `postgres` solo lo usan initdb y los backups.
set -eu
: "${PG_WORKER_PASSWORD:?falta PG_WORKER_PASSWORD}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v pw="$PG_WORKER_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
create extension if not exists timescaledb;
select format('create role worker login password %L', :'pw')
 where not exists (select 1 from pg_roles where rolname = 'worker') \gexec
revoke all on database :"db" from public;
revoke connect on database postgres from public;
revoke create on schema public from public;
grant connect on database :"db" to worker;
create schema if not exists worker authorization worker;
alter role worker set search_path = worker, public;
SQL
