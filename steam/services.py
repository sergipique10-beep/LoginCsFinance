import asyncio
import logging
import time
from datetime import date, timedelta

import httpx

from stores import (
    HISTORY_EMPTY_TTL, IMAGE_FAIL_TTL,
    _item_history_cache, _item_image_cache, _item_rarity_cache, _image_cache_meta,
    _market_lookup_cache, _market_providers_cache, _fx_cache,
)
from steam.clients import fx, static_catalog, steamwebapi
from steam.clients.steamwebapi import _history_limiter
from steam.errors import HistoryBusy, InvalidPayload, SourceTimeout, SourceUnavailable, UpstreamError
from steam.mappers.items import _delta_from_history

logger = logging.getLogger("uvicorn.error")

# SEC-16: cuerpo del 503 cuando steamwebapi da 402 (cuota MENSUAL agotada, reset el
# día 10). No es un 429: el usuario no va «demasiado rápido» y reintentar no sirve.
# `code` es el contrato con el front (error.interceptor.ts); el texto puede cambiar.
UPSTREAM_QUOTA_DETAIL = {"code": "upstream_quota", "message": "steamwebapi monthly quota exhausted"}
# SEC-16: y cuando lo lleno es el límite POR MINUTO (20/60 s): transitorio, con Retry-After.
UPSTREAM_RATE_LIMIT_DETAIL = {"code": "upstream_rate_limit", "message": "steamwebapi per-minute limit reached"}


_STATIC_SKINS_URL     = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
_STATIC_STICKERS_URL  = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/stickers.json"
_STATIC_KEYCHAINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/keychains.json"
_STATIC_KNIVES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/knives.json"
_STATIC_CRATES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json"
_STATIC_AGENTS_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/agents.json"
_STATIC_PATCHES_URL   = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/patches.json"

_WEAR_NAMES = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]


# ── Price history ─────────────────────────────────────────────────────────────

async def _fetch_history_for_item(
    client: httpx.AsyncClient, name: str, *, limiter_timeout: float | None = None,
) -> list:
    cache_key = f"{name}:csfloat:35d"
    now = time.monotonic()
    # Un vacío (fallo o sin datos) vale 5 min, no 23 h: evita tormentas de reintentos
    # sin dejar fuera durante un día una skin que vuelve a tener histórico.
    hit = _item_history_cache.fresh(cache_key, now, empty_ttl=HISTORY_EMPTY_TTL)
    if hit is not None:
        return hit
    try:
        if limiter_timeout is None:
            await _history_limiter.acquire()
        else:
            try:
                await asyncio.wait_for(_history_limiter.acquire(), timeout=limiter_timeout)
            except asyncio.TimeoutError:
                # Sin cachear: un vacío "por saturación" no es un dato.
                raise HistoryBusy(name) from None
        now = time.monotonic()  # limiter may have blocked; refresh for cache stamps
        today = date.today()
        try:
            raw = await steamwebapi.market_history(
                client, "csfloat", name,
                (today - timedelta(days=35)).isoformat(), today.isoformat(),
                timeout=30.0,
            )
        except (SourceTimeout, SourceUnavailable):
            raise   # red: lo registra el except genérico de abajo, como antes
        except UpstreamError as exc:
            logger.warning("[item-history] %s → HTTP %s: %s", name, exc.status, exc.body_excerpt[:200])
            _item_history_cache.put(cache_key, [], now)
            return []
        if not isinstance(raw, list):
            logger.warning("[item-history] %s → unexpected format: %s", name, str(raw)[:200])
            _item_history_cache.put(cache_key, [], now)
            return []
        pts = sorted(
            [
                {
                    "date":   p.get("createdat", "")[:10],
                    "price":  float(p.get("price") or 0),
                    "volume": int(p.get("quantity") or 0),
                }
                for p in raw if p.get("price")
            ],
            key=lambda p: p["date"],
        )
        logger.info("[item-history] %s → %d points (csfloat)", name, len(pts))
        _item_history_cache.put(cache_key, pts, now)
        return pts
    except HistoryBusy:
        raise
    except Exception as exc:
        logger.warning("[item-history] %s → exception: %s", name, exc)
        _item_history_cache.put(cache_key, [], now)
        return []


