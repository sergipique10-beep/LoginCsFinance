"""Cálculo puro de tendencia de precio a 7 días.

Regresión lineal por mínimos cuadrados sobre el histórico reciente de una skin.
Sin dependencias externas (numpy/scipy) — fórmula directa.

Entrada: lista de puntos ``[{"date": str, "price": float}, ...]`` ordenados
por fecha ascendente.
Salida: dict con tendencia, confianza, cambio estimado y metadatos.
"""

from __future__ import annotations

HORIZONTE_DIAS = 7
UMBRAL_PCT = 2.0
R2_ALTA = 0.5
R2_MEDIA = 0.2
MIN_PUNTOS = 14


def calcular(points: list[dict]) -> dict:
    """Calcula la tendencia de precio a 7 días a partir de una serie histórica.

    Clasificación:
      - ``cambio_pct > +UMBRAL_PCT`` → ``"sube"``
      - ``cambio_pct < -UMBRAL_PCT`` → ``"baja"``
      - en medio → ``"estable"``

    Confianza (vía R² del ajuste lineal):
      - R² >= 0.5 y clasificación no-estable → ``"alta"``
      - R² >= 0.2 → ``"media"``
      - si no → ``"baja"``

    Salvaguardas:
      - ``< MIN_PUNTOS`` puntos válidos → ``"desconocida"``
      - Outliers (> 10× mediana) descartados
      - price <= 0 descartados
      - Nunca lanza: si no puede calcular, devuelve ``"desconocida"``
    """
    if not points:
        return {"tendencia": "desconocida", "motivo": "sin datos"}

    # 1. Filtrar puntos inválidos
    valid = [p for p in points if isinstance(p.get("price"), (int, float)) and p["price"] > 0]
    if len(valid) < 2:
        return {"tendencia": "desconocida", "motivo": "datos insuficientes"}

    # 2. Filtrar outliers (> 10× mediana)
    prices = sorted(p["price"] for p in valid)
    median = prices[len(prices) // 2]
    if median > 0:
        valid = [p for p in valid if p["price"] <= median * 10]
    if len(valid) < MIN_PUNTOS:
        return {"tendencia": "desconocida", "motivo": "datos insuficientes"}

    # 3. Preparar series x (día 0..n-1) e y (precio)
    n = len(valid)
    x_vals = list(range(n))
    y_vals = [p["price"] for p in valid]

    precio_actual = y_vals[-1]

    # 4. Regresión lineal: y ≈ a + b·x
    sum_x = sum(x_vals)
    sum_y = sum(y_vals)
    sum_xy = sum(x * y for x, y in zip(x_vals, y_vals))
    sum_x2 = sum(x * x for x in x_vals)

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return {"tendencia": "desconocida", "motivo": "varianza nula en las fechas"}

    b = (n * sum_xy - sum_x * sum_y) / denom  # pendiente (€/día)
    a = (sum_y - b * sum_x) / n               # intercepto

    # 5. Proyección a HORIZONTE_DIAS
    cambio_estimado_eur = b * HORIZONTE_DIAS
    cambio_estimado_pct = (cambio_estimado_eur / precio_actual * 100) if precio_actual else 0.0

    # 6. R² (coeficiente de determinación)
    y_mean = sum_y / n
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(x_vals, y_vals))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # 7. Clasificación
    if cambio_estimado_pct > UMBRAL_PCT:
        tendencia = "sube"
    elif cambio_estimado_pct < -UMBRAL_PCT:
        tendencia = "baja"
    else:
        tendencia = "estable"

    # 8. Confianza
    if r2 >= R2_ALTA and tendencia != "estable":
        confianza = "alta"
    elif r2 >= R2_MEDIA:
        confianza = "media"
    else:
        confianza = "baja"

    return {
        "tendencia": tendencia,
        "confianza": confianza,
        "cambio_estimado_pct": round(cambio_estimado_pct, 2),
        "precio_actual": round(precio_actual, 4),
        "dias_analizados": n,
    }
