"""Inventario en la Timescale del VPS (COST-07): inventory_assets y asset_decoded."""
from typing import Any

UPSERT_DECODED = """
insert into asset_decoded (assetid, paintwear, paintseed, paintindex, defindex, certificate, checksum_ok, decoded_at)
values ($1, $2, $3, $4, $5, $6, $7, now())
on conflict (assetid) do update set paintwear = excluded.paintwear, paintseed = excluded.paintseed,
  paintindex = excluded.paintindex, defindex = excluded.defindex, certificate = excluded.certificate,
  checksum_ok = excluded.checksum_ok, decoded_at = now()
"""

INSERT_ASSET = """
insert into inventory_assets (steam_id, assetid, classid, instanceid, market_hash_name, amount, seen_at)
values ($1, $2, $3, $4, $5, $6, now())
"""


async def upsert_decoded(pool: Any, rows: list[tuple[str, dict[str, Any], str]]) -> None:
    """rows: (assetid, decodificado, hex del certificado)."""
    args = [(aid, d.get("paintwear"), d.get("paintseed"), d.get("paintindex"), d.get("defindex"), hx,
             d.get("checksum_ok")) for aid, d, hx in rows]
    async with pool.acquire() as conn:
        await conn.executemany(UPSERT_DECODED, args)


async def replace_assets(pool: Any, steam_id: str, items: list[dict[str, Any]]) -> None:
    """El inventario actual sustituye al anterior del usuario, en una transacción."""
    args = [(steam_id, i["assetid"], i["classid"], i["instanceid"], i["market_hash_name"], i["amount"])
            for i in items]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("delete from inventory_assets where steam_id = $1", steam_id)
        await conn.executemany(INSERT_ASSET, args)


async def delete_user(pool: Any, steam_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("delete from inventory_assets where steam_id = $1", steam_id)
