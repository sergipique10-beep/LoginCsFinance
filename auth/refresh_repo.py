"""
Refresh tokens vigentes (tabla refresh_tokens, Supabase) — SEC-11.

Hasta SEC-11 los JTIs vivían en un dict en memoria: cada deploy, reinicio o
despertar de Render lo vaciaba y cerraba la sesión de todos los usuarios.

Un refresh es válido si su fila existe y no ha caducado. Revocar = borrar la fila.
`consume` borra y comprueba en una sola sentencia (DELETE ... RETURNING): si dos
peticiones presentan el mismo refresh a la vez, solo una recibe la fila, así que
la rotación de un solo uso no tiene carrera.

Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py.
"""
import asyncio
from datetime import datetime, timezone

from steam.cap_history_repo import get_supabase

_TABLE = "refresh_tokens"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def save(jti: str, steam_id: str, expires_at: datetime) -> None:
    def _do() -> None:
        get_supabase().table(_TABLE).insert({
            "jti": jti,
            "steam_id": steam_id,
            "expires_at": expires_at.isoformat(),
        }).execute()

    await asyncio.to_thread(_do)


async def consume(jti: str, steam_id: str) -> bool:
    """Borra el refresh si está vigente. True si existía (el token es válido y ya está rotado).

    De paso purga los caducados: la tabla no crece sin límite y no hace falta un cron.
    """
    def _do() -> bool:
        now = _now()
        resp = (get_supabase().table(_TABLE).delete()
                .eq("jti", jti)
                .eq("steam_id", steam_id)
                .gt("expires_at", now)
                .execute())
        get_supabase().table(_TABLE).delete().lte("expires_at", now).execute()
        return bool(resp.data)

    return await asyncio.to_thread(_do)


async def revoke(jti: str) -> None:
    def _do() -> None:
        get_supabase().table(_TABLE).delete().eq("jti", jti).execute()

    await asyncio.to_thread(_do)


async def delete_all_for_user(steam_id: str) -> None:
    """Borrado de cuenta (LAUNCH-04): cierra la sesión en todos los dispositivos."""
    def _do() -> None:
        get_supabase().table(_TABLE).delete().eq("steam_id", steam_id).execute()

    await asyncio.to_thread(_do)
