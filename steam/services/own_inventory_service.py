"""Inventario y perfil servidos desde lo que descarga el worker del VPS (COST-07).

Solo actúa con `INVENTORY_SOURCE` / `PROFILE_SOURCE` en `own` (y, para encolar en sombra,
`shadow`). Mantiene la máquina de estados de `inventory_service.get_inventory`:

- `ok`: snapshot fresco (< OWN_FRESH_H) o uno nuevo que llegó durante el long-poll.
- `stale`: el snapshot viejo, si la petición falla o no llega a tiempo; `reason` = el código.
- `error`: sin nada que servir; `rate_limit` (→ 429) o `http_4xx` (→ `[]`).
- inventario privado → `UpstreamError(403)`, que la ruta convierte en 403 como hoy.

Sustituye a `_retry_inventory`/backoff: el reintento y el ritmo los lleva la cola del worker.
La tarjeta se compone con `market_current` construyendo un `SteamItem` y pasándolo por el
mismo `_map_item`: el contrato con el front no cambia.
"""
import asyncio
import datetime as dt
import logging
import time
from typing import Any

from settings import INVENTORY_WAIT_S, OWN_FRESH_H, PROFILE_WAIT_S
from steam import own_source_repo as repo
from steam.cache.user_cache import _inventory_cache, _profile_cache
from steam.domain.models import Fetched, PriceQuote, ProfileData, SkinCard, SteamItem, Variant
from steam.errors import UpstreamError
from steam.errors.handling import degraded, log_degraded
from steam.mappers.item_mapper import _map_item
from steam.mappers.profile_mapper import _map_profile
from steam.utils.urls import steam_cdn_url

logger = logging.getLogger("uvicorn.error")
POLL_S = 1.0

# Nombre de mercado que ve el front en externalPrices; las ventas de Skinport cuentan como Skinport.
_MARKET_LABEL = {"skinport": "skinport", "skinport_sales": "skinport", "marketcsgo": "marketcsgo", "buff": "buff"}


def _age_h(captured_at: str) -> float:
    try:
        ts = dt.datetime.fromisoformat(captured_at)
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - ts).total_seconds() / 3600


def _steam_item(asset: dict[str, Any], row: dict[str, Any] | None) -> SteamItem:
    row = row or {}
    prices = [p for p in row.get("prices") or [] if p.get("price") is not None]
    used = [p["price"] for p in prices if p.get("used")]
    ref = row.get("ref_price_usd")
    ref = float(ref) if ref is not None else None
    pi = asset.get("paint_index")
    return SteamItem(
        id=asset.get("assetid"), asset_id=asset.get("assetid"), market_hash_name=asset["market_hash_name"],
        market_name=asset.get("name"), slug=None, weapon_type=None, item_type=asset.get("item_type"),
        item_name=asset.get("name"), image=steam_cdn_url(asset["icon_url"]) if asset.get("icon_url") else None,
        rarity=asset.get("rarity"), color=asset.get("rarity_color"), border_color=asset.get("rarity_color"),
        quality=asset.get("quality"), is_stattrak=asset.get("is_stattrak"), is_souvenir=asset.get("is_souvenir"),
        is_star=asset.get("is_star"), exterior=asset.get("exterior"), float_value=asset.get("float_value"),
        float_min=None, float_max=None, paint_index=pi,
        variants=(Variant(paint_index=pi, phase=asset["phase"]),) if pi is not None and asset.get("phase") else (),
        # `latest_price` (→ priceLatest) mira primero price_latest_sell: ahí va la referencia.
        price_latest_sell=ref, price=None, lowest_price=None, price_usd=None, price_latest=ref,
        price_median=None, price_real=ref, price_real_24h=None, price_real_7d=None, price_real_30d=None,
        price_safe=ref, price_min=min(used) if used else None, price_max=max(used) if used else None,
        prices=tuple(PriceQuote(market=_MARKET_LABEL.get(p["market"], p["market"]), price=p["price"],
                                quantity=p.get("listings") or p.get("volume")) for p in prices),
        sold_24h=row.get("volume_24h"), sold_7d=None, sold_30d=None, sold_total=None,
        offer_volume=sum(p.get("listings") or 0 for p in prices) or None,
        buy_order_volume=None, buy_order_price=None, hours_to_sold=None,
        marketable=asset.get("marketable"), tradable=asset.get("tradable"), trade_lock_days=None,
        steam_url=None)


