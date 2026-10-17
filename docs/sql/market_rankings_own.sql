-- Tablas SOMBRA de COST-05 — correr en el SQL editor del proyecto Supabase `cs-finance`
-- (o con apply_migration). Idempotente.
--
-- Las escribe el worker del VPS (jobs compute_rankings, compute_index y daily_close) mientras dura
-- la convivencia; Render sigue leyendo las de steamwebapi (market_trending, market_movers,
-- market_cap_history, precios_historicos). En el corte (COST-06) el worker pasa a escribir en las
-- de producción cambiando MARKET_*_TABLE / PRECIOS_HISTORICOS_TABLE, y estas se borran.
--
-- Los rankings copian la forma de los originales (`like … including all`: mismas columnas, defaults,
-- PK e índices) para que `_to_row` escriba exactamente la misma fila. Requiere que
-- docs/sql/market_rankings.sql esté aplicado (lo está en producción).

create table if not exists public.market_trending_own (like public.market_trending including all);
alter table public.market_trending_own enable row level security;

create table if not exists public.market_movers_own (like public.market_movers including all);
alter table public.market_movers_own enable row level security;

-- Índice propio: mismas columnas que market_cap_history más `sold24h` (el `/market/index.sold24h`
-- de steamwebapi venía de la API, no de la tabla; con el corte tendrá que salir de aquí).
-- `buyorderpriceindex` queda a null: ninguna fuente del worker trae órdenes de compra.
create table if not exists public.market_cap_history_own (
    ts                 timestamptz primary key,
    priceindex         double precision,
    realpriceindex     double precision,
    buyorderpriceindex double precision,
    turnover24h        double precision,
    sold24h            integer
);
alter table public.market_cap_history_own enable row level security;

-- Cierre diario propio para las tracked_skins (source = 'own'). Sin FK a tracked_skins a propósito:
-- la tabla de producción la gestiona Render; esta solo compara. En el corte se vuelca en
-- precios_historicos con `insert … on conflict (market_hash_name, date) do nothing`.
create table if not exists public.precios_historicos_own (
    market_hash_name text    not null,
    date             date    not null,
    price            numeric not null,
    volume           integer,
    source           text    not null default 'own',
    created_at       timestamptz not null default now(),
    primary key (market_hash_name, date)
);
alter table public.precios_historicos_own enable row level security;

-- Comparación diaria de la convivencia (criterios del diseño § 6.3):
--   -- solape del top-50 del trending
--   with a as (select name from public.market_trending order by turnover desc nulls last limit 50),
--        b as (select name from public.market_trending_own order by turnover desc nulls last limit 50)
--   select count(*) * 2.0 / 100 as solape from a join b using (name);
--   -- ratio precio propio / steamwebapi por fila (como en COST-03)
--   select percentile_cont(array[0.1, 0.5, 0.9]) within group (order by o.price_latest / t.price_latest)
--   from public.market_trending_own o join public.market_trending t using (name) where t.price_latest > 0;
--   -- signo de la variación diaria del índice
--   select date_trunc('day', ts) d, last(priceindex) … (ver comentario de COST-05 en Plane)