async def _enrich_prices(
    client: httpx.AsyncClient, items: list, concurrency: int = 5, *, limiter_timeout: float | None = None,
) -> list:
    """Deltas 24h/7d/30d desde el histórico de CSFloat. **No muta** la entrada:
    devuelve una lista nueva con dicts nuevos para los items con histórico, y el
    mismo dict, intacto, para los que no tienen (/internal/enrich-tick lo usa para
    distinguir "sin datos" de "con deltas").
    """
    sem = asyncio.Semaphore(concurrency)

    async def fetch(name: str):
        async with sem:
            return await _fetch_history_for_item(client, name, limiter_timeout=limiter_timeout)

    histories = await asyncio.gather(*[fetch(it["name"]) for it in items])
    result = []
    for item, pts in zip(items, histories):
        if pts:
            # Use the most recent point in the CSFloat history as "current" price so
            # we compare CSFloat vs CSFloat (same market). Using priceLatest (Steam)
            # vs CSFloat history produces misleading cross-market deltas.
            latest = pts[-1]["price"]
            item = {
                **item,
                "priceDelta24h": _delta_from_history(pts, 1, latest),
                "priceDelta7d":  _delta_from_history(pts, 7, latest),
                "priceDelta30d": _delta_from_history(pts, 30, latest),
            }
        result.append(item)
    return result


# ── Image cache ───────────────────────────────────────────────────────────────

def _cache_images(raw_items: list) -> None:
    for raw in raw_items:
        img = raw.get("image", "")
        if not img:
            continue
        for key in (raw.get("markethashname"), raw.get("marketname")):
            if key:
                _item_image_cache[key] = img


def _enrich_images_from_cache(items: list) -> list:
    """Rellena `image` desde el catálogo estático. **Muta** los items en sitio y
    devuelve la misma lista (CLEAN-05: mismo contrato que _enrich_market_prices)."""
    if not _item_image_cache:
        return items
    for item in items:
        if not item.get("image"):
            name = item.get("name", "")
            img = _item_image_cache.get(name, "")
            # StatTrak variants share the same skin image as their base version.
            # Removing "StatTrak™ " covers all cases in one step:
            #   "★ StatTrak™ X (wear)" → "★ X (wear)"  (knife, image from API)
            #   "StatTrak™ X (wear)"   → "X (wear)"     (weapon, image from API or ByMykel)
            if not img and "StatTrak™ " in name:
                img = _item_image_cache.get(name.replace("StatTrak™ ", "", 1), "")
            # Non-StatTrak "★ X": ByMykel stores knives without the star prefix.
            if not img and name.startswith("★ "):
                img = _item_image_cache.get(name[2:], "")
            # Souvenir items: try the base skin name without the "Souvenir " prefix.
            if not img and name.startswith("Souvenir "):
                item_type = (item.get("itemType") or "").lower()
                if "charm" not in item_type:
                    img = _item_image_cache.get(name[len("Souvenir "):], "")
            item["image"] = img
    return items


def _rarity_of(item: dict) -> tuple[str, str] | None:
    """(rareza, color hex sin '#') de un ítem del catálogo estático, o None si no la trae."""
    rarity = item.get("rarity") or {}
    name, color = rarity.get("name"), (rarity.get("color") or "").lstrip("#")
    return (name, color) if name and color else None


def _register_keys(keys: list[str], item: dict, image: str) -> None:
    rarity = _rarity_of(item)
    for key in keys:
        _item_image_cache[key] = image
        if rarity:
            _item_rarity_cache[key] = rarity


