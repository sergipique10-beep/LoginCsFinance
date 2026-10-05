"""Seed + captura diaria de precios por-skin.

seed_tracked(): siembra tracked_skins desde el JSON curado si está vacía.
capture(): recorre la cola del price-tick (por prioridad y, dentro de ella,
menos-recientemente-capturadas primero, hasta PRICE_LOOKUP_CAP por lote y
PRICE_DAILY_BUDGET por día), hace lookup por-nombre en steamwebapi /item vía el
limiter compartido, y hace upsert del snapshot del día. Best-effort: un fallo
por skin no aborta la corrida.
"""
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from settings import PRICE_LOOKUP_CAP, PRICE_DAILY_BUDGET
from steam.api import steam_client
from steam.api.steam_client import _history_limiter
from steam.domain.validators import canonical_price
from steam.errors import QuotaExhausted
from steam.errors.handling import DEGRADABLE, log_degraded, reason_of
from steam import price_history_repo as repo

logger = logging.getLogger("uvicorn.error")

_SEED_PATH = Path(__file__).parent / "data" / "tracked_seed.json"


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
    data = await steam_client.item(client, name)
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

    # PERF-11: presupuesto diario. Lo gastado hoy se lee de la BD, así que el
    # tope aguanta aunque el workflow haga varios lotes o Render reinicie.
    restante = max(PRICE_DAILY_BUDGET - await repo.count_captured_on(today), 0)

    # Solo las que aún no se han capturado HOY: es lo que hace que la llamada
    # N+1 avance en vez de repetir el mismo lote. fetch_tracked ya ordena por
    # prioridad y last_captured, así que basta con excluir las de hoy para que
    # el orden natural haga de cursor.
    pendientes = await repo.count_pending(today)
    lote = min(PRICE_LOOKUP_CAP, restante)
    names = await repo.fetch_tracked(lote, before=today) if lote else []
    # Las que hoy no van a entrar. Se cuentan antes del lote: pendientes y
    # restante bajan igual con cada intento, así que el número es estable
    # entre lotes y el workflow puede avisar con el del último.
    fuera = max(pendientes - restante, 0)

    rows: list[dict] = []
    # Todas las intentadas, con o sin éxito: se marcan igual para que la rueda
    # avance POR INTENTO. Si solo se marcaran las capturadas, una skin que falla
    # el lookup siempre (nombre inválido, delistada) seguiría "pendiente" para
    # siempre y el bucle del workflow la reintentaría lote tras lote sin que
    # `pendientes` bajara nunca. Mismo criterio que enrich-tick con enriched_at.
    intentadas: list[str] = []
    skipped = 0
    errors = 0
    quota_exhausted = False

    for name in names:
        intentadas.append(name)
        try:
            item = await _lookup_item(client, name)
        except QuotaExhausted as exc:
            # Inútil seguir: cada llamada será otro 402 hasta el reset mensual.
            errors += 1
            quota_exhausted = True
            logger.error("[price] cuota de steamwebapi agotada, lote abortado: %s", exc)
            break
        except DEGRADABLE as exc:
            # Best-effort: un fallo de la fuente en una skin no aborta el lote. Lo que no
            # sea un fallo de la fuente es un bug y sube.
            errors += 1
            logger.warning("[price] lookup falló para %r: %s", name, exc)
            log_degraded("price_capture", reason_of(exc), "empty")
            continue

        price = canonical_price(item)
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

    # Lo que queda para el siguiente lote DENTRO del presupuesto. El workflow
    # repite mientras sea > 0; lo que no cabe va en `fuera_de_presupuesto`.
    restantes = max(min(pendientes, restante) - len(intentadas), 0)
    logger.info(
        "[price] lote: %d pedidas, %d capturadas, %d saltadas, %d errores | "
        "quedan %d pendientes, %d fuera de presupuesto (%d/día)",
        len(names), len(rows), skipped, errors, restantes, fuera, PRICE_DAILY_BUDGET,
    )

    return {"tracked_run": len(names), "captured": len(rows),
            "skipped": skipped, "errors": errors,
            "pendientes": restantes, "quota_exhausted": quota_exhausted,
            "fuera_de_presupuesto": fuera,
            "presupuesto_restante": max(restante - len(intentadas), 0)}
