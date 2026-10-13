#!/bin/sh
# Crea las tablas del worker COMO el rol worker: así es su dueño y puede gestionar
# sus propias políticas de Timescale sin ser superusuario.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
set role worker;
\i /schema/schema.sql
SQL