def _register_skin(item: dict) -> None:
    name = item.get("name", "")
    image = item.get("image", "")
    if not name or not image:
        return
    wears = [w.get("name", "") for w in item.get("wears", []) if w.get("name")]
    if not wears:
        wears = _WEAR_NAMES
    bases = [name, f"★ {name}"]
    if item.get("stattrak"):
        bases += [f"StatTrak™ {name}", f"★ StatTrak™ {name}"]
    keys = bases + [f"{base} ({wear})" for base in bases for wear in wears]
    _register_keys(keys, item, image)


def _register_flat(item: dict) -> None:
    image = item.get("image", "")
    if not image:
        return
    keys = [item.get(field, "") for field in ("market_hash_name", "name")]
    _register_keys([k for k in keys if k], item, image)


def _rarity_from_cache(name: str) -> tuple[str, str] | None:
    """Rareza de un market_hash_name según el catálogo estático (UX-39). Sirve para
    payloads que no la traen, como topmovers. None si el ítem no está en el catálogo
    (p. ej. sticker slabs): quien pinta decide qué hacer sin ella."""
    found = _item_rarity_cache.get(name)
    if not found and name.startswith("Souvenir "):
        found = _item_rarity_cache.get(name[len("Souvenir "):])
    return found


# PERF-18: una sola recarga a la vez. Sin él, N peticiones con la caché caducada
# descargaban N veces los siete JSON (estampida tras despertar Render).
_image_cache_lock = asyncio.Lock()


def _image_cache_fresh(now: float) -> bool:
    # CAL-08: tras un fallo total, backoff corto en vez de reintentar en cada petición.
    return (_image_cache_meta.fresh("catalog", now) is not None
            or _image_cache_meta.in_backoff("catalog", now))


async def _fetch_static_images(client: httpx.AsyncClient) -> None:
    if _image_cache_fresh(time.monotonic()):
        return
    async with _image_cache_lock:
        # Doble comprobación: quien tenía el lock pudo recargar mientras esperábamos.
        # Una cancelación a media descarga suelta el lock (async with) y no deja
        # `ts` ni `failed_ts`, que solo se escriben al final: el siguiente reintenta.
        now = time.monotonic()
        if _image_cache_fresh(now):
            return
        await _load_static_images(client, now)


def _log_catalog_failure(label: str, exc: Exception) -> None:
    """Una fuente del catálogo falló: se salta y se registra."""
    if isinstance(exc, InvalidPayload):
        logger.warning("[image-cache] %s unexpected format: %s", label, exc.body_excerpt)
    elif isinstance(exc, UpstreamError) and exc.status is not None:
        logger.warning("[image-cache] %s returned %s", label, exc.status)
    else:
        logger.warning("[image-cache] could not fetch %s: %s", label, exc)


async def _load_static_images(client: httpx.AsyncClient, now: float) -> None:
    sources_with_wears = [
        ("skins",  _STATIC_SKINS_URL),
        ("knives", _STATIC_KNIVES_URL),
    ]
    sources_flat = [
        ("stickers",  _STATIC_STICKERS_URL),
        ("keychains", _STATIC_KEYCHAINS_URL),
        ("crates",    _STATIC_CRATES_URL),
        ("agents",    _STATIC_AGENTS_URL),
        ("patches",   _STATIC_PATCHES_URL),
    ]

    total_before = len(_item_image_cache)
    fetched: dict[str, int] = {}

    for label, url in sources_with_wears:
        try:
            data = await static_catalog.fetch_source(client, url)
            for item in data:
                _register_skin(item)
            fetched[label] = len(data)
        except Exception as exc:
            _log_catalog_failure(label, exc)

    for label, url in sources_flat:
        try:
            data = await static_catalog.fetch_source(client, url)
            for item in data:
                _register_flat(item)
            fetched[label] = len(data)
        except Exception as exc:
            _log_catalog_failure(label, exc)

    # CAL-08: "catalog" significa "última carga buena" (stores.py). Si no cargó ninguna
    # fuente (GitHub caído en el arranque de Render), no se estampa: se reintenta
    # pasado IMAGE_FAIL_TTL en vez de pasar 23 h con `image: ""`.
    if not fetched:
        _image_cache_meta.mark_failed("catalog", now)
        logger.warning("[image-cache] all sources failed; retry in %ds", IMAGE_FAIL_TTL)
        return
    _image_cache_meta.put("catalog", len(_item_image_cache), now)
    logger.info(
        "[image-cache] loaded %d total entries (%+d new) — sources: %s",
        len(_item_image_cache),
        len(_item_image_cache) - total_before,
        fetched,
    )


