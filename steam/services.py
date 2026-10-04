import asyncio
import logging
import time
from datetime import date, timedelta

import httpx

from settings import STEAM_API_KEY
from stores import (
    ITEM_HISTORY_CACHE_TTL, IMAGE_CACHE_TTL, MARKET_LOOKUP_CACHE_TTL,
    MARKET_PROVIDERS_CACHE_TTL, FX_CACHE_TTL,
    _item_history_cache, _item_image_cache, _item_rarity_cache, _image_cache_meta,
    _market_lookup_cache, _market_providers_cache, _fx_cache, _lookup_failed_at,
)
from steam.mappers import _delta_from_history, _map_topmovers_item

logger = logging.getLogger("uvicorn.error")

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"
STEAM_MARKET_API = "https://www.steamwebapi.com/market"


def steam_auth_headers() -> dict[str, str]:
    """Cabecera de autenticación de steamwebapi (SEC-13).

    La clave va en `X-Api-Key`, nunca en la query (`?key=` es el modo legacy): una
    URL con el secreto acaba en cualquier log que registre URLs, como el INFO de
    httpx o el `str()` de sus excepciones. Solo en llamadas a steamwebapi: el
    cliente compartido también habla con GitHub, frankfurter, Leetify…
    """
    return {"X-Api-Key": STEAM_API_KEY}

# SEC-16: cuerpo del 503 cuando steamwebapi da 402 (cuota MENSUAL agotada, reset el
# día 10). No es un 429: el usuario no va «demasiado rápido» y reintentar no sirve.
# `code` es el contrato con el front (error.interceptor.ts); el texto puede cambiar.
UPSTREAM_QUOTA_DETAIL = {"code": "upstream_quota", "message": "steamwebapi monthly quota exhausted"}
# SEC-16: y cuando lo lleno es el límite POR MINUTO (20/60 s): transitorio, con Retry-After.
UPSTREAM_RATE_LIMIT_DETAIL = {"code": "upstream_rate_limit", "message": "steamwebapi per-minute limit reached"}

# Tipo de cambio: frankfurter sirve los tipos de referencia del BCE, sin clave ni
# registro. El host .app redirige 301 a .dev, asi que se apunta directo a .dev.
_FX_API = "https://api.frankfurter.dev/v1/latest"

_STATIC_SKINS_URL     = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
_STATIC_STICKERS_URL  = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/stickers.json"
_STATIC_KEYCHAINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/keychains.json"
_STATIC_KNIVES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/knives.json"
_STATIC_CRATES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json"
_STATIC_AGENTS_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/agents.json"
_STATIC_PATCHES_URL   = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/patches.json"

_WEAR_NAMES = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]

_MOVERS_LIMIT = 10


# ── Price history ─────────────────────────────────────────────────────────────

_HISTORY_EMPTY_TTL = 300  # 5 min backoff for failed/empty results to avoid retry storms


class _SlidingWindowLimiter:
    """Caps calls to at most `limit` per `window` seconds, process-wide.

    steamwebapi Starter allows 20 req/60s *per endpoint*. _enrich_prices fires
    one csfloat/history call per item (up to 80 for trending) — without this,
    everything past the 20th got HTTP 429 → empty history → priceDelta7d=None →
    "N/A" badges. Callers that exceed the window wait their turn instead of failing.
    ponytail: single global window; if inventory+trending+movers contend heavily,
    split per-endpoint limiters — but they all hit csfloat/history so one is correct.
    """

    def __init__(self, limit: int, window: float):
        self._limit = limit
        self._window = window
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self._window]
                if len(self._calls) < self._limit:
                    self._calls.append(now)
                    return
                wait = self._window - (now - self._calls[0])
            await asyncio.sleep(max(wait, 0.05))


# 18/60s leaves headroom under the real 20/60s cap for concurrent requests to the
# same endpoint (e.g. /market/prices lookups) sharing the quota.
_history_limiter = _SlidingWindowLimiter(limit=18, window=60.0)


class HistoryBusy(Exception):
    """El limiter del histórico está lleno y el llamador no puede esperar (PERF-03).

    Los crons esperan lo que haga falta (mejor tarde que perder el dato); el chat
    no: una espera de hasta 60 s dentro de una respuesta interactiva es un chat
    muerto. Cancelar `acquire()` es seguro: registra la llamada y devuelve sin
    ningún `await` entre medias, así que una cancelación durante la espera no deja
    un hueco fantasma en la ventana.
    """


