"""Timescale del VPS (rol `worker`, esquema `worker`). asyncpg con pool pequeño."""
from typing import Any

import asyncpg


async def create_pool(dsn: str) -> Any:
    # search_path lo fija el rol (00-roles.sh); se repite por si el DSN apunta a otro rol.
    return await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=30,
                                     server_settings={"search_path": "worker,public"})


async def select_one(pool: Any) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("select 1") == 1
