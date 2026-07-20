"""Orquestador de la tool de predicción de tendencia.

Baja el histórico de precios de una skin y calcula la tendencia a 7 días.
Best-effort: cualquier excepción → resultado "desconocida" sin propagar.
"""

import logging

import httpx

from predict.trend import calcular

logger = logging.getLogger("uvicorn.error")


async def predecir_tendencia(client: httpx.AsyncClient, name: str) -> dict:
    """Predice la tendencia de precio de una skin a 7 días.

    Reutiliza ``_fetch_history_for_item`` para obtener el histórico de CSFloat
    (~50 días). Si falla o no hay datos, devuelve ``tendencia: "desconocida"``
    con un motivo descriptivo.
    """
    try:
        from steam.services import _fetch_history_for_item
        pts = await _fetch_history_for_item(client, name)
    except Exception as exc:
        logger.warning("[predict] _fetch_history_for_item falló para %s: %s", name, exc)
        return {"tendencia": "desconocida", "motivo": f"error al obtener histórico: {exc}"}

    if not pts:
        return {"tendencia": "desconocida", "motivo": f"sin histórico para {name}"}

    try:
        return calcular(pts)
    except Exception as exc:
        logger.warning("[predict] calcular falló para %s: %s", name, exc)
        return {"tendencia": "desconocida", "motivo": f"error en el cálculo: {exc}"}
