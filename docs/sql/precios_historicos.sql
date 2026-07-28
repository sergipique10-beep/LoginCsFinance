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
    -- FK a tracked_skins: la serie de una skin no tiene sentido sin la skin.
    -- CASCADE porque el borrado natural es "dejamos de seguir esta skin" — sin
    -- él, una futura purga de tracked_skins (el análogo del purge_stale que ya
    -- existe para market_trending) dejaría la serie huérfana en silencio.
    -- El código ya respetaba el invariante: price_capture.capture() inserta
    -- únicamente nombres que acaba de leer de tracked_skins.
    market_hash_name text    not null
                     references public.tracked_skins (market_hash_name)
                     on delete cascade,
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
