-- Último inventario bueno por usuario (PERF-14) — correr en el SQL editor del
-- proyecto Supabase `cs-finance`. Idempotente.
--
-- Por qué existe: ante un 429 de steamwebapi (límite por minuto) el backend sirve el
-- último inventario con su fecha de captura en vez de un error. La caché en RAM
-- (`_inventory_cache`) no vale para eso: se vacía cada vez que Render free duerme el
-- proceso y solo guarda un `time.monotonic()`, que no se puede enseñar como «hace X».
--
-- Una fila por usuario: es el ÚLTIMO snapshot, no un histórico. Cada lectura con 200
-- la sobrescribe. `captured_at` lo pone el servidor.
--
-- Dato personal (qué skins tiene alguien): sin políticas, RLS activa = ningún cliente
-- habla con la tabla; solo el backend con la service key. Se borra con la cuenta
-- (DELETE /auth/account → inventory_snapshot_repo.delete_for_user).
create table if not exists public.inventory_snapshots (
    steam_id     text        primary key,
    items        jsonb       not null,
    captured_at  timestamptz not null default now()
);

alter table public.inventory_snapshots enable row level security;
