-- Esquema del worker en la Timescale del VPS (COST-01).
-- Lo ejecuta 10-schema.sh en el primer arranque del contenedor, ya con el rol `worker`
-- creado (00-roles.sh) y como ese rol: todo lo de aquí es suyo y vive en su esquema.
-- Idempotente: se puede volver a lanzar a mano (runbook § Esquema).
--
-- Dos familias:
--   series  market_quotes (una fila por fuente y captura) y market_ref (precio de referencia
--           consolidado), hypertables con columnstore a 7 d y retención de 400 d, más sus
--           agregados diarios continuos. 400 d > 365: el «hace 1 año» siempre existe.
--   estado  inventory_assets, asset_decoded, catalog_items, source_health: tablas planas.

set search_path = worker, public;

-- ---------------------------------------------------------------- series
create table if not exists market_quotes (
    ts               timestamptz not null,
    market_hash_name text        not null,
    source           text        not null,          -- skinport | marketcsgo | buff | steam | csfloat …
    price_usd        numeric(12, 4),                -- null = la fuente trae el ítem sin precio
    volume           integer,                       -- ventas en la ventana que declare la fuente
    listings         integer,
    primary key (market_hash_name, source, ts)
);
select create_hypertable('market_quotes', by_range('ts', interval '1 day'), if_not_exists => true);

-- Última cotización por (ítem, fuente). market_quotes solo recibe una fila cuando el valor
-- CAMBIA respecto a esta tabla (el 2026-10-16, con una fila por captura, llegaron 8,6 M de filas
-- y 1,8 GB en 18 h: el 95 % de Skinport repetía el valor anterior, y la lectura
-- «distinct on … where ts > now()-24h» tardaba 63 s con un timeout de 30 s). `ts` es el último
-- cambio (= la fila de market_quotes); `seen_at`, la última vez que la fuente lo confirmó.
create table if not exists market_latest (
    market_hash_name text        not null,
    source           text        not null,
    ts               timestamptz not null,
    seen_at          timestamptz not null,
    price_usd        numeric(12, 4),
    volume           integer,
    listings         integer,
    primary key (market_hash_name, source)
);
-- Relleno inicial desde la serie, solo si la tabla está vacía (idempotente y barato al repetir).
insert into market_latest (market_hash_name, source, ts, seen_at, price_usd, volume, listings)
select distinct on (market_hash_name, source) market_hash_name, source, ts, ts, price_usd, volume, listings
from market_quotes
where not exists (select 1 from market_latest)
order by market_hash_name, source, ts desc
on conflict do nothing;

create table if not exists market_ref (
    ts               timestamptz not null,
    market_hash_name text        not null,
    price_usd        numeric(12, 4) not null,
    sources          smallint    not null,          -- cuántas fuentes entraron en la referencia
    method           text        not null,          -- p. ej. 'median', 'steam-only'
    primary key (market_hash_name, ts)
);
select create_hypertable('market_ref', by_range('ts', interval '7 days'), if_not_exists => true);

-- Columnstore: segmentar por ítem (las lecturas son «la serie de una skin»).
alter table market_quotes set (timescaledb.enable_columnstore,
    timescaledb.segmentby = 'market_hash_name, source', timescaledb.orderby = 'ts desc');
alter table market_ref set (timescaledb.enable_columnstore,
    timescaledb.segmentby = 'market_hash_name', timescaledb.orderby = 'ts desc');
call add_columnstore_policy('market_quotes', after => interval '7 days', if_not_exists => true);
call add_columnstore_policy('market_ref', after => interval '7 days', if_not_exists => true);

select add_retention_policy('market_quotes', drop_after => interval '400 days', if_not_exists => true);
select add_retention_policy('market_ref', drop_after => interval '400 days', if_not_exists => true);

-- ------------------------------------------------------- agregados diarios
create materialized view if not exists market_daily
with (timescaledb.continuous, timescaledb.materialized_only = false) as
select time_bucket(interval '1 day', ts) as day,
       market_hash_name,
       source,
       last(price_usd, ts) as close_usd,
       min(price_usd)      as low_usd,
       max(price_usd)      as high_usd,
       avg(price_usd)      as avg_usd,
       max(volume)         as volume,
       count(*)            as samples
from market_quotes
group by day, market_hash_name, source
with no data;

create materialized view if not exists market_ref_daily
with (timescaledb.continuous, timescaledb.materialized_only = false) as
select time_bucket(interval '1 day', ts) as day,
       market_hash_name,
       last(price_usd, ts) as close_usd,
       min(price_usd)      as low_usd,
       max(price_usd)      as high_usd,
       max(sources)        as sources
from market_ref
group by day, market_hash_name
with no data;

-- Refresco: los 3 últimos días cada hora (el cierre diario de COST-05 lee de aquí).
select add_continuous_aggregate_policy('market_daily', start_offset => interval '3 days',
    end_offset => interval '1 hour', schedule_interval => interval '1 hour', if_not_exists => true);
select add_continuous_aggregate_policy('market_ref_daily', start_offset => interval '3 days',
    end_offset => interval '1 hour', schedule_interval => interval '1 hour', if_not_exists => true);
-- Los agregados viven tanto como su fuente.
select add_retention_policy('market_daily', drop_after => interval '400 days', if_not_exists => true);
select add_retention_policy('market_ref_daily', drop_after => interval '400 days', if_not_exists => true);

-- ---------------------------------------------------------------- estado
create table if not exists inventory_assets (
    steam_id         text        not null,
    assetid          text        not null,
    classid          text        not null,
    instanceid       text        not null,
    market_hash_name text,
    amount           integer     not null default 1,
    seen_at          timestamptz not null default now(),
    primary key (steam_id, assetid)
);
create index if not exists inventory_assets_name_idx on inventory_assets (market_hash_name);

-- Lo que sale de asset_properties + el certificado decodificado (COST-07). Por assetid:
-- un asset conserva float y patrón aunque cambie de dueño.
create table if not exists asset_decoded (
    assetid          text primary key,
    paintwear        double precision,
    paintseed        integer,
    paintindex       integer,
    defindex         integer,
    pattern_template integer,
    certificate      text,
    checksum_ok      boolean,
    decoded_at       timestamptz not null default now()
);

create table if not exists catalog_items (
    market_hash_name text primary key,
    weapon           text,
    category         text,
    rarity           text,
    rarity_color     text,
    image_url        text,
    source           text        not null default 'bymykel',
    updated_at       timestamptz not null default now()
);

-- Una fila por fuente: lo que mira Uptime Kuma / market_age_min (COST-08) y el breaker (COST-02).
create table if not exists source_health (
    source               text primary key,
    last_ok_at           timestamptz,
    last_error_at        timestamptz,
    last_status          integer,
    last_error           text,
    consecutive_failures integer     not null default 0,
    updated_at           timestamptz not null default now()
);
