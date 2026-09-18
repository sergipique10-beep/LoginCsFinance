-- Push notifications (FCM) — correr en el SQL editor del proyecto Supabase
-- `cs-finance` (el mismo de market_cap_history).
--
-- Estas dos tablas se crearon en su día vía el MCP de Supabase (apply_migration)
-- y nunca se versionaron, a diferencia del resto del esquema. Este fichero
-- reconstruye ese DDL para que el entorno sea reproducible: sin él, recrear el
-- proyecto Supabase desde cero deja las push rotas en silencio — el registro de
-- token devuelve 500 y el news-tick no encuentra dónde deduplicar.

-- Tokens FCM de los dispositivos registrados. El token es la identidad del
-- dispositivo (PK); steam_id es su dueño actual, lo que permite push
-- personalizadas (alertas de precio) además del broadcast de noticias.
create table if not exists public.device_tokens (
    token       text primary key,
    platform    text not null check (platform in ('android', 'ios')),
    created_at  timestamptz not null default now(),
    steam_id    text
);
alter table public.device_tokens enable row level security;

-- PUSH-06 (2026-09-18): la fase 1 era broadcast puro y no guardaba el dueño.
-- Nullable a propósito: los tokens ya registrados siguen recibiendo noticias y
-- se rellenan solos en el siguiente arranque de la app (registerForPush corre
-- en cada sesión restaurada). Un token sin steam_id nunca recibe una push
-- personalizada.
alter table public.device_tokens add column if not exists steam_id text;
create index if not exists device_tokens_steam_id_idx on public.device_tokens (steam_id);

-- Dedup del cron de noticias: un gid ya presente no se vuelve a notificar.
-- Es lo que hace `POST /internal/news-tick` idempotente frente a reintentos
-- del workflow de GitHub Actions.
create table if not exists public.notified_news (
    gid          text primary key,
    notified_at  timestamptz not null default now()
);
alter table public.notified_news enable row level security;