# ── Multi-market price lookup ─────────────────────────────────────────────────

_TRACKED_MARKETS = ("csfloat", "buff")


# PERF-17: tras un fallo, no se reintenta durante LOOKUP_FAIL_TTL (5 min). Sin esto,
# cada inventario/movers/búsqueda con la fuente caída repetía dos lookups condenados
# a fallar, gastando cuota y hasta 30 s de timeout. El backoff vive en la propia caché
# (`mark_failed`), aparte del último dato bueno.


def _lookup_stale(market: str) -> dict[str, float]:
    stale = _market_lookup_cache.stale(market)
    return stale if stale is not None else {}


async def _fetch_market_price_lookup(client: httpx.AsyncClient, market: str) -> dict[str, float]:
    now = time.monotonic()
    hit = _market_lookup_cache.fresh(market, now)
    if hit is not None:
        return hit
    if _market_lookup_cache.in_backoff(market, now):
        return _lookup_stale(market)
    try:
        try:
            data = await steamwebapi.market_prices(client, market, {"format": "json"}, timeout=30.0)
        except (SourceTimeout, SourceUnavailable):
            raise
        except UpstreamError as exc:
            logger.warning("[market-lookup] %s returned %s", market, exc.status)
            _market_lookup_cache.mark_failed(market, now)
            return _lookup_stale(market)
        if not isinstance(data, list):
            _market_lookup_cache.mark_failed(market, now)
            return _lookup_stale(market)
        lookup: dict[str, float] = {}
        for item in data:
            name = item.get("market_hash_name") or item.get("markethashname") or item.get("name")
            price = item.get("price") or item.get("value") or 0
            if name and price:
                lookup[name] = float(price)
        _market_lookup_cache.put(market, lookup, now)
        logger.info("[market-lookup] %s: %d prices loaded", market, len(lookup))
        return lookup
    except Exception as exc:
        logger.warning("[market-lookup] could not fetch %s: %s", market, exc)
        _market_lookup_cache.mark_failed(market, now)
        return _lookup_stale(market)


async def _enrich_market_prices(client: httpx.AsyncClient, items: list) -> list:
    """Añade `<market>Price` por cada mercado de _TRACKED_MARKETS. **Muta** los
    items en sitio y devuelve la misma lista."""
    lookups = await asyncio.gather(*(_fetch_market_price_lookup(client, m) for m in _TRACKED_MARKETS))
    for item in items:
        name = item.get("name", "")
        for market, lookup in zip(_TRACKED_MARKETS, lookups, strict=True):
            item[f"{market}Price"] = lookup.get(name) or None   # csfloatPrice, buffPrice
    return items


_STEAM_FAVICON = "https://store.steampowered.com/favicon.ico"

_PROVIDER_IDS = {"csfloat", "buff"}

# Known public logos used as fallback when the API doesn't return them
_KNOWN_LOGOS: dict[str, str] = {
    "steam":   _STEAM_FAVICON,
    "csfloat": "https://csfloat.com/favicon.ico",
    "buff":    "https://buff.163.com/favicon.ico",
}

_FALLBACK_PROVIDERS = [
    {"id": "steam",   "name": "Steam",   "logoUrl": _KNOWN_LOGOS["steam"]},
    {"id": "csfloat", "name": "CSFloat", "logoUrl": _KNOWN_LOGOS["csfloat"]},
    {"id": "buff",    "name": "Buff163", "logoUrl": _KNOWN_LOGOS["buff"]},
]


