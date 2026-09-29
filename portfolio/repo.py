"""
Persistencia del histórico de cartera por usuario (tabla portfolio_history, Supabase).

Un punto por día y por price provider: lo impone la PK compuesta
(steam_id, provider, date), así que guardar dos veces el mismo día es un upsert,
no una fila nueva. Es la misma regla que aplicaba el cliente en localStorage antes
de UX-16; la diferencia es que ahora sobrevive al logout y al cambio de dispositivo.

Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py.
"""
import asyncio
from datetime import date, datetime, timezone

from steam.cap_history_repo import get_supabase

_TABLE = "portfolio_history"
_COLS = "provider, date, value"


async def list_for_user(steam_id: str, provider: str) -> list[dict]:
    """Serie del usuario para un provider, de más antigua a más reciente.

    El orden ascendente es el que espera la gráfica: el cliente pinta los puntos
    en el orden en que llegan.
    """
    def _do() -> list[dict]:
        resp = (get_supabase().table(_TABLE)
                .select(_COLS)
                .eq("steam_id", steam_id)
                .eq("provider", provider)
                .order("date", desc=False)
                .execute())
        return resp.data or []

    return await asyncio.to_thread(_do)


async def upsert_today(steam_id: str, provider: str, value: float) -> dict:
    """Graba el punto de HOY, sobrescribiéndolo si ya existe.

    La fecha la pone el servidor, no el cliente: si viniera del dispositivo, un reloj
    mal puesto (o manipulado) ensuciaría la serie con fechas futuras que luego
    mandarían en el orden de la gráfica.
    """
    hoy = date.today().isoformat()

    def _do() -> dict:
        fila = {
            "steam_id": steam_id,
            "provider": provider,
            "date": hoy,
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        (get_supabase().table(_TABLE)
         .upsert(fila, on_conflict="steam_id,provider,date")
         .execute())
        # Se devuelve la forma del GET (sin steam_id): el cliente ya sabe quién es,
        # y así las dos respuestas tienen el mismo contrato.
        return {"provider": provider, "date": hoy, "value": value}

    return await asyncio.to_thread(_do)


async def delete_all_for_user(steam_id: str) -> None:
    """Borrado de cuenta (LAUNCH-04): toda la serie del usuario, en todos los providers."""
    def _do() -> None:
        get_supabase().table(_TABLE).delete().eq("steam_id", steam_id).execute()

    await asyncio.to_thread(_do)
