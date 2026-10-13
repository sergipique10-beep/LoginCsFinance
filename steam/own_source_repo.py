"""Supabase para el inventario y el perfil que descarga el worker del VPS (COST-07).

Render no habla con Steam en este camino: pide (`fetch_requests`), espera (long-poll sobre la
petición) y lee lo que el worker dejó (`OWN_INVENTORY_TABLE`, `steam_profiles`,
`market_current`). DDL en docs/sql/{fetch_requests,steam_profiles,inventory_snapshots_shadow,
market_current}.sql. Mismo cliente y traducción de errores que el resto (`storage_call`).
"""
from typing import Any, cast

from settings import MARKET_CURRENT_TABLE, OWN_INVENTORY_TABLE
from steam.cap_history_repo import get_supabase, storage_call

PRIO_INTERACTIVE, PRIO_ALERT, PRIO_BACKGROUND = 0, 1, 2


def _rows(resp: Any) -> list[dict[str, Any]]:
    """supabase-py tipa `data` como JSON genérico; aquí siempre es una lista de filas."""
    return cast(list[dict[str, Any]], resp.data or [])


async def request(kind: str, steam_id: str, prio: int) -> int:
    def _do() -> int:
        resp = get_supabase().table("fetch_requests").insert(
            {"kind": kind, "steam_id": steam_id, "prio": prio}).execute()
        return int(_rows(resp)[0]["id"])
    return await storage_call(_do)


async def request_state(request_id: int) -> tuple[str, str | None]:
    """(status, error) de una petición: pending | queued | done | error."""
    def _do() -> tuple[str, str | None]:
        resp = get_supabase().table("fetch_requests").select("status, error").eq("id", request_id).limit(1).execute()
        row = (_rows(resp) or [{"status": "pending", "error": None}])[0]
        return row["status"], row.get("error")
    return await storage_call(_do)


async def load_snapshot(steam_id: str) -> tuple[list, str] | None:
    def _do() -> tuple[list, str] | None:
        resp = (get_supabase().table(OWN_INVENTORY_TABLE).select("items, captured_at")
                .eq("steam_id", steam_id).limit(1).execute())
        rows = _rows(resp)
        return (rows[0]["items"], rows[0]["captured_at"]) if rows else None
    return await storage_call(_do)


async def load_profile(steam_id: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        resp = get_supabase().table("steam_profiles").select("*").eq("steam_id", steam_id).limit(1).execute()
        rows = _rows(resp)
        return rows[0] if rows else None
    return await storage_call(_do)


async def market_rows(names: list[str], chunk: int = 200) -> dict[str, dict[str, Any]]:
    """{market_hash_name: fila de market_current} para los nombres pedidos."""
    def _do() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(names), chunk):
            resp = (get_supabase().table(MARKET_CURRENT_TABLE).select("*")
                    .in_("market_hash_name", names[i:i + chunk]).execute())
            out.update({r["market_hash_name"]: r for r in _rows(resp)})
        return out
    return await storage_call(_do) if names else {}


async def delete_for_user(steam_id: str) -> None:
    """DELETE /me: lo del worker con SteamID. inventory_assets vive en el VPS: se pide su
    borrado con una petición `delete` (la fila queda cerrada y se purga con las demás)."""
    def _do() -> None:
        sb = get_supabase()
        sb.table("fetch_requests").delete().eq("steam_id", steam_id).execute()
        sb.table("steam_profiles").delete().eq("steam_id", steam_id).execute()
        sb.table(OWN_INVENTORY_TABLE).delete().eq("steam_id", steam_id).execute()
        sb.table("fetch_requests").insert({"kind": "delete", "steam_id": steam_id, "prio": 0}).execute()
    await storage_call(_do)
