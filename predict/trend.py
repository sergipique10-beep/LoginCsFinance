"""Cálculo puro de tendencia de precio con horizonte configurable.

Regresión lineal por mínimos cuadrados sobre el histórico reciente de una skin.
Sin dependencias externas (numpy/scipy) — fórmula directa.

Entrada: lista de puntos ``[{"date": str, "price": float}, ...]`` ordenados
por fecha ascendente.
Salida: dict con estimación puntual, intervalo, dirección, confianza, versión
del modelo y error de backtest.

**Nunca se devuelve una cifra suelta**: toda estimación puntual va acompañada de
su intervalo y del resultado del backtest walk-forward contra la predicción naive.
Si el modelo no bate a naive, la confianza se degrada a "baja" y se declara —
el consumidor (el agente) debe comunicarlo, no ocultarlo.
"""

from __future__ import annotations

import math

from predict.backtest import walk_forward

MODEL_VERSION = "linreg-ols-v1"

HORIZONTE_DIAS = 7          # default; `calcular` acepta horizon_days
HORIZONTE_MAX = 30          # más allá, la extrapolación lineal es fantasía
UMBRAL_PCT = 2.0
R2_ALTA = 0.5
R2_MEDIA = 0.2
MIN_PUNTOS = 14
# z para un intervalo ~95% bajo normalidad de los residuos.
_Z_95 = 1.96


def _unknown(motivo: str, horizon: int) -> dict:
    return {
        "tendencia": "desconocida",
        "motivo": motivo,
        "horizon_days": horizon,
        "model_version": MODEL_VERSION,
    }


def calcular(points: list[dict], horizon_days: int = HORIZONTE_DIAS) -> dict:
    """Estima la evolución de precio a ``horizon_days`` vista.

    Clasificación por cambio estimado: > +UMBRAL_PCT → "sube", < -UMBRAL_PCT →
    "baja", en medio → "estable".

    Confianza (R² del ajuste), degradada por el backtest:
      - R² >= 0.5 y no-estable → "alta"
      - R² >= 0.2 → "media"
      - si no → "baja"
      - **si el backtest dice que no bate a naive → "baja"**, sea cual sea el R².

    Salvaguardas: < MIN_PUNTOS válidos → "desconocida"; outliers (>10× mediana),
    precios <= 0 descartados; nunca lanza.
    """
    horizon = max(1, min(int(horizon_days or HORIZONTE_DIAS), HORIZONTE_MAX))

    if not points:
        return _unknown("sin datos", horizon)

    valid = [p for p in points if isinstance(p.get("price"), (int, float)) and p["price"] > 0]
    if len(valid) < 2:
        return _unknown("datos insuficientes", horizon)

    prices_sorted = sorted(p["price"] for p in valid)
    median = prices_sorted[len(prices_sorted) // 2]
    if median > 0:
        valid = [p for p in valid if p["price"] <= median * 10]
    if len(valid) < MIN_PUNTOS:
        return _unknown("datos insuficientes", horizon)

    n = len(valid)
    x_vals = list(range(n))
    y_vals = [float(p["price"]) for p in valid]
    precio_actual = y_vals[-1]

    sum_x = sum(x_vals)
    sum_y = sum(y_vals)
    sum_xy = sum(x * y for x, y in zip(x_vals, y_vals))
    sum_x2 = sum(x * x for x in x_vals)

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return _unknown("varianza nula en las fechas", horizon)

    b = (n * sum_xy - sum_x * sum_y) / denom  # pendiente (moneda/día)
    a = (sum_y - b * sum_x) / n

    # Estimación puntual a `horizon` días desde el último punto observado.
    precio_estimado = a + b * (x_vals[-1] + horizon)
    cambio_estimado_eur = precio_estimado - precio_actual
    cambio_estimado_pct = (cambio_estimado_eur / precio_actual * 100) if precio_actual else 0.0

    # R² y error estándar de los residuos → intervalo de la estimación.
    y_mean = sum_y / n
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(x_vals, y_vals))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Desviación de los residuos (n-2 gdl por los dos parámetros ajustados).
    resid_sd = math.sqrt(ss_res / (n - 2)) if n > 2 and ss_res > 0 else 0.0
    # El error crece con la distancia extrapolada: factor sqrt(1 + h/n) como
    # aproximación barata al ensanchamiento del intervalo de predicción.
    margen = _Z_95 * resid_sd * math.sqrt(1.0 + horizon / n)

    if cambio_estimado_pct > UMBRAL_PCT:
        tendencia = "sube"
    elif cambio_estimado_pct < -UMBRAL_PCT:
        tendencia = "baja"
    else:
        tendencia = "estable"

    if r2 >= R2_ALTA and tendencia != "estable":
        confianza = "alta"
    elif r2 >= R2_MEDIA:
        confianza = "media"
    else:
        confianza = "baja"

    # Gate: si el walk-forward no bate a naive, la confianza cae a "baja" y se
    # declara el motivo. No se oculta la cifra, pero deja de venderse como fiable.
    bt = walk_forward(y_vals, horizon)
    backtest: dict = {"evaluado": False, "motivo": "serie corta para walk-forward"}
    if bt is not None:
        backtest = {
            "evaluado": True,
            "mae_modelo": round(bt["mae_model"], 4),
            "mae_naive": round(bt["mae_naive"], 4),
            "folds": bt["folds"],
            "supera_naive": bt["beats_naive"],
        }
        if not bt["beats_naive"]:
            confianza = "baja"

    return {
        "tendencia": tendencia,
        "confianza": confianza,
        "precio_actual": round(precio_actual, 4),
        "precio_estimado": round(precio_estimado, 4),
        "intervalo": {
            "min": round(precio_estimado - margen, 4),
            "max": round(precio_estimado + margen, 4),
            "nivel": 0.95,
        },
        "cambio_estimado_pct": round(cambio_estimado_pct, 2),
        "horizon_days": horizon,
        "dias_analizados": n,
        "r2": round(r2, 4),
        "model_version": MODEL_VERSION,
        "backtest": backtest,
    }
