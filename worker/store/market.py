"""Escritura y lectura de precios en la Timescale del VPS (COST-03). Esquema: deploy/vps/schema.sql."""
import datetime as dt
from typing import Any

from worker.models import CatalogItem, Quote
from worker.pricing.reference import Reference

# Un lote entero en una sentencia: actualiza market_latest (seen_at siempre; ts y valores solo si
# cambian) y escribe en la serie market_quotes únicamente las cotizaciones cuyo valor ha cambiado.
# Sin esto la serie crecía una fila por captura (8,6 M filas / 1,8 GB en 18 h) y la lectura de
# «la última de cada fuente» pasó de milisegundos a 63 s (deploy/vps/schema.sql, market_latest).
INSERT_QUOTES = """
with q as (
    select * from unnest($1::timestamptz[], $2::text[], $3::text[], $4::float8[], $5::int[], $6::int[])
        as t(ts, market_hash_name, source, price_usd, volume, listings)
), changed as (
    insert into market_latest (market_hash_name, source, ts, seen_at, price_usd, volume, listings)
    select market_hash_name, source, ts, ts, price_usd::numeric(12, 4), volume, listings from q
    on conflict (market_hash_name, source) do update set
        seen_at   = excluded.seen_at,
        ts        = case when (market_latest.price_usd, market_latest.volume, market_latest.listings)
                              is distinct from (excluded.price_usd, excluded.volume, excluded.listings)
                         then excluded.ts else market_latest.ts end,
        price_usd = excluded.price_usd, volume = excluded.volume, listings = excluded.listings
    where (market_latest.price_usd, market_latest.volume, market_latest.listings)
          is distinct from (excluded.price_usd, excluded.volume, excluded.listings)
       or market_latest.seen_at < excluded.seen_at
    returning market_hash_name, source, ts, seen_at, price_usd, volume, listings
)
insert into market_quotes (ts, market_hash_name, source, price_usd, volume, listings)
select ts, market_hash_name, source, price_usd, volume, listings from changed where ts = seen_at
on conflict do nothing
"""

# La última cotización de cada (ítem, fuente) confirmada dentro de la ventana. `seen_at` hace de
# `ts` de la Quote: la edad que pesa la referencia es la de la última confirmación, no la del
# último cambio (un precio que lleva 6 h sin moverse sigue siendo un precio vigente).
LATEST = """
select seen_at, market_hash_name, source, price_usd::float8, volume, listings
from market_latest where seen_at > $1
"""

INSERT_REF = """
insert into market_ref (ts, market_hash_name, price_usd, sources, method)
values ($1, $2, $3, $4, 'weighted-median') on conflict do nothing
"""

UPSERT_CATALOG = """
insert into catalog_items (market_hash_name, weapon, category, rarity, rarity_color, image_url, updated_at)
values ($1, $2, $3, $4, $5, $6, now())
on conflict (market_hash_name) do update set weapon = excluded.weapon, category = excluded.category,
  rarity = excluded.rarity, rarity_color = excluded.rarity_color, image_url = excluded.image_url,
  updated_at = now()
"""

SOURCE_OK = """
insert into source_health (source, last_ok_at, last_status, consecutive_failures, updated_at)
values ($1, now(), 200, 0, now())
on conflict (source) do update set last_ok_at = now(), last_status = 200, consecutive_failures = 0,
  updated_at = now()
"""

SOURCE_FAIL = """
insert into source_health (source, last_error_at, last_error, consecutive_failures, updated_at)
values ($1, now(), $2, 1, now())
on conflict (source) do update set last_error_at = now(), last_error = $2,
  consecutive_failures = source_health.consecutive_failures + 1, updated_at = now()
"""


async def insert_quotes(pool: Any, quotes: list[Quote], chunk: int = 5000) -> int:
    """Escribe un lote; devuelve cuántas cotizaciones llegaron (no cuántas cambiaron)."""
    # Un (ítem, fuente) repetido dentro del mismo lote rompería el ON CONFLICT DO UPDATE
    # («cannot affect row a second time»): se queda la más reciente.
    uniq: dict[tuple[str, str], Quote] = {}
    for q in quotes:
        cur = uniq.get((q.market_hash_name, q.source))
        if cur is None or q.ts >= cur.ts:
            uniq[(q.market_hash_name, q.source)] = q
    rows = list(uniq.values())
    async with pool.acquire() as conn:
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            await conn.execute(INSERT_QUOTES,
                               [q.ts for q in part], [q.market_hash_name for q in part], [q.source for q in part],
                               [q.price_usd for q in part], [q.volume for q in part], [q.listings for q in part])
    return len(quotes)


async def latest_quotes(pool: Any, since: dt.datetime) -> dict[str, list[Quote]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(LATEST, since)
    out: dict[str, list[Quote]] = {}
    for r in rows:
        out.setdefault(r[1], []).append(Quote(r[0], r[1], r[2], r[3], r[4], r[5]))
    return out


async def insert_refs(pool: Any, ts: dt.datetime, refs: list[Reference]) -> None:
    rows = [(ts, r.market_hash_name, r.ref_price_usd, r.sources) for r in refs if r.ref_price_usd is not None]
    async with pool.acquire() as conn:
        await conn.executemany(INSERT_REF, rows)


async def upsert_catalog(pool: Any, items: list[CatalogItem]) -> None:
    rows = [(c.market_hash_name, c.weapon, c.category, c.rarity, c.rarity_color, c.image_url) for c in items]
    async with pool.acquire() as conn:
        await conn.executemany(UPSERT_CATALOG, rows)


async def source_ok(pool: Any, source: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SOURCE_OK, source)


async def source_fail(pool: Any, source: str, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SOURCE_FAIL, source, error[:500])
