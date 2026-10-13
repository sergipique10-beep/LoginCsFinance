"""Escritura y lectura de precios en la Timescale del VPS (COST-03). Esquema: deploy/vps/schema.sql."""
import datetime as dt
from typing import Any

from worker.models import CatalogItem, Quote
from worker.pricing.reference import Reference

INSERT_QUOTE = """
insert into market_quotes (ts, market_hash_name, source, price_usd, volume, listings)
values ($1, $2, $3, $4, $5, $6) on conflict do nothing
"""

# La última cotización de cada (ítem, fuente) dentro de la ventana.
LATEST = """
select distinct on (market_hash_name, source)
       ts, market_hash_name, source, price_usd::float8, volume, listings
from market_quotes where ts > $1
order by market_hash_name, source, ts desc
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


async def insert_quotes(pool: Any, quotes: list[Quote]) -> int:
    rows = [(q.ts, q.market_hash_name, q.source, q.price_usd, q.volume, q.listings) for q in quotes]
    async with pool.acquire() as conn:
        await conn.executemany(INSERT_QUOTE, rows)
    return len(rows)


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
