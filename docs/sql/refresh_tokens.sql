-- Refresh tokens vigentes (SEC-11) — correr en el SQL editor del proyecto Supabase
-- `cs-finance` ANTES del deploy que lo usa. Idempotente.
--
-- Por qué existe: hasta el 2026-10-03 los JTIs vivían en un dict en memoria del
-- proceso. Cada deploy, reinicio o despertar de Render lo vaciaba, y con él todos
-- los refresh tokens (7 días de vida): cada usuario volvía a /login.
--
-- Un refresh es válido si su fila existe y `expires_at` no ha pasado. Revocar =
-- borrar la fila; rotar = borrar la vieja (DELETE ... RETURNING, un solo uso) e
-- insertar la nueva. Los caducados se purgan en cada /auth/refresh.
create table if not exists public.refresh_tokens (
    jti         text        primary key,
    steam_id    text        not null,
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);

-- Borrado de cuenta (DELETE /me) y "cerrar sesión en todos los dispositivos".
create index if not exists refresh_tokens_steam_id_idx
    on public.refresh_tokens (steam_id);

-- Purga de caducados.
create index if not exists refresh_tokens_expires_at_idx
    on public.refresh_tokens (expires_at);

-- RLS sin políticas: solo la service key del backend la toca. La tabla dice quién
-- tiene sesión abierta: nunca debe ser legible con la anon key por PostgREST.
alter table public.refresh_tokens enable row level security;
