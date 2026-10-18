-- shadow_report: una fila por día de convivencia (spec § 6.3), la escribe el job `shadow_compare`
-- del worker (worker/jobs/shadow_compare.py) a las 00:25 UTC. Solo durante la convivencia; el corte
-- (COST-06) exige 7 filas seguidas con ok = true. Idempotente.
create table if not exists public.shadow_report (
    date                    date primary key,
    n                       integer,            -- nombres comparados (trending ∩ trending_own, ≥ 10 $)
    mediana_desv_pct        numeric,            -- |propio/steamwebapi − 1| mediana, en %
    p90_desv_pct            numeric,
    ratio_mediana           numeric,            -- propio/steamwebapi mediana (para calibrar k_mercado)
    corr_delta7d            numeric,
    n_delta7d               integer,
    overlap_trending_top50  numeric,            -- fracción del top-50 de steamwebapi presente en el propio
    overlap_movers          numeric,            -- ídem sobre (name, bucket) de los movers
    ratio_sold24h_mediana   numeric,            -- volumen Steam propio / sold_24h steamwebapi (top-N)
    n_sold                  integer,
    index_sign_match        boolean,            -- null = falta un día en alguna de las dos series
    steam_cooldown_last_s   integer,            -- cool:last de Valkey (0 = sin 429 en 24 h)
    ok                      boolean not null default false,
    fails                   text,               -- métricas que no cumplen, separadas por coma
    created_at              timestamptz not null default now()
);
alter table public.shadow_report enable row level security;