async def _fetch_fx_rate(client: httpx.AsyncClient) -> tuple[float | None, bool]:
    """USD→EUR del BCE, cacheado 24 h. Devuelve (tasa, es_fresca).

    El backend no convierte nada: solo sirve el numero (UX-08). Si la fuente cae se
    reutiliza el ultimo valor conocido marcado como stale, para que el cliente pueda
    avisar en vez de convertir con una tasa fantasma. Sin valor previo → (None, False)
    y el cliente se queda en USD.
    """
    now = time.monotonic()
    hit = _fx_cache.fresh("usdeur", now)
    if hit is not None:
        return hit, True
    try:
        try:
            data = await fx.latest_usd_eur(client)
        except (SourceTimeout, SourceUnavailable):
            raise   # red: lo registra el except genérico de abajo, como antes
        except UpstreamError as exc:
            logger.warning("[fx] frankfurter returned %s", exc.status)
            return _fx_cache.stale("usdeur"), False
        rate = (data.get("rates") or {}).get("EUR")
        # Un tipo USD/EUR fuera de este rango es un error de la fuente, no un
        # movimiento de mercado: mejor servir el ultimo bueno que corromper precios.
        if not isinstance(rate, (int, float)) or not 0.5 < rate < 2.0:
            logger.warning("[fx] tasa implausible: %r", rate)
            return _fx_cache.stale("usdeur"), False
        _fx_cache.put("usdeur", float(rate), now)
        logger.info("[fx] USD/EUR = %s", rate)
        return float(rate), True
    except Exception as exc:
        logger.warning("[fx] failed: %s", exc)
        return _fx_cache.stale("usdeur"), False


def _providers_stale() -> list[dict]:
    """PERF-17: con la fuente caída, el último dato bueno si existe; si no, el respaldo."""
    last = _market_providers_cache.stale("providers")
    return last if last is not None else _FALLBACK_PROVIDERS


async def _fetch_market_providers(client: httpx.AsyncClient) -> list[dict]:
    now = time.monotonic()
    hit = _market_providers_cache.fresh("providers", now)
    if hit is not None:
        return hit
    if _market_providers_cache.in_backoff("providers", now):
        return _providers_stale()
    try:
        try:
            data = await steamwebapi.info_markets(client)
        except (SourceTimeout, SourceUnavailable):
            raise
        except UpstreamError as exc:
            logger.warning("[market-providers] info/markets returned %s", exc.status)
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale()
        if not isinstance(data, list):
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale()

        if data:
            logger.info("[market-providers] sample keys: %s", list(data[0].keys()))

        lookup: dict[str, dict] = {}
        for m in data:
            mid = (m.get("id") or m.get("key") or m.get("name") or "").lower()
            if mid in _PROVIDER_IDS:
                api_logo = (
                    m.get("logo") or m.get("logoUrl") or m.get("logo_url") or
                    m.get("image") or m.get("imageUrl") or m.get("image_url") or
                    m.get("icon") or m.get("iconUrl") or m.get("icon_url") or
                    m.get("thumbnail") or ""
                )
                lookup[mid] = {
                    "id":      mid,
                    "name":    m.get("name") or mid.capitalize(),
                    "logoUrl": api_logo or _KNOWN_LOGOS.get(mid, ""),
                }

        providers = [{"id": "steam", "name": "Steam", "logoUrl": _STEAM_FAVICON}]
        for pid in ("csfloat", "buff"):
            providers.append(lookup.get(pid) or next(f for f in _FALLBACK_PROVIDERS if f["id"] == pid))

        _market_providers_cache.put("providers", providers, now)
        logger.info("[market-providers] loaded %d providers", len(providers))
        return providers
    except Exception as exc:
        logger.warning("[market-providers] failed: %s", exc)
        _market_providers_cache.mark_failed("providers", now)
        return _providers_stale()

