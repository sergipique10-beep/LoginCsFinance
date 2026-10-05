"""Histórico del índice de mercado de CS2 (CLEAN-18, ex market_service): captura horaria
en Supabase (`capture_cap_snapshot`, /internal/cap-tick) y lectura agrupada por timeframe
(`get_cap_history`, /market/cap-history). Sin FastAPI.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx

from steam.cache import stats_all
from steam.cap_history_repo import fetch_range, insert_snapshot
from steam.adapters.steam_adapter import adapt_market_index
from steam.api import steam_client
from steam.errors import (
    SourceTimeout, SourceUnavailable, UnexpectedPayload,
    UpstreamError,
)
from steam.utils.dates import hour_floor


logger = logging.getLogger("uvicorn.error")

# ── Histórico del índice (cap-history) ────────────────────────────────────────

_CAP_TF_MAP: dict[str, timedelta] = {
    "7d":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "3m":  timedelta(days=90),
    "6m":  timedelta(days=180),
    "1y":  timedelta(days=365),
    "3y":  timedelta(days=1095),
}

# Tamaño de bucket de downsampling por timeframe. A 3 años de snapshots
# horarios serían ~26k puntos crudos; agrupando se mantiene el payload acotado.
_CAP_BUCKET_MAP: dict[str, timedelta] = {
    "7d":  timedelta(hours=1),
    "1m":  timedelta(hours=6),
    "3m":  timedelta(days=1),
    "6m":  timedelta(days=1),
    "1y":  timedelta(weeks=1),
    "3y":  timedelta(weeks=1),
}

_CAP_FIELDS = ("priceindex", "realpriceindex", "buyorderpriceindex", "turnover24h")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def _parse_ts(ts: str) -> datetime:
    """Parsea un timestamptz ISO (con 'Z' o offset) a datetime aware en UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _downsample(rows: list[dict], bucket: timedelta) -> list[dict]:
    """
    Agrupa filas por floor(ts / bucket) y promedia cada campo.
    Mantiene { ts, v } (v = priceindex) y añade real/buyorder/turnover medios.
    `ts` de salida = inicio del bucket. Asume `rows` ordenado por ts asc.
    """
    bucket_s = bucket.total_seconds()
    grouped: dict[float, list[dict]] = {}
    order: list[float] = []
    for row in rows:
        ts = _parse_ts(row["ts"])
        idx = (ts - _EPOCH).total_seconds() // bucket_s
        if idx not in grouped:
            grouped[idx] = []
            order.append(idx)
        grouped[idx].append(row)

    out: list[dict] = []
    for idx in order:
        members = grouped[idx]
        start = _EPOCH + timedelta(seconds=idx * bucket_s)
        point: dict = {"ts": start.isoformat().replace("+00:00", "Z")}
        for field in _CAP_FIELDS:
            vals = [m[field] for m in members if m.get(field) is not None]
            point[field] = sum(vals) / len(vals) if vals else None
        # Contrato con el frontend: v = priceindex.
        point["v"] = point["priceindex"]
        out.append(point)
    return out


async def get_cap_history(tf: str) -> list[dict]:
    """Serie del índice desde Supabase, agrupada según `tf` (validado por la ruta)."""
    cutoff = datetime.now(timezone.utc) - _CAP_TF_MAP[tf]
    rows = await fetch_range(cutoff)
    return _downsample(rows, _CAP_BUCKET_MAP[tf])


# ── Ticks (crons externos) ────────────────────────────────────────────────────

async def capture_cap_snapshot(client: httpx.AsyncClient) -> dict:
    """/internal/cap-tick: guarda un snapshot horario del índice de precio."""
    try:
        data = await steam_client.market_index(client, timeout=15.0)
    except (SourceTimeout, SourceUnavailable):
        raise
    except UpstreamError as exc:
        logger.warning("[cap-tick] market-index returned %s", exc.status)
        raise

    mi = adapt_market_index(data)   # forma inesperada → UnexpectedPayload
    if mi.price_index is None:
        logger.warning("[cap-tick] 'priceindex' missing from response")
        raise UnexpectedPayload("'priceindex' missing from Steam response")

    # Floor al inicio de la hora: la PK es `ts`, así que varias capturas dentro
    # de la misma hora colapsan en una sola fila (upsert idempotente).
    hour_ts = hour_floor(datetime.now(timezone.utc))

    point = {
        "ts": hour_ts.isoformat().replace("+00:00", "Z"),
        "priceindex": mi.price_index,
        "realpriceindex": mi.real_price_index,
        "buyorderpriceindex": mi.buy_order_price_index,
        "turnover24h": mi.turnover_24h,
    }

    await insert_snapshot(point)
    logger.info("[cap-tick] snapshot saved: %s = %.4f", point["ts"], point["priceindex"])
    # Observabilidad de las cachés (CLEAN-16): una línea por caché cada hora, sin endpoint.
    for name, st in stats_all().items():
        logger.info("[steam-cache] name=%s entries=%d hits=%d misses=%d stale_served=%d",
                    name, st["entries"], st["hits"], st["misses"], st["stale_served"])
    return {"ok": True, "ts": point["ts"], "priceindex": point["priceindex"]}