async def compose(items: list[dict[str, Any]]) -> list[SkinCard]:
    names = sorted({i["market_hash_name"] for i in items})
    rows = await repo.market_rows(names)
    # Una tarjeta por asset, como steamwebapi: el `id` es el assetid y tiene que ser único.
    return [_map_item(_steam_item(a, rows.get(a["market_hash_name"]))) for a in items]


async def _wait(request_id: int, timeout: float) -> tuple[str, str | None]:
    """Long-poll sobre la petición: (status final o 'timeout', error)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, error = await repo.request_state(request_id)
        if status in ("done", "error"):
            return status, error
        await asyncio.sleep(POLL_S)
    return "timeout", None


def _inventory(items: list, captured_at: str | None = None) -> Any:
    from steam.services.inventory_service import Inventory   # import tardío: inventory_service nos importa
    return Inventory(items, captured_at)


async def get_inventory(steam_id: str, *, force: bool = False) -> Fetched[Any]:
    now = time.monotonic()
    if not force:
        hit = _inventory_cache.fresh(steam_id, now)
        if hit is not None:
            return Fetched(_inventory(hit))
    snap = await repo.load_snapshot(steam_id)
    if snap is not None and not force and _age_h(snap[1]) < OWN_FRESH_H:
        cards = await compose(snap[0])
        _inventory_cache.put(steam_id, cards, now)
        return Fetched(_inventory(cards))

    rid = await repo.request("inventory", steam_id, repo.PRIO_INTERACTIVE)
    status, error = await _wait(rid, INVENTORY_WAIT_S)
    if status == "done" and (fresh := await repo.load_snapshot(steam_id)) is not None:
        cards = await compose(fresh[0])
        _inventory_cache.put(steam_id, cards, time.monotonic())
        return Fetched(_inventory(cards))
    if error == "private":
        raise UpstreamError(403, "inventory is private (worker)")
    reason = error or ("refresh_pending" if status == "timeout" else "unknown")
    if snap is not None:
        log_degraded("inventory", reason, "stale")
        return Fetched(_inventory(await compose(snap[0]), snap[1]), "stale", reason)
    log_degraded("inventory", reason, "empty")
    # Sin nada que enseñar: «vuelve en un rato» es un 429 para el front (como hoy).
    return Fetched(_inventory([]), "error", "rate_limit" if not reason.startswith("http_") else reason)


async def enqueue_shadow(steam_id: str, kind: str = "inventory") -> None:
    """Modo `shadow`: pide al worker una descarga de fondo para comparar. Nunca falla la ruta."""
    try:
        await repo.request(kind, steam_id, repo.PRIO_BACKGROUND)
    except Exception as e:  # noqa: BLE001 — la sombra es best-effort; producción no puede caer por ella
        logger.warning("[inventory] shadow enqueue %s steam_id=%s error=%r", kind, steam_id, e)


def _profile_data(row: dict[str, Any]) -> ProfileData:
    return ProfileData(persona_name=row.get("persona_name"), avatar_full=row.get("avatar_full"),
                       avatar_medium=row.get("avatar_medium"), profile_url=row.get("profile_url"),
                       persona_state=row.get("persona_state"))


async def get_profile(steam_id: str) -> Fetched[dict]:
    now = time.monotonic()
    hit = _profile_cache.fresh(steam_id, now)
    if hit is not None:
        return Fetched(hit)
    row = await repo.load_profile(steam_id)
    if row is None or _age_h(row.get("updated_at") or "") >= OWN_FRESH_H:
        rid = await repo.request("profile", steam_id, repo.PRIO_INTERACTIVE)
        if (await _wait(rid, PROFILE_WAIT_S))[0] == "done":
            row = await repo.load_profile(steam_id) or row
    profile = _map_profile(_profile_data(row) if row else None, steam_id)
    if row is None:
        return degraded("profile", "empty_body", "empty", profile)
    _profile_cache.put(steam_id, profile, now)
    return Fetched(profile)
