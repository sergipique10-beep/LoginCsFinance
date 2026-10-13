-- Cola de descargas que Render pide al worker del VPS (COST-07) — correr en el SQL editor del
-- proyecto Supabase `cs-finance`. Idempotente. Aplicado en `cs-finance` el 2026-10-13 (migración cost07_fetch_requests).
--
-- Render inserta (kind, steam_id, prio); el worker la lee cada 3 s (ingress), la pasa a
-- `queued`, descarga y la cierra en `done` o `error` (`error` = private | rate_limit |
-- http_4xx | upstream). Render hace long-poll sobre el snapshot y, si la petición acaba en
-- error, decide el status HTTP con ese código (private → 403).
--
-- prio: 0 interactivo (GET /inventory, /me), 1 alerta, 2 fondo.
-- Dato personal (SteamID): RLS sin políticas; se borra con la cuenta (DELETE /me).
create table if not exists public.fetch_requests (
    id          bigint generated always as identity primary key,
    kind        text        not null check (kind in ('inventory', 'profile', 'delete')),
    steam_id    text        not null,
    prio        smallint    not null default 2 check (prio between 0 and 9),
    status      text        not null default 'pending' check (status in ('pending', 'queued', 'done', 'error')),
    error       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists fetch_requests_pending on public.fetch_requests (prio, created_at) where status = 'pending';
create index if not exists fetch_requests_user on public.fetch_requests (steam_id, kind, created_at desc);
alter table public.fetch_requests enable row level security;

-- Las cerradas no sirven para nada pasado un día (purga manual o desde un tick):
--   delete from public.fetch_requests where status in ('done', 'error') and updated_at < now() - interval '1 day';
