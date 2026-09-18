-- Alertas de precio por skin (PUSH-07) — correr en el SQL editor del proyecto
-- Supabase `cs-finance`. Requiere device_tokens.steam_id (docs/sql/device_tokens.sql).
--
-- Una alerta es de UN SOLO DISPARO: triggered_at null = activa; con fecha =
-- disparada (sale de la rueda y se conserva para que el usuario la vea).
-- last_checked_at es el cursor LRU del alerts-tick (mismo patrón que
-- tracked_skins.last_captured).
create table if not exists public.price_alerts (
    id               bigint generated always as identity primary key,
    steam_id         text    not null,
    market_hash_name text    not null,
    direction        text    not null check (direction in ('above', 'below')),
    threshold        numeric not null check (threshold > 0),
    created_at       timestamptz not null default now(),
    last_checked_at  timestamptz,
    triggered_at     timestamptz,
    triggered_price  numeric
);

-- Solo las activas entran en la rueda: índice parcial ordenado por el cursor.
create index if not exists price_alerts_active_idx
    on public.price_alerts (last_checked_at nulls first)
    where triggered_at is null;

create index if not exists price_alerts_steam_id_idx
    on public.price_alerts (steam_id);

alter table public.price_alerts enable row level security;
