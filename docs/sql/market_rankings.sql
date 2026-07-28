-- Rankings de mercado CS2 (trending + movers) — correr en el SQL editor del
-- proyecto Supabase `cs-finance` (el mismo de market_cap_history).
--
-- market_trending YA EXISTE en producción: los `create table if not exists` son
-- no-ops y la parte que realmente aplica son los `alter table add column`.
-- market_movers nunca tuvo DDL en el repo (se creó a mano) — aquí queda escrita.
--
-- Contexto del cambio: el trending-tick pasa de replace-all (18 items) a upsert
-- por `name` (~500 items), y el enriquecimiento con csfloat/history se separa a
-- su propio tick que avanza como una rueda sobre la tabla. De ahí seen_at
-- (purga) y enriched_at (rueda).

create table if not exists public.market_trending (
    name             text primary key,
    rank             integer not null default 0,
    slug             text,
    weapon_type      text,
    item_name        text,
    item_type        text,
    image            text,
    rarity           text,
    rarity_color     text,
    border_color     text,
    quality          text,
    is_stat_trak     boolean not null default false,
    is_souvenir      boolean not null default false,
    is_star          boolean not null default false,
    exterior         text,
    float_min        numeric,
    float_max        numeric,
    paint_index      integer,
    phase            text,
    price_latest     numeric not null default 0,
    csfloat_price    numeric,
    buff_price       numeric,
    price_delta_24h  numeric,
    price_delta_7d   numeric,
    price_delta_30d  numeric,
    updated_at       timestamptz not null default now()
);
alter table public.market_trending enable row level security;

create table if not exists public.market_movers (
    name             text not null,
    bucket           text not null,             -- 'hot' | 'cold'
    rank             integer not null default 0,
    slug             text,
    weapon_type      text,
    item_name        text,
    item_type        text,
    image            text,
    rarity           text,
    rarity_color     text,
    border_color     text,
    quality          text,
    is_stat_trak     boolean not null default false,
    is_souvenir      boolean not null default false,
    is_star          boolean not null default false,
    exterior         text,
    float_min        numeric,
    float_max        numeric,
    paint_index      integer,
    phase            text,
    price_latest     numeric not null default 0,
    csfloat_price    numeric,
    buff_price       numeric,
    price_delta_24h  numeric,
    price_delta_7d   numeric,
    price_delta_30d  numeric,
    updated_at       timestamptz not null default now(),
    -- PK compuesta, no `name` sola: el mismo item puede aparecer en hot y en
    -- cold (verificado contra el esquema real en producción).
    primary key (name, bucket)
);
alter table public.market_movers enable row level security;

-- ── Columnas de datos (las dos tablas) ──────────────────────────────────────
-- Van en ambas porque las dos comparten `_to_row` en steam/market_rows.py: si
-- solo estuvieran en trending, el insert de movers pincharía con "column does
-- not exist".
--
-- sold_24h arregla además un bug visible: la tarjeta hace
-- 'Vol: ' + item.sold24h y _row_to_item no lo devolvía → "Vol: undefined/24h".
--
-- turnover (price_latest * sold_24h) se persiste para ordenar sin recalcular:
-- con upsert el `rank` deja de ser fiable como orden (ver market.py) y este es
-- el criterio real de la lista.
alter table public.market_trending
    add column if not exists sold_24h      integer,
    add column if not exists sold_7d       integer,
    add column if not exists sold_30d      integer,
    add column if not exists offer_volume  integer,
    add column if not exists hours_to_sold numeric,
    add column if not exists price_real    numeric,
    add column if not exists steam_url     text,
    add column if not exists turnover      numeric;

alter table public.market_movers
    add column if not exists sold_24h      integer,
    add column if not exists sold_7d       integer,
    add column if not exists sold_30d      integer,
    add column if not exists offer_volume  integer,
    add column if not exists hours_to_sold numeric,
    add column if not exists price_real    numeric,
    add column if not exists steam_url     text,
    add column if not exists turnover      numeric;

-- ── Rueda progresiva y purga (solo trending) ────────────────────────────────
-- movers sigue con replace-all (20 items, DELETE+INSERT en cada tick): no
-- acumula enriquecimiento, así que no necesita ni rueda ni purga.
alter table public.market_trending
    add column if not exists seen_at     timestamptz not null default now(),
    add column if not exists enriched_at timestamptz;

-- Blindaje: si la purga corre entre el fetch_stalest y el upsert del
-- enrich-tick, ese upsert re-insertaría la fila sin `rank` y violaría el
-- not null. Con default 0 entra como rank 0 y la siguiente captura lo corrige.
alter table public.market_trending alter column rank set default 0;

-- enrich-tick pide los N con enriched_at más antiguo, nulls primero.
create index if not exists market_trending_enriched_at_idx
    on public.market_trending (enriched_at nulls first);

-- La purga filtra por seen_at.
create index if not exists market_trending_seen_at_idx
    on public.market_trending (seen_at);

-- GET /market/trending ordena por turnover desc.
create index if not exists market_trending_turnover_idx
    on public.market_trending (turnover desc nulls last);