async def _fetch_history_for_item(
    client: httpx.AsyncClient, name: str, *, limiter_timeout: float | None = None,
) -> list:
    cache_key = f"{name}:csfloat:35d"
    now = time.monotonic()
    cached = _item_history_cache.get(cache_key)
    if cached:
        ttl = _HISTORY_EMPTY_TTL if not cached[0] else ITEM_HISTORY_CACHE_TTL
        if now - cached[1] < ttl:
            return cached[0]
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
        resp = await client.get(
            f"{STEAM_MARKET_API}/csfloat/history",
            headers=steam_auth_headers(),
            params={
                "market_hash_name": name,
                "start_date": (today - timedelta(days=35)).isoformat(),
                "end_date": today.isoformat(),
            },
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.warning("[item-history] %s → HTTP %s: %s", name, resp.status_code, resp.text[:200])
            _item_history_cache[cache_key] = ([], now)
            return []
        raw = resp.json()
        if not isinstance(raw, list):
            logger.warning("[item-history] %s → unexpected format: %s", name, str(raw)[:200])
            _item_history_cache[cache_key] = ([], now)
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
        _item_history_cache[cache_key] = (pts, now)
        return pts
    except HistoryBusy:
        raise
    except Exception as exc:
        logger.warning("[item-history] %s → exception: %s", name, exc)
        _item_history_cache[cache_key] = ([], now)
        return []


async def _enrich_prices(
    client: httpx.AsyncClient, items: list, concurrency: int = 5, *, limiter_timeout: float | None = None,
) -> list:
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


def _enrich_images_from_cache(items: list) -> None:
    if not _item_image_cache:
        return
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


_IMAGE_EMPTY_TTL = 300  # 5 min de backoff si fallan todas las fuentes, como _HISTORY_EMPTY_TTL


# PERF-18: una sola recarga a la vez. Sin él, N peticiones con la caché caducada
# descargaban N veces los siete JSON (estampida tras despertar Render).
_image_cache_lock = asyncio.Lock()


def _image_cache_fresh(now: float) -> bool:
    # time.monotonic() arranca en el uptime del sistema, no en 0. Usar 0.0 como
    # "nunca cargado" hacía que `now - 0.0 < TTL` fuese True en cualquier equipo
    # con <23h de uptime → retornaba sin poblar el cache. Centinela None explícito.
    last_ts = _image_cache_meta.get("ts")
    if last_ts is not None and now - last_ts < IMAGE_CACHE_TTL:
        return True
    # CAL-08: tras un fallo total, backoff corto en vez de reintentar en cada petición.
    failed_ts = _image_cache_meta.get("failed_ts")
    return failed_ts is not None and now - failed_ts < _IMAGE_EMPTY_TTL


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
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                logger.warning("[image-cache] %s returned %s", label, resp.status_code)
                continue
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("[image-cache] %s unexpected format: %s", label, type(data).__name__)
                continue
            for item in data:
                _register_skin(item)
            fetched[label] = len(data)
        except Exception as exc:
            logger.warning("[image-cache] could not fetch %s: %s", label, exc)

    for label, url in sources_flat:
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                logger.warning("[image-cache] %s returned %s", label, resp.status_code)
                continue
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("[image-cache] %s unexpected format: %s", label, type(data).__name__)
                continue
            for item in data:
                _register_flat(item)
            fetched[label] = len(data)
        except Exception as exc:
            logger.warning("[image-cache] could not fetch %s: %s", label, exc)

    # CAL-08: `ts` significa "última carga buena" (stores.py). Si no cargó ninguna
    # fuente (GitHub caído en el arranque de Render), no se estampa: se reintenta
    # pasado _IMAGE_EMPTY_TTL en vez de pasar 23 h con `image: ""`.
    if not fetched:
        _image_cache_meta["failed_ts"] = now
        logger.warning("[image-cache] all sources failed; retry in %ds", _IMAGE_EMPTY_TTL)
        return
    _image_cache_meta["ts"] = now
    _image_cache_meta.pop("failed_ts", None)
    logger.info(
        "[image-cache] loaded %d total entries (%+d new) — sources: %s",
        len(_item_image_cache),
        len(_item_image_cache) - total_before,
        fetched,
    )


# ── Movers ────────────────────────────────────────────────────────────────────

# ── Multi-market price lookup ─────────────────────────────────────────────────

_TRACKED_MARKETS = ("csfloat", "buff")


# PERF-17: tras un fallo, no se reintenta durante 5 min (como _HISTORY_EMPTY_TTL).
# Sin esto, cada inventario/movers/búsqueda con la fuente caída repetía dos lookups
# condenados a fallar, gastando cuota y hasta 30 s de timeout.
_LOOKUP_FAIL_TTL = 300


def _in_fail_backoff(key: str, now: float) -> bool:
    failed = _lookup_failed_at.get(key)
    return failed is not None and now - failed < _LOOKUP_FAIL_TTL


async def _fetch_market_price_lookup(client: httpx.AsyncClient, market: str) -> dict[str, float]:
    now = time.monotonic()
    cached = _market_lookup_cache.get(market)
    if cached and now - cached[1] < MARKET_LOOKUP_CACHE_TTL:
        return cached[0]
    fail_key = f"lookup:{market}"
    if _in_fail_backoff(fail_key, now):
        return (cached[0] if cached else {})
    try:
        resp = await client.get(
            f"{STEAM_MARKET_API}/{market}/prices",
            headers=steam_auth_headers(),
            params={"format": "json"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.warning("[market-lookup] %s returned %s", market, resp.status_code)
            _lookup_failed_at[fail_key] = now
            return (cached[0] if cached else {})
        data = resp.json()
        if not isinstance(data, list):
            _lookup_failed_at[fail_key] = now
            return (cached[0] if cached else {})
        lookup: dict[str, float] = {}
        for item in data:
            name = item.get("market_hash_name") or item.get("markethashname") or item.get("name")
            price = item.get("price") or item.get("value") or 0
            if name and price:
                lookup[name] = float(price)
        _market_lookup_cache[market] = (lookup, now)
        _lookup_failed_at.pop(fail_key, None)
        logger.info("[market-lookup] %s: %d prices loaded", market, len(lookup))
        return lookup
    except Exception as exc:
        logger.warning("[market-lookup] could not fetch %s: %s", market, exc)
        _lookup_failed_at[fail_key] = now
        return (cached[0] if cached else {})


async def _enrich_market_prices(client: httpx.AsyncClient, items: list) -> list:
    csfloat_lookup, buff_lookup = await asyncio.gather(
        _fetch_market_price_lookup(client, "csfloat"),
        _fetch_market_price_lookup(client, "buff"),
    )
    for item in items:
        name = item.get("name", "")
        item["csfloatPrice"] = csfloat_lookup.get(name) or None
        item["buffPrice"] = buff_lookup.get(name) or None
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
    cached = _fx_cache.get("usdeur")
    if cached and now - cached[1] < FX_CACHE_TTL:
        return cached[0], True
    try:
        resp = await client.get(
            _FX_API,
            params={"base": "USD", "symbols": "EUR"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("[fx] frankfurter returned %s", resp.status_code)
            return (cached[0], False) if cached else (None, False)
        rate = (resp.json().get("rates") or {}).get("EUR")
        # Un tipo USD/EUR fuera de este rango es un error de la fuente, no un
        # movimiento de mercado: mejor servir el ultimo bueno que corromper precios.
        if not isinstance(rate, (int, float)) or not 0.5 < rate < 2.0:
            logger.warning("[fx] tasa implausible: %r", rate)
            return (cached[0], False) if cached else (None, False)
        _fx_cache["usdeur"] = (float(rate), now)
        logger.info("[fx] USD/EUR = %s", rate)
        return float(rate), True
    except Exception as exc:
        logger.warning("[fx] failed: %s", exc)
        return (cached[0], False) if cached else (None, False)


async def _fetch_market_providers(client: httpx.AsyncClient) -> list[dict]:
    now = time.monotonic()
    cached = _market_providers_cache.get("providers")
    if cached and now - cached[1] < MARKET_PROVIDERS_CACHE_TTL:
        return cached[0]
    # PERF-17: con la fuente caída, el último dato bueno si existe; si no, el respaldo.
    stale = cached[0] if cached else _FALLBACK_PROVIDERS
    if _in_fail_backoff("providers", now):
        return stale
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/info/markets",
            headers=steam_auth_headers(),
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning("[market-providers] info/markets returned %s", resp.status_code)
            _lookup_failed_at["providers"] = now
            return stale
        data = resp.json()
        if not isinstance(data, list):
            _lookup_failed_at["providers"] = now
            return stale

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

        _market_providers_cache["providers"] = (providers, now)
        _lookup_failed_at.pop("providers", None)
        logger.info("[market-providers] loaded %d providers", len(providers))
        return providers
    except Exception as exc:
        logger.warning("[market-providers] failed: %s", exc)
        _lookup_failed_at["providers"] = now
        return stale


# ── Movers ────────────────────────────────────────────────────────────────────

def _build_movers_from_topmovers(gainers: list, losers: list) -> dict | None:
    if not gainers and not losers:
        return None
    def _is_slab(raw: dict) -> bool:
        name = (raw.get("marketname") or raw.get("markethashname") or "").lower()
        return "sticker slab" in name
    hot  = [_map_topmovers_item(g) for g in gainers if not _is_slab(g)][:_MOVERS_LIMIT]
    cold = [_map_topmovers_item(l) for l in losers  if not _is_slab(l)][:_MOVERS_LIMIT]
    logger.info("[market-movers] topmovers raw: gainers=%d losers=%d | after_filter: hot=%d cold=%d",
                len(gainers), len(losers), len(hot), len(cold))
    hot  = sorted(hot,  key=lambda x: x["_change24h"], reverse=True)
    cold = sorted(cold, key=lambda x: x["_change24h"])
    for item in hot + cold:
        del item["_change24h"]
    return {"hot": hot, "cold": cold}
