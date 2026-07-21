"""Orquestador de las tools de predicción e histórico.

Baja el histórico de precios de una skin y calcula la tendencia al horizonte
pedido. Best-effort: cualquier excepción → resultado "desconocida" sin propagar,
para que un fallo de red no rompa la conversación del agente.
"""

import logging

import httpx

from predict.trend import calcular

logger = logging.getLogger("uvicorn.error")


# Mínimo de puntos propios para preferir nuestra serie sobre CSFloat. Por debajo,
# la tabla aún no ha acumulado suficiente (el cron captura 1 punto/día/skin).
_MIN_PUNTOS_PROPIOS = 20


async def _historico(client: httpx.AsyncClient, name: str) -> list[dict]:
    """Histórico de precios de una skin, priorizando la serie propia.

    1. `precios_historicos` (Supabase): la captura diaria de `/internal/price-tick`.
       Es nuestra, no gasta cuota de steamwebapi y crece sin techo.
    2. Si aún no hay suficientes puntos, cae a `_fetch_history_for_item`
       (CSFloat, ~50d), que respeta el limiter de 18/60s y su caché de 23h.

    Ambas fuentes devuelven la misma forma `[{"date", "price", "volume"}]`.
    """
    try:
        from steam import price_history_repo as repo
        propios = await repo.fetch_prices(name)
        if len(propios) >= _MIN_PUNTOS_PROPIOS:
            logger.info("[predict] %s: %d puntos propios", name, len(propios))
            return propios
    except Exception as exc:  # noqa: BLE001 — sin Supabase seguimos con CSFloat
        logger.warning("[predict] fetch_prices falló para %s: %s", name, exc)

    from steam.services import _fetch_history_for_item
    return await _fetch_history_for_item(client, name)


async def predecir_tendencia(
    client: httpx.AsyncClient, name: str, horizon_days: int = 7
) -> dict:
    """Predice la evolución de precio de una skin al horizonte indicado."""
    try:
        pts = await _historico(client, name)
    except Exception as exc:
        logger.warning("[predict] histórico falló para %s: %s", name, exc)
        return {"tendencia": "desconocida", "motivo": f"error al obtener histórico: {exc}"}

    if not pts:
        return {"tendencia": "desconocida", "motivo": f"sin histórico para {name}"}

    try:
        return calcular(pts, horizon_days=horizon_days)
    except Exception as exc:
        logger.warning("[predict] calcular falló para %s: %s", name, exc)
        return {"tendencia": "desconocida", "motivo": f"error en el cálculo: {exc}"}


async def obtener_historico(
    client: httpx.AsyncClient, name: str, range_days: int = 30
) -> dict:
    """Histórico de precios ya observado — sin forecast.

    Para preguntas sobre el pasado ("¿cuánto costaba hace un mes?"), que no
    necesitan modelo: devuelve la serie recortada al rango pedido más un resumen
    (primero/último/máx/mín/cambio) para que el agente no tenga que calcular.
    """
    try:
        pts = await _historico(client, name)
    except Exception as exc:
        logger.warning("[predict] histórico falló para %s: %s", name, exc)
        return {"disponible": False, "motivo": f"error al obtener histórico: {exc}"}

    valid = [
        p for p in pts
        if isinstance(p.get("price"), (int, float)) and p["price"] > 0
    ]
    if not valid:
        return {"disponible": False, "motivo": f"sin histórico para {name}"}

    rango = max(1, int(range_days or 30))
    serie = valid[-rango:] if len(valid) > rango else valid

    precios = [float(p["price"]) for p in serie]
    primero, ultimo = precios[0], precios[-1]
    cambio_pct = ((ultimo - primero) / primero * 100) if primero else 0.0

    return {
        "disponible": True,
        "puntos": len(serie),
        "desde": serie[0].get("date"),
        "hasta": serie[-1].get("date"),
        "precio_inicial": round(primero, 4),
        "precio_final": round(ultimo, 4),
        "precio_maximo": round(max(precios), 4),
        "precio_minimo": round(min(precios), 4),
        "cambio_pct": round(cambio_pct, 2),
        "serie": [
            {"date": p.get("date"), "price": round(float(p["price"]), 4)} for p in serie
        ],
    }
