"""Backtesting walk-forward del modelo de tendencia.

Criterio de exposición: el modelo solo merece confianza si bate a la predicción
naive ("dentro de H días el precio será el de hoy"). Si no la bate, la
predicción se sigue devolviendo pero degradada a confianza "baja" y
declarándolo — nunca se presenta como fiable algo que no lo es.

Walk-forward: para cada corte t de la serie, se ajusta el modelo SOLO con los
puntos hasta t (nada de mirar el futuro) y se compara la proyección a H días
contra el precio real observado en t+H. El error es el MAE sobre todos los
cortes evaluables.

Sin dependencias externas — mismo criterio que trend.py.
"""

from __future__ import annotations

# Mínimo de puntos para ajustar en cada corte. Por debajo, el ajuste es ruido.
MIN_TRAIN = 14
# Mínimo de cortes evaluables para que el MAE signifique algo.
MIN_FOLDS = 5


def _fit_slope_intercept(y_vals: list[float]) -> tuple[float, float] | None:
    """Regresión lineal y ≈ a + b·x sobre índices 0..n-1. None si no es calculable.

    Misma fórmula que trend.calcular; aquí se necesita aislada para poder
    reajustar en cada corte del walk-forward.
    """
    n = len(y_vals)
    if n < 2:
        return None
    sum_x = n * (n - 1) / 2
    sum_y = sum(y_vals)
    sum_xy = sum(i * y for i, y in enumerate(y_vals))
    sum_x2 = sum(i * i for i in range(n))
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return None
    b = (n * sum_xy - sum_x * sum_y) / denom
    a = (sum_y - b * sum_x) / n
    return a, b


def walk_forward(prices: list[float], horizon: int) -> dict | None:
    """MAE walk-forward del modelo lineal vs. el naive, sobre la misma serie.

    Devuelve None si la serie no da para al menos MIN_FOLDS cortes — en ese caso
    no hay evidencia para afirmar nada sobre la calidad del modelo.

    Returns:
        {"mae_model", "mae_naive", "folds", "beats_naive"}
    """
    errors_model: list[float] = []
    errors_naive: list[float] = []

    # El corte t usa prices[:t] para entrenar y prices[t-1+horizon] como verdad.
    for t in range(MIN_TRAIN, len(prices)):
        target_idx = t - 1 + horizon
        if target_idx >= len(prices):
            break
        train = prices[:t]
        fit = _fit_slope_intercept(train)
        if fit is None:
            continue
        a, b = fit
        last_idx = len(train) - 1
        pred_model = a + b * (last_idx + horizon)
        pred_naive = train[-1]  # "dentro de H días, lo mismo que hoy"
        actual = prices[target_idx]
        errors_model.append(abs(pred_model - actual))
        errors_naive.append(abs(pred_naive - actual))

    if len(errors_model) < MIN_FOLDS:
        return None

    mae_model = sum(errors_model) / len(errors_model)
    mae_naive = sum(errors_naive) / len(errors_naive)
    return {
        "mae_model": mae_model,
        "mae_naive": mae_naive,
        "folds": len(errors_model),
        "beats_naive": mae_model < mae_naive,
    }
