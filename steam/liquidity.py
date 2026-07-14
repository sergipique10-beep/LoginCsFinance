"""Liquidity Score: qué tan rápido podés convertir un ítem en dinero.

Responde UNA pregunta: si listo este ítem hoy, ¿en cuánto se vende y a qué precio
real? Todos los pesos salen de ahí — un score que midiera "salud del mercado" o
"confianza en la valuación" tendría otros.

Recibe el dict CRUDO de steamwebapi, no el ya mapeado: `_map_item` colapsa None a 0
(`d.get("sold24h") or 0`) y eso destruiría la distinción entre "no hay datos" (None,
→ "N/A") y "cero ventas" (0, → score bajo legítimo).

Ver docs/superpowers/specs/2026-07-14-liquidity-score-design.md.
"""
import math

# Anclas de saturación. La justificación de cada número está en el spec.
_VELOCITY_SATURATION = 500.0   # ventas/día: más volumen que esto ya no acelera la venta
_HOURS_FLOOR = 720.0           # 30 días: tardar un mes es liquidez cero a efectos prácticos
_MAX_HAIRCUT = 0.50            # si el bid está al 50% de la vitrina, "vender rápido" es regalar
_BUYORDER_SATURATION = 5000.0

# Un bid por encima del ask es imposible en un mercado real: es basura de la API.
# Mismo espíritu que _MAX_PLAUSIBLE_RATIO en mappers._inline_delta.
_MAX_BID_OVER_ASK = 1.05

# Si los componentes disponibles no cubren al menos esta fracción del peso total,
# no sabemos lo suficiente para dar un número. Preferimos "N/A" a un 0% falso.
_MIN_WEIGHT_COVERAGE = 0.5

# El peso solo no alcanza: velocity + demand + consistency suman exactamente 0.50 y
# pasarían la compuerta, dando un número con confianza sobre un ítem del que no sabemos
# ni en cuánto se vende ni a qué precio real — las dos mitades de la pregunta que el
# score dice responder. Exigimos al menos una de las dos.
_CORE_COMPONENTS = ("timeToSell", "haircut")

_WEIGHTS = {
    "velocity":    0.30,   # se vende mucho
    "timeToSell":  0.25,   # se vende rápido
    "haircut":     0.25,   # te pagan cerca del precio de vitrina
    "demand":      0.10,   # hay compradores esperando
    "consistency": 0.10,   # los mercados coinciden en el precio
}


def _num(value) -> float | None:
    """None si el valor falta o no es numérico. NO colapsa 0 a None: 0 es un dato."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_ratio(value: float, saturation: float) -> float:
    """Escala logarítmica: la diferencia entre 5 y 50 importa mucho más que entre 400 y 450."""
    return _clamp(math.log1p(value) / math.log1p(saturation))


def _velocity(raw: dict) -> float | None:
    sold24h = _num(raw.get("sold24h"))
    sold7d = _num(raw.get("sold7d"))
    if sold24h is None and sold7d is None:
        return None
    v = ((sold24h or 0.0) + (sold7d or 0.0) / 7.0) / 2.0
    return _log_ratio(v, _VELOCITY_SATURATION)


def _time_to_sell(raw: dict) -> float | None:
    """Horas hasta vender. Usa la estimación del proveedor; si viene en 0, deriva la cola.

    La cola = cuántas horas tarda en agotarse el stock listado delante tuyo al ritmo
    de ventas actual. Es una magnitud con significado físico, no una proxy.
    """
    hours = _num(raw.get("hourstosold"))
    if not hours:
        listings = _num(raw.get("offervolume"))
        sold24h = _num(raw.get("sold24h"))
        if listings is None or sold24h is None:
            return None
        if sold24h <= 0:
            return 0.0   # nada se vendió en 24h: la cola no avanza. No es "no sé", es "no se mueve".
        hours = listings / (sold24h / 24.0)
    return _clamp(1.0 - math.log1p(hours) / math.log1p(_HOURS_FLOOR))


def _haircut(raw: dict) -> float | None:
    """Cuánto perdés si querés salir ya: la distancia entre el mejor bid y la vitrina.

    Un bid de 0 NO es un dato faltante: es "nadie te compra esto a ningún precio", la
    señal más ilíquida que existe. Tratarlo como faltante descartaba el componente y
    repartía su 0.25 entre los demás, con lo que un ítem sin ningún comprador puntuaba
    MÁS ALTO que uno con un bid malo pero real. Un 0 real cae solo hasta haircut 0.0.

    La cadena de precio replica la de `_map_item`: si un ítem se valora por `lowestprice`,
    el score tiene que ver el mismo precio que el usuario ve en pantalla.
    """
    price = (
        _num(raw.get("pricelatestsell")) or
        _num(raw.get("price")) or
        _num(raw.get("lowestprice")) or
        _num(raw.get("priceusd"))
    )
    bid = _num(raw.get("buyorderprice"))
    if price is None or price <= 0:
        return None   # sin precio de referencia no hay ratio que calcular
    if bid is None:
        return None   # el campo no vino: no sabemos. Distinto de que venga en 0.
    if bid > price * _MAX_BID_OVER_ASK:
        return None   # bid por encima del ask: imposible. Basura de la API, no un chollo.
    return _clamp(1.0 - ((price - bid) / price) / _MAX_HAIRCUT)


def _demand(raw: dict) -> float | None:
    """Buy orders = compradores haciendo fila con la plata en la mano. Suma, no resta."""
    buy_orders = _num(raw.get("buyordervolume"))
    if buy_orders is None:
        return None
    return _log_ratio(buy_orders, _BUYORDER_SATURATION)


def _consistency(raw: dict) -> float | None:
    """Si los mercados divergen, tu 'precio' depende de dónde vendas."""
    prices = [_num(p.get("price")) for p in (raw.get("prices") or [])]
    prices = [p for p in prices if p and p > 0]
    if len(prices) < 2:
        return None
    return _clamp(min(prices) / max(prices))


def compute_liquidity(raw: dict) -> tuple[float | None, dict | None]:
    """Score 0-100 + desglose. (None, None) cuando no hay datos suficientes."""
    components = {
        "velocity":    _velocity(raw),
        "timeToSell":  _time_to_sell(raw),
        "haircut":     _haircut(raw),
        "demand":      _demand(raw),
        "consistency": _consistency(raw),
    }
    available = {k: v for k, v in components.items() if v is not None}
    total_weight = sum(_WEIGHTS[k] for k in available)
    if total_weight < _MIN_WEIGHT_COVERAGE:
        return None, None
    if not any(k in available for k in _CORE_COMPONENTS):
        return None, None   # sin tiempo ni precio, el número no responde la pregunta

    score = sum(_WEIGHTS[k] * v for k, v in available.items()) / total_weight * 100.0
    breakdown = {
        k: {"value": round(v, 4), "weight": round(_WEIGHTS[k] / total_weight, 4)}
        for k, v in available.items()
    }
    return round(score, 2), breakdown
