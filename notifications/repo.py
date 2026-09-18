"""
Persistencia de push notifications en Supabase: tokens de dispositivo (FCM)
y noticias CS2 ya notificadas (dedup para el cron de news-tick).

Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py — mismo
proyecto Supabase, no hace falta un segundo cliente.
"""
import asyncio

from steam.cap_history_repo import get_supabase

_DEVICE_TOKENS_TABLE = "device_tokens"
_NOTIFIED_NEWS_TABLE = "notified_news"


async def register_device_token(token: str, platform: str, steam_id: str) -> None:
    """Upsert por token. El steam_id se pisa a propósito: si el mismo
    dispositivo cambia de cuenta Steam, las push personalizadas van al dueño
    actual, no al anterior."""
    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).upsert(
            {"token": token, "platform": platform, "steam_id": steam_id}, on_conflict="token"
        ).execute()

    await asyncio.to_thread(_do)


async def list_device_tokens() -> list[str]:
    def _do() -> list[str]:
        resp = get_supabase().table(_DEVICE_TOKENS_TABLE).select("token").execute()
        return [row["token"] for row in (resp.data or [])]

    return await asyncio.to_thread(_do)


async def list_device_tokens_for(steam_ids: list[str]) -> dict[str, list[str]]:
    """Tokens agrupados por steam_id. Un usuario sin dispositivos no aparece
    en el dict; los tokens sin steam_id (registrados antes de PUSH-06) nunca
    reciben push personalizadas."""
    if not steam_ids:
        return {}

    def _do() -> dict[str, list[str]]:
        resp = (
            get_supabase()
            .table(_DEVICE_TOKENS_TABLE)
            .select("token, steam_id")
            .in_("steam_id", steam_ids)
            .execute()
        )
        grouped: dict[str, list[str]] = {}
        for row in resp.data or []:
            grouped.setdefault(row["steam_id"], []).append(row["token"])
        return grouped

    return await asyncio.to_thread(_do)


async def delete_device_tokens(tokens: list[str]) -> None:
    if not tokens:
        return

    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).delete().in_("token", tokens).execute()

    await asyncio.to_thread(_do)


async def delete_device_token(token: str) -> None:
    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).delete().eq("token", token).execute()

    await asyncio.to_thread(_do)


async def filter_new_news_gids(gids: list[str]) -> list[str]:
    """Returns the subset of gids NOT already present in notified_news."""
    if not gids:
        return []

    def _do() -> list[str]:
        resp = (
            get_supabase()
            .table(_NOTIFIED_NEWS_TABLE)
            .select("gid")
            .in_("gid", gids)
            .execute()
        )
        already_notified = {row["gid"] for row in (resp.data or [])}
        return [g for g in gids if g not in already_notified]

    return await asyncio.to_thread(_do)


async def mark_news_notified(gids: list[str]) -> None:
    if not gids:
        return

    def _do() -> None:
        rows = [{"gid": g} for g in gids]
        get_supabase().table(_NOTIFIED_NEWS_TABLE).upsert(rows, on_conflict="gid").execute()

    await asyncio.to_thread(_do)
