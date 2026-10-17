"""Índice de mercado propio, encadenado (COST-05). Puro: sin red ni BD.

Sustituye al `priceindex` de steamwebapi sin romper la gráfica que ya existe:

    cesta   = top-N nombres por facturación de 30 d en Skinport (volume_30d × avg_30d), fijada el
              día 1 de cada mes (los pesos w_i = volume_30d no cambian dentro del mes)
    I_t     = I_{t-1} × Σ w_i·p_i,t / Σ w_i·p_i,t-1        (Laspeyres encadenado, solo nombres con
              precio en t y en t-1; un ítem que falta un tick no mueve el índice)
    I_0     = último `priceindex` de steamwebapi (la serie continúa donde acaba la antigua)

El rebalanceo no salta: el cociente del primer tick del mes usa los pesos nuevos tanto en el
numerador como en el denominador. `priceindex` encadena `ref_price`; `realpriceindex`, la mediana de
ventas de 24 h de Skinport. `buyorderpriceindex` no se calcula (ninguna fuente del worker trae
órdenes de compra): queda a null hasta que la haya.
"""
from dataclasses import dataclass, field, replace
from typing import Any

BASKET_SIZE = 500
# Con menos nombres con precio en los dos instantes el cociente es ruido: se conserva el índice anterior.
MIN_CHAIN_NAMES = 100


@dataclass(frozen=True)
class SalesStats:
    """Lo que `skinport_sales` deja por nombre (Valkey `mk:sales`): volúmenes y medianas por ventana."""
    volume_24h: int = 0
    volume_7d: int = 0
    volume_30d: int = 0
    median_24h: float | None = None
    median_7d: float | None = None
    median_30d: float | None = None
    avg_30d: float | None = None

    @property
    def turnover_30d(self) -> float:
        return self.volume_30d * (self.avg_30d or self.median_30d or 0.0)


@dataclass(frozen=True)
class IndexState:
    """Lo que hay que recordar entre ticks (Valkey `index:state`, JSON)."""
    month: str = ""                                       # 'YYYY-MM' de la cesta vigente
    weights: dict[str, int] = field(default_factory=dict)  # w_i = volume_30d al fijar la cesta
    prev_price: dict[str, float] = field(default_factory=dict)
    prev_real: dict[str, float] = field(default_factory=dict)
    priceindex: float | None = None
    realpriceindex: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"month": self.month, "weights": self.weights, "prev_price": self.prev_price,
                "prev_real": self.prev_real, "priceindex": self.priceindex, "realpriceindex": self.realpriceindex}

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "IndexState":
        return cls(**d) if d else cls()


def basket(stats: dict[str, SalesStats], size: int = BASKET_SIZE) -> dict[str, int]:
    """Top-`size` por facturación de 30 d, con su peso (volume_30d). Solo nombres con ventas."""
    ranked = sorted(((s.turnover_30d, n) for n, s in stats.items() if s.volume_30d > 0 and s.turnover_30d > 0),
                    reverse=True)
    return {n: stats[n].volume_30d for _, n in ranked[:size]}


def chain(prev_index: float, weights: dict[str, int], prev: dict[str, float], cur: dict[str, float],
          min_names: int = MIN_CHAIN_NAMES) -> tuple[float, int]:
    """I_t y cuántos nombres entraron. Sin bastantes nombres comunes devuelve el índice anterior."""
    common = [n for n in weights if n in prev and n in cur and prev[n] > 0 and cur[n] > 0]
    if len(common) < min_names:
        return prev_index, 0
    num = sum(weights[n] * cur[n] for n in common)
    den = sum(weights[n] * prev[n] for n in common)
    return prev_index * num / den, len(common)


def step(state: IndexState, month: str, stats: dict[str, SalesStats], price: dict[str, float],
         real: dict[str, float], i0_price: float, i0_real: float | None,
         min_names: int = MIN_CHAIN_NAMES) -> tuple[IndexState, int]:
    """Un tick del índice: rebalancea si cambió el mes, encadena y devuelve el estado nuevo y el
    número de nombres que entraron en el cociente (0 en el primer tick: I_t = I_0)."""
    weights = basket(stats) if month != state.month else state.weights
    pi = state.priceindex if state.priceindex is not None else i0_price
    ri = state.realpriceindex if state.realpriceindex is not None else i0_real
    used = 0
    if state.prev_price:
        pi, used = chain(pi, weights, state.prev_price, price, min_names)
    if ri is not None and state.prev_real:
        ri, _ = chain(ri, weights, state.prev_real, real, min_names)
    in_basket = lambda d: {n: v for n, v in d.items() if n in weights}  # noqa: E731
    return replace(state, month=month, weights=weights, prev_price=in_basket(price), prev_real=in_basket(real),
                   priceindex=pi, realpriceindex=ri), used


def turnover_24h(stats: dict[str, SalesStats], steam: dict[str, tuple[float, int]]) -> tuple[float, int]:
    """(facturación, unidades) de 24 h: Skinport para todo el catálogo; Steam (precio, ventas) sustituye a
    Skinport en los nombres del N pequeño con lectura fresca."""
    total, units = 0.0, 0
    for n, s in stats.items():
        if n in steam:
            continue
        total += s.volume_24h * (s.median_24h or 0.0)
        units += s.volume_24h
    for p, v in steam.values():
        total += p * v
        units += v
    return round(total, 2), units
