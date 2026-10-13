-- Precio de referencia vigente por ítem (COST-03) — correr en el SQL editor del proyecto
-- Supabase `cs-finance` (o con apply_migration). Idempotente.
--
-- Lo escribe el worker del VPS (job compute_reference, cada 15 min) y SOLO las filas cuyo
-- contenido cambió (sha256 en Valkey). Mientras dure la convivencia es una tabla SOMBRA: nada
-- del backend de Render la lee hasta el corte (COST-06, PRICE_SOURCE=own).
--
-- `prices` lleva todas las cotizaciones del ítem (fuente, precio, volumen, ofertas, ts y si
-- entraron en la referencia). No es el feed crudo de ninguna fuente: no se redistribuye.
-- Tamaño esperado: ~25 k filas, < 50 MB (criterio de COST-03).
--
-- Datos públicos de mercado, pero solo los escribe el backend: RLS activa sin políticas.

-- En Supabase las extensiones viven en el esquema `extensions` (el advisor marca las de public).
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;

create table if not exists public.market_current (
    market_hash_name text primary key,
    ref_price_usd    numeric(12, 4),
    ref_confidence   smallint     not null default 0 check (ref_confidence between 0 and 3),
    ref_ts           timestamptz,
    sources          smallint     not null default 0,
    volume_24h       integer,
    turnover_24h     numeric(14, 2),
    prices           jsonb        not null default '[]'::jsonb,
    updated_at       timestamptz  not null default now()
);

-- Búsqueda por nombre (sustituye a /market/items?q= de steamwebapi).
create index if not exists market_current_name_trgm
    on public.market_current using gin (market_hash_name extensions.gin_trgm_ops);
-- Rankings por turnover (trending/movers, COST-05).
create index if not exists market_current_turnover
    on public.market_current (turnover_24h desc nulls last);

alter table public.market_current enable row level security;

-- Comprobaciones diarias de la convivencia (criterio de COST-03):
--   select count(*) filter (where ref_confidence >= 2) as conf_ge2,
--          avg(extract(epoch from now() - ref_ts) / 3600)::numeric(6,2) as edad_media_h,
--          pg_size_pretty(pg_total_relation_size('public.market_current')) as tamano
--   from public.market_current;
-- El % de filas cambiadas por tick sale en el log del job (`changed_pct`, < 20 % esperado).
