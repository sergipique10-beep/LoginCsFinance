-- PERF-11 — cola priorizada del price-tick. Correr en el SQL editor del
-- proyecto Supabase `cs-finance` DESPUÉS de precios_historicos.sql y
-- price_alerts.sql. Idempotente.
--
-- Por qué: el price-tick hacía un lookup a steamwebapi por cada skin de
-- tracked_skins y día (~800) y el plan Starter da ~333/día para todo el
-- backend. Ahora gasta como mucho PRICE_DAILY_BUDGET (variable de entorno) y,
-- si la población no cabe, elige por prioridad.
--
-- Poda BLANDA: una skin que nadie ha visto en 30 días sale de la cola pero NO
-- se borra. Borrarla dispararía el ON DELETE CASCADE de precios_historicos y
-- se perdería su serie entera, sin vuelta atrás.

-- Cuándo se vio por última vez en cualquier origen (inventario, trending,
-- alerta). Lo refresca register_tracked en cada registro.
alter table public.tracked_skins add column if not exists last_seen timestamptz;
-- Cuándo apareció por última vez en un inventario. `source` guarda solo el
-- PRIMER origen: una skin que entró por el trending y luego está en un
-- inventario seguiría marcada 'trending' sin esta columna.
alter table public.tracked_skins add column if not exists inventory_seen_at timestamptz;

-- Arranque: las filas existentes no tienen historial de "visto". Se les da
-- 30 días de gracia desde hoy; las que nadie vuelva a registrar en ese plazo
-- salen solas de la cola. Las de origen inventario arrancan con prioridad de
-- inventario. `where ... is null` hace que re-ejecutar esto no pise nada.
update public.tracked_skins set last_seen = now() where last_seen is null;
update public.tracked_skins set inventory_seen_at = now()
 where source = 'inventory' and inventory_seen_at is null;
alter table public.tracked_skins alter column last_seen set default now();

-- La cola que lee el price-tick. prioridad: 0 alerta activa, 1 en un
-- inventario visto en 30 días, 2 el resto (trending, seed top_n).
-- security_invoker: la vista aplica los permisos de quien consulta, no los
-- del dueño. El backend usa service_role y la ve entera; anon/authenticated
-- chocan con el RLS de tracked_skins igual que hoy.
create or replace view public.price_tick_queue with (security_invoker = true) as
select ts.market_hash_name,
       ts.last_captured,
       case
         when a.market_hash_name is not null then 0
         when ts.inventory_seen_at >= now() - interval '30 days' then 1
         else 2
       end as prioridad
  from public.tracked_skins ts
  left join (select distinct market_hash_name
               from public.price_alerts
              where triggered_at is null) a using (market_hash_name)
 where a.market_hash_name is not null
    or ts.last_seen >= now() - interval '30 days';
