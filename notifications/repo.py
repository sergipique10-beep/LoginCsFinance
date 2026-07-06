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


async def register_device_token(token: str, platform: str) -> None:
    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).upsert(
            {"token": token, "platform": platform}, on_conflict="token"
        ).execute()

    await asyncio.to_thread(_do)


async def list_device_tokens() -> list[str]:
    def _do() -> list[str]:
        resp = get_supabase().table(_DEVICE_TOKENS_TABLE).select("token").execute()
        return [row["token"] for row in (resp.data or [])]

    return await asyncio.to_thread(_do)


async def delete_device_tokens(tokens: list[str]) -> None:
    if not tokens:
        return

    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).delete().in_("token", tokens).execute()

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
