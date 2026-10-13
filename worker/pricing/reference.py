"""Precio de referencia de un ítem a partir de varias fuentes (COST-03). Puro: sin red ni BD.

    w = log1p(qty) × 0.5^(edad_h / HALF_LIFE_H) × k_mercado
    m = mediana simple de los precios; fuera los que caen fuera de [m/2, 2m]
    ref_price = mediana ponderada (por w) de los que quedan

`ref_confidence` (0–3) = nº de fuentes distintas que sobreviven al filtro, con tope 3; 0 = sin
precio. `prices` lleva TODAS las cotizaciones (también las descartadas, marcadas), que es lo que
pinta la lista de mercados del front.

Los `k_mercado` arrancan en 1,0 (neutros) y se calibran en la convivencia (diseño § 6.3) vía
la variable PRICE_K (JSON), p. ej. '{"buff": 1.1, "marketcsgo": 0.8}'.
"""
import datetime as dt
import json
import math
import os
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from worker.models import Quote

HALF_LIFE_H = 6.0


def market_k(raw: str | None = None) -> dict[str, float]:
    raw = os.environ.get("PRICE_K", "") if raw is None else raw
    return {k: float(v) for k, v in (json.loads(raw) if raw.strip() else {}).items()}


@dataclass(frozen=True)
class Reference:
    market_hash_name: str
    ref_price_usd: float | None
    ref_confidence: int
    ref_ts: dt.datetime | None          # la cotización más reciente que entró
    sources: int
    volume_24h: int | None
    prices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def turnover_24h(self) -> float | None:
        if self.ref_price_usd is None or self.volume_24h is None:
            return None
        return round(self.ref_price_usd * self.volume_24h, 2)


def weight(q: Quote, now: dt.datetime, k: dict[str, float] | None = None) -> float:
    age_h = max(0.0, (now - q.ts).total_seconds() / 3600)
    return math.log1p(q.qty) * 0.5 ** (age_h / HALF_LIFE_H) * (k or {}).get(q.source, 1.0)


def weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Mediana ponderada (valor, peso): el primer valor donde el peso acumulado llega a la mitad.
    En empate exacto en la mitad, media de los dos valores vecinos."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        raise ValueError("sin pesos positivos")
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if math.isclose(acc, half) and i + 1 < len(pairs):
            return (v + pairs[i + 1][0]) / 2
        if acc > half:
            return v
    return pairs[-1][0]


def compute(name: str, quotes: list[Quote], now: dt.datetime,
            k: dict[str, float] | None = None) -> Reference:
    priced = [(q.price_usd, q) for q in quotes if q.price_usd is not None and q.price_usd > 0]
    vols = [q.volume for q in quotes if q.volume is not None]
    volume_24h = max(vols) if vols else None
    if not priced:
        return Reference(name, None, 0, None, 0, volume_24h, _prices(quotes, set()))
    m = median(p for p, _ in priced)
    kept = [(p, q) for p, q in priced if m / 2 <= p <= 2 * m]
    try:
        ref = weighted_median([(p, weight(q, now, k)) for p, q in kept])
    except ValueError:  # todos los k a 0: sin preferencia, mediana simple
        ref = median(p for p, _ in kept)
    sources = len({q.source for _, q in kept})
    return Reference(name, round(ref, 4), min(sources, 3), max(q.ts for _, q in kept), sources,
                     volume_24h, _prices(quotes, {id(q) for _, q in kept}))


def _prices(quotes: list[Quote], kept: set[int]) -> list[dict[str, Any]]:
    return [{"market": q.source, "price": q.price_usd, "volume": q.volume, "listings": q.listings,
             "ts": q.ts.isoformat(), "used": id(q) in kept}
            for q in sorted(quotes, key=lambda q: q.source)]
