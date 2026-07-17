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
    """Snapshotea hasta PRICE_LOOKUP_CAP skins seguidas. Best-effort por skin."""
    names = await repo.fetch_tracked(PRICE_LOOKUP_CAP)
    today = date.today().isoformat()

    rows: list[dict] = []
    captured_names: list[str] = []
    skipped = 0
    errors = 0

    for name in names:
        try:
            item = await _lookup_item(client, name)
        except Exception as exc:  # noqa: BLE001 — best-effort: un fallo no aborta la corrida
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
        captured_names.append(name)

    await repo.upsert_prices(rows)
    await repo.mark_captured(captured_names, today)

    return {"tracked_run": len(names), "captured": len(rows),
            "skipped": skipped, "errors": errors}
