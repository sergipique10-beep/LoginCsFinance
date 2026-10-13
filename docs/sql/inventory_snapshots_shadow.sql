-- Snapshot de inventario que escribe el worker del VPS (COST-07): la SOMBRA de
-- `inventory_snapshots` durante la convivencia. Misma forma, pero `items` son assets SIN precio
-- (worker/decode/inventory.py: extract); Render compone la tarjeta con `market_current`.
-- Render solo la lee con INVENTORY_SOURCE=own. En el corte (COST-06) se renombra.
-- Correr en el SQL editor del proyecto Supabase `cs-finance`. Idempotente. Aplicado en `cs-finance` el 2026-10-13 (migración cost07_inventory_snapshots_shadow).
-- Dato personal: RLS sin políticas; se borra con la cuenta (DELETE /me).
create table if not exists public.inventory_snapshots_shadow (
    steam_id    text primary key,
    items       jsonb       not null default '[]'::jsonb,
    captured_at timestamptz not null default now()
);
alter table public.inventory_snapshots_shadow enable row level security;
