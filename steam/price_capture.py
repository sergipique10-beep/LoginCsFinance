"""Seed + captura diaria de precios por-skin.

seed_tracked(): siembra tracked_skins desde el JSON curado si está vacía.
capture(): recorre las skins seguidas (menos-recientemente-capturadas primero,
hasta PRICE_LOOKUP_CAP), hace lookup por-nombre en steamwebapi /item vía el
limiter compartido, y hace upsert del snapshot del día. Best-effort: un fallo
por skin no aborta la corrida.
"""
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from settings import STEAM_API_KEY, PRICE_LOOKUP_CAP
from steam.services import STEAM_WEB_API, _history_limiter
from steam import price_history_repo as repo

logger = logging.getLogger("uvicorn.error")

_SEED_PATH = Path(__file__).parent / "data" / "tracked_seed.json"
_LOOKUP_TIMEOUT = 20.0


def _canonical_price(item: dict) -> float | None:
    """Precio canónico: pricelatestsell → pricelatest → pricemedian (primero > 0)."""
    for key in ("pricelatestsell", "pricelatest", "pricemedian"):
        try:
            v = float(item.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return None


def _load_seed() -> list[str]:
    return json.loads(_SEED_PATH.read_text(encoding="utf-8"))


async def seed_tracked() -> int:
    """Registra el seed curado si tracked_skins está vacía. Devuelve nº registrado."""
    if await repo.count_tracked() > 0:
        return 0
    names = _load_seed()
    await repo.register_tracked(names, "top_n")
    logger.info("[price] seed: registradas %d skins", len(names))
    return len(names)


async def _lookup_item(client: httpx.AsyncClient, name: str) -> dict:
    """GET /item?market_hash_name=<name> vía el limiter compartido. Devuelve el item."""
    await _history_limiter.acquire()
    resp = await client.get(
        f"{STEAM_WEB_API}/item",
        params={"key": STEAM_API_KEY, "game": "cs2",
                "market_hash_name": name, "format": "json"},
        timeout=_LOOKUP_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})


async def capture(client: httpx.AsyncClient) -> dict:
    """Captura UN LOTE de hasta PRICE_LOOKUP_CAP skins pendientes del día.

    Es una corrida completa e independiente: escribe sus puntos y marca
    `last_captured` antes de devolver. El workflow la llama repetidamente hasta
    que `pendientes` llega a 0 — Render free corta las peticiones largas, así
    que una sola corrida sobre toda la población da 502 y pierde el trabajo
    entero.

    Best-effort por skin: un fallo de lookup no aborta el lote.
    """
    today = date.today().isoformat()

    # Solo las que aún no se han capturado HOY: es lo que hace que la llamada
    # N+1 avance en vez de repetir el mismo lote. fetch_tracked ya ordena por
    # last_captured ascendente (nulls primero), así que basta con excluir las
    # de hoy para que el orden natural haga de cursor.
    pendientes = await repo.count_pending(today)
    names = await repo.fetch_tracked(PRICE_LOOKUP_CAP, before=today)

    rows: list[dict] = []
    # Todas las intentadas, con o sin éxito: se marcan igual para que la rueda
    # avance POR INTENTO. Si solo se marcaran las capturadas, una skin que falla
    # el lookup siempre (nombre inválido, delistada) seguiría "pendiente" para
    # siempre y el bucle del workflow la reintentaría lote tras lote sin que
    # `pendientes` bajara nunca. Mismo criterio que enrich-tick con enriched_at.
    intentadas: list[str] = []
    skipped = 0
    errors = 0

    for name in names:
        intentadas.append(name)
        try:
            item = await _lookup_item(client, name)
        except Exception as exc:  # noqa: BLE001 — best-effort: un fallo no aborta el lote
            errors += 1
            logger.warning("[price] lookup falló para %r: %s", name, exc)
            continue

        price = _canonical_price(item)
        if price is None:
            skipped += 1
            continue

        volume = item.get("sold24h")
        rows.append({
            "market_hash_name": name,
            "date": today,
            "price": price,
            "volume": int(volume) if volume is not None else None,
            "source": "steamwebapi",
        })

    await repo.upsert_prices(rows)
    await repo.mark_captured(intentadas, today)

    # Lo que queda para el siguiente lote. El workflow repite mientras sea > 0.
    restantes = max(pendientes - len(intentadas), 0)
    logger.info(
        "[price] lote: %d pedidas, %d capturadas, %d saltadas, %d errores | "
        "quedan %d pendientes",
        len(names), len(rows), skipped, errors, restantes,
    )

    return {"tracked_run": len(names), "captured": len(rows),
            "skipped": skipped, "errors": errors,
            "pendientes": restantes}
