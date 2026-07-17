-- Captura de precios históricos por-skin — correr en el SQL editor del proyecto
-- Supabase `cs-finance` (el mismo de market_cap_history).

create table if not exists public.tracked_skins (
    market_hash_name text primary key,
    source           text not null,            -- 'top_n' | 'inventory'
    first_seen       timestamptz not null default now(),
    last_captured    date                        -- null = nunca capturada (prioridad máxima)
);
alter table public.tracked_skins enable row level security;

create table if not exists public.precios_historicos (
    id               bigint generated always as identity primary key,
    market_hash_name text    not null,
    date             date    not null,
    price            numeric not null,
    volume           int,
    source           text,
    created_at       timestamptz not null default now(),
    unique (market_hash_name, date)
);
create index if not exists precios_historicos_name_date_idx
    on public.precios_historicos (market_hash_name, date);
alter table public.precios_historicos enable row level security;
