-- Histórico del valor de la cartera por usuario (UX-16) — correr en el SQL editor
-- del proyecto Supabase `cs-finance`. Idempotente.
--
-- Por qué existe: hasta el 2026-09-29 esta serie vivía SOLO en el localStorage del
-- dispositivo, y `wipeLocalAccountData()` (LAUNCH-04) la borraba en cada cierre de
-- sesión. Como se guarda un punto por día, el usuario perdía meses de histórico al
-- desloguearse —o al reinstalar la app— y la gráfica de Inventory salía plana
-- durante días. El dato no existía en ningún otro sitio: no había de dónde
-- recuperarlo.
--
-- La PK compuesta es la regla de negocio, no un detalle: (steam_id, provider, date)
-- impone "un punto por día y proveedor de precios", que es exactamente lo que hacía
-- el cliente con `savePortfolioSnapshot()`. Un segundo envío el mismo día es un
-- upsert (on conflict), no una fila nueva.
--
-- Hay una serie por price provider (steam / csfloat / buff) porque el valor total
-- depende de a qué mercado se miren los precios: mezclarlas daría saltos que no son
-- revalorización.
create table if not exists public.portfolio_history (
    steam_id    text        not null,
    provider    text        not null,
    date        date        not null,
    value       numeric     not null check (value >= 0),
    updated_at  timestamptz not null default now(),
    primary key (steam_id, provider, date)
);

-- La consulta del cliente es siempre "mi serie de este provider, por fecha".
create index if not exists portfolio_history_user_idx
    on public.portfolio_history (steam_id, provider, date desc);

-- Como el resto del esquema: el acceso va por la service key del backend, que
-- resuelve el steam_id desde el `sub` del JWT. Ningún cliente habla con la tabla.
alter table public.portfolio_history enable row level security;
