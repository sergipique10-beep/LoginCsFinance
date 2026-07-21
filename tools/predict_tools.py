"""Tools de predicción e histórico de precios para el orquestador de Sharky."""

from __future__ import annotations

import logging

import httpx

from tools.registry import register_tool

logger = logging.getLogger("uvicorn.error")


async def _predecir_tendencia(
    *, market_hash_name: str, client: httpx.AsyncClient, horizon_days: int = 7
) -> dict:
    """Predice la evolución de precio de una skin al horizonte indicado."""
    from predict.service import predecir_tendencia
    return await predecir_tendencia(client, market_hash_name, horizon_days=horizon_days)


async def _obtener_historico(
    *, market_hash_name: str, client: httpx.AsyncClient, range_days: int = 30
) -> dict:
    """Histórico de precios ya observado, sin predicción."""
    from predict.service import obtener_historico
    return await obtener_historico(client, market_hash_name, range_days=range_days)


def register_predict_tools() -> None:
    """Registra las tools de predicción e histórico en el registry."""
    register_tool(
        name="predecir_tendencia_skin",
        description=(
            "Predice la evolución de precio de una skin de CS2 a N días vista. "
            "Devuelve estimación puntual CON intervalo, dirección (sube/baja/estable), "
            "nivel de confianza, versión del modelo y el error del backtest frente a "
            "la predicción naive. Si 'confianza' es 'baja' o 'backtest.supera_naive' "
            "es false, DEBES advertir al usuario de que la estimación es poco fiable. "
            "Úsala solo para el futuro; para precios pasados usa obtener_historico_skin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_hash_name": {
                    "type": "string",
                    "description": "Market hash name canónico, ej: 'AK-47 | Redline (Field-Tested)'",
                },
                "horizon_days": {
                    "type": "integer",
                    "description": "Días a futuro de la predicción (1-30). Por defecto 7.",
                },
            },
            "required": ["market_hash_name"],
        },
        fn=_predecir_tendencia,
    )

    register_tool(
        name="obtener_historico_skin",
        description=(
            "Histórico de precios YA OBSERVADO de una skin de CS2: serie de puntos "
            "más resumen (precio inicial, final, máximo, mínimo y cambio %). "
            "Úsala para preguntas sobre el pasado ('¿cuánto costaba hace un mes?', "
            "'¿ha subido últimamente?'). No predice nada."
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_hash_name": {
                    "type": "string",
                    "description": "Market hash name canónico, ej: 'AK-47 | Redline (Field-Tested)'",
                },
                "range_days": {
                    "type": "integer",
                    "description": "Días de histórico a devolver. Por defecto 30.",
                },
            },
            "required": ["market_hash_name"],
        },
        fn=_obtener_historico,
    )
