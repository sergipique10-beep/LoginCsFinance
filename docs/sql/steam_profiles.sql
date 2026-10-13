-- Perfil público de Steam por usuario (COST-07), lo escribe el worker con GetPlayerSummaries.
-- Correr en el SQL editor del proyecto Supabase `cs-finance`. Idempotente.
-- Tabla SOMBRA hasta el corte: Render solo la lee con PROFILE_SOURCE=own.
-- Dato personal: RLS sin políticas; se borra con la cuenta (DELETE /me).
create table if not exists public.steam_profiles (
    steam_id      text primary key,
    persona_name  text,
    avatar_full   text,
    avatar_medium text,
    profile_url   text,
    persona_state smallint,
    visibility    smallint,
    updated_at    timestamptz not null default now()
);
alter table public.steam_profiles enable row level security;
