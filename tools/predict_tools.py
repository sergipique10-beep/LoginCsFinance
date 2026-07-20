"""Tool de predicción de tendencia para el orquestador de Sharky."""

from __future__ import annotations

import logging

import httpx

from tools.registry import register_tool

logger = logging.getLogger("uvicorn.error")


async def _predecir_tendencia(*, market_hash_name: str, client: httpx.AsyncClient) -> dict:
    """Predice la tendencia de precio de una skin a 7 días."""
    from predict.service import predecir_tendencia
    return await predecir_tendencia(client, market_hash_name)


def register_predict_tools() -> None:
    """Registra la tool de predicción en el registry."""
    register_tool(
        name="predecir_tendencia_skin",
        description=(
            "Predice la tendencia de precio de una skin de CS2 a 7 días. "
            "Indica si el precio sube, baja o se mantiene estable, con un nivel "
            "de confianza basado en la linealidad del histórico reciente."
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_hash_name": {
                    "type": "string",
                    "description": "Market hash name canónico, ej: 'AK-47 | Redline (Field-Tested)'",
                },
            },
            "required": ["market_hash_name"],
        },
        fn=_predecir_tendencia,
    )
