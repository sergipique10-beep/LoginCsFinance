-- Push notifications (FCM) — correr en el SQL editor del proyecto Supabase
-- `cs-finance` (el mismo de market_cap_history).
--
-- Estas dos tablas se crearon en su día vía el MCP de Supabase (apply_migration)
-- y nunca se versionaron, a diferencia del resto del esquema. Este fichero
-- reconstruye ese DDL para que el entorno sea reproducible: sin él, recrear el
-- proyecto Supabase desde cero deja las push rotas en silencio — el registro de
-- token devuelve 500 y el news-tick no encuentra dónde deduplicar.

-- Tokens FCM de los dispositivos registrados. Sin steam_id a propósito: el
-- contenido es broadcast (misma noticia para todos), así que no hay
-- personalización por usuario. El token ES la identidad del dispositivo.
create table if not exists public.device_tokens (
    token       text primary key,
    platform    text not null check (platform in ('android', 'ios')),
    created_at  timestamptz not null default now()
);
alter table public.device_tokens enable row level security;

-- Dedup del cron de noticias: un gid ya presente no se vuelve a notificar.
-- Es lo que hace `POST /internal/news-tick` idempotente frente a reintentos
-- del workflow de GitHub Actions.
create table if not exists public.notified_news (
    gid          text primary key,
    notified_at  timestamptz not null default now()
);
alter table public.notified_news enable row level security;
