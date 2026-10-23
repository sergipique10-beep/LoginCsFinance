"""Comparación diaria de la convivencia (spec § 6.3): steamwebapi vs propio → Supabase `shadow_report`.

  shadow_compare   00:25   market_trending / market_movers / market_cap_history (steamwebapi, los escribe
                           Render) frente a sus tablas sombra `_own` (COST-05), el volumen de Steam del
                           top-N (`market_latest`, source='steam') y el último cooldown de Steam (Valkey)
                           → una fila por día con las métricas y si cumple los objetivos del corte.

El corte (COST-06) solo se ejecuta con 7 filas seguidas con `ok = true` (gate del 2026-10-23, en las constantes de abajo). Hasta hoy la comparación eran
SQL a mano (COST-03 16-10, COST-05 17-10): sin fila diaria no hay «7 días seguidos» medibles.

Solo lee: no toca Steam, Skinport ni las tablas de producción. Puro salvo las lecturas: `report()`.
"""
import datetime as dt
import logging
import statistics
from typing import Any

from worker import settings
from worker.broker import broker
from worker.jobs import market_sync as ms
from worker.store import market
from worker.store import supabase as sb_store

log = logging.getLogger(__name__)

MIN_PRICE = 10.0          # la desviación se mide en nombres ≥ 10 $ (spec § 6.3)
TOP = 50
# Gate del corte (spec § 6.3, redefinido el 2026-10-23). Lo que se le exige a lo propio:
#  - la referencia es CONSISTENTE con steamwebapi (ratio mediano en banda, sin colas rotas): un 20–30 % más baja
#    que Steam por construcción (comisión del 15 %, saldo no retirable), no se le pide igualarla;
#  - los rankings propios están COMPLETOS (trending y movers llenos);
#  - el índice sigue el SIGNO del mercado ≥ 80 % de los días de la última semana en que steamwebapi se movió de
#    verdad (zona muerta: un ±0,1 % no tiene signo), y no DERIVA: la variación acumulada en la ventana no se aparta
#    más de 1 punto de la de steamwebapi. La deriva la destapó la primera ejecución en seco (23-10): −2,1 % propio
#    frente a −0,4 % en 6 días. Zona muerta y deriva máxima son criterio, no medida.
# Fuera del gate, como dato: los solapes con steamwebapi y `corr_delta7d` (5 días seguidos en 2–4 % y 0,1–0,4 del
# 18 al 22-10): sus rankings ordenan por volumen de Steam y los nuestros por el de Skinport (un 4–17 % del de Steam),
# así que miden lo distintos que son los mercados, no un fallo. Sin Steam (COST-14, COST-04 cancelado) salen
# también `steam_desv_pct`, `ratio_sold24h` y el cooldown: sus columnas se siguen escribiendo y quedan a null/0.
RATIO_BAND = (0.6, 1.0)   # ratio_mediana referencia / steamwebapi
RATIO_P10_MIN = 0.4       # cola baja: una referencia rota (p10 0,002 el 16-10) la saca de la banda
TRENDING_MIN = 450        # de 500 (rankings.TRENDING_LIMIT): el trending propio no puede quedarse a medias
MOVERS_FULL = 10          # hot y cold, cada uno (= rankings.MOVERS_LIMIT)
SIGN_WINDOW = 7           # días
SIGN_MIN_DAYS = 5         # con menos días comparables no se juzga (aún no hay serie)
SIGN_MIN_MATCH = 0.8
SIGN_DEADBAND_PCT = 0.2   # |variación diaria de steamwebapi| por debajo de esto: el signo es ruido, no se juzga
DRIFT_MAX_PP = 1.0        # |Σ propio − Σ steamwebapi| en la ventana, en puntos porcentuales

Row = dict[str, Any]


# ---------------------------------------------------------------- puro

def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _top(rows: list[Row], n: int = TOP) -> set[str]:
    return {r["name"] for r in sorted(rows, key=lambda r: _f(r.get("turnover")) or 0.0, reverse=True)[:n]}


def _overlap(ref: set[Any], own: set[Any]) -> float:
    """Fracción del conjunto de steamwebapi (la referencia) que también está en el propio."""
    return round(len(ref & own) / len(ref), 3) if ref else 0.0


def _corr(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = [a for a, _ in pairs], [b for _, b in pairs]
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return round(statistics.correlation(xs, ys), 3)


def _closes(points: list[Row]) -> dict[dt.date, float]:
    """Último `priceindex` de cada día."""
    last: dict[dt.date, float] = {}
    for p in sorted(points, key=lambda p: str(p["ts"])):
        v = _f(p.get("priceindex"))
        if v is not None:
            last[dt.datetime.fromisoformat(str(p["ts"]).replace("Z", "+00:00")).date()] = v
    return last


def _day_change(closes: dict[dt.date, float], day: dt.date) -> float | None:
    """Variación de `day` frente al cierre del día anterior, en %."""
    prev = closes.get(day - dt.timedelta(days=1))
    return None if day not in closes or not prev else (closes[day] / prev - 1) * 100


def _day_sign(points: list[Row], day: dt.date) -> int | None:
    d = _day_change(_closes(points), day)
    return None if d is None else (d > 0) - (d < 0)


def _index_check(own_idx: list[Row], sw_idx: list[Row], day: dt.date) -> tuple[float | None, float | None]:
    """(fracción de días con el mismo signo, deriva acumulada en puntos) en la ventana; None con pocos días."""
    own_c, sw_c = _closes(own_idx), _closes(sw_idx)
    days = [day - dt.timedelta(days=i) for i in range(SIGN_WINDOW)]
    pairs = [(o, w) for d in days if (o := _day_change(own_c, d)) is not None and (w := _day_change(sw_c, d)) is not None]
    if len(pairs) < SIGN_MIN_DAYS:
        return None, None
    drift = round(abs(sum(o for o, _ in pairs) - sum(w for _, w in pairs)), 3)
    judged = [(o > 0) == (w > 0) for o, w in pairs if abs(w) >= SIGN_DEADBAND_PCT]
    match = round(sum(judged) / len(judged), 3) if len(judged) >= SIGN_MIN_DAYS else None
    return match, drift


def report(day: dt.date, own: list[Row], sw: list[Row], own_mv: list[Row], sw_mv: list[Row],
           own_idx: list[Row], sw_idx: list[Row], steam: dict[str, tuple[float | None, int | None]],
           cooldown_last_s: int) -> Row:
    """`steam` = nombre → (precio Steam propio, ventas 24 h) del top-N (`market_latest`, source='steam')."""
    sw_by = {r["name"]: r for r in sw if (_f(r.get("price_latest")) or 0.0) >= MIN_PRICE}
    pairs = [(o_p, s_p) for o in own
             if o["name"] in sw_by and (o_p := _f(o.get("price_latest"))) and (s_p := _f(sw_by[o["name"]]["price_latest"]))]
    devs = sorted(abs(a / b - 1) * 100 for a, b in pairs)
    ratios = sorted(a / b for a, b in pairs)
    d7 = [(o_d, s_d) for o in own if o["name"] in sw_by
          and (o_d := _f(o.get("price_delta_7d"))) is not None and (s_d := _f(sw_by[o["name"]].get("price_delta_7d"))) is not None]
    sold = sorted(v / s for n, (_, v) in steam.items()
                  if n in sw_by and (s := _f(sw_by[n].get("sold_24h"))) and v)
    steam_devs = sorted(abs(p / s - 1) * 100 for n, (p, _) in steam.items()
                        if p and n in sw_by and (s := _f(sw_by[n]["price_latest"])))
    own_sign, sw_sign = _day_sign(own_idx, day), _day_sign(sw_idx, day)
    out: Row = {
        "date": day.isoformat(), "n": len(pairs),
        "mediana_desv_pct": round(statistics.median(devs), 2) if devs else None,
        "p90_desv_pct": round(statistics.quantiles(devs, n=10)[8], 2) if len(devs) >= 10 else None,
        "ratio_mediana": round(statistics.median(ratios), 4) if ratios else None,
        "ratio_p10": round(statistics.quantiles(ratios, n=10)[0], 4) if len(ratios) >= 10 else None,
        "ratio_p90": round(statistics.quantiles(ratios, n=10)[8], 4) if len(ratios) >= 10 else None,
        "steam_desv_pct": round(statistics.median(steam_devs), 2) if steam_devs else None, "n_steam": len(steam_devs),
        "corr_delta7d": _corr(d7), "n_delta7d": len(d7),
        "overlap_trending_top50": _overlap(_top(sw), _top(own)),
        "overlap_movers": _overlap({(r["name"], r["bucket"]) for r in sw_mv}, {(r["name"], r["bucket"]) for r in own_mv}),
        "ratio_sold24h_mediana": round(statistics.median(sold), 3) if sold else None, "n_sold": len(sold),
        "index_sign_match": None if own_sign is None or sw_sign is None else own_sign == sw_sign,
        "steam_cooldown_last_s": int(cooldown_last_s),
    }
    fails = []
    rm = out["ratio_mediana"]
    if rm is None or not RATIO_BAND[0] <= rm <= RATIO_BAND[1]:
        fails.append("ratio_mediana")
    if out["ratio_p10"] is not None and out["ratio_p10"] < RATIO_P10_MIN:
        fails.append("ratio_p10")
    if len(own) < TRENDING_MIN:
        fails.append("trending_n")
    buckets = [r["bucket"] for r in own_mv]
    if min(buckets.count("hot"), buckets.count("cold")) < MOVERS_FULL:
        fails.append("movers_full")
    match, drift = _index_check(own_idx, sw_idx, day)
    if match is not None and match < SIGN_MIN_MATCH:
        fails.append("index_sign_7d")
    if drift is not None and drift > DRIFT_MAX_PP:
        fails.append("index_drift_7d")
    out["ok"] = not fails
    out["fails"] = ",".join(fails)
    return out


# ---------------------------------------------------------------- job

COLS = "name, price_latest, price_delta_7d, sold_24h, turnover"


async def run_shadow_compare(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        now = ms.now_utc()
        day = (now - dt.timedelta(days=1)).date()
        sb = state.supabase
        sw = await sb_store.select_all(sb, "market_trending", COLS)
        own = await sb_store.select_all(sb, settings.MARKET_TRENDING_TABLE, COLS)
        sw_mv = await sb_store.select_all(sb, "market_movers", "name, bucket")
        own_mv = await sb_store.select_all(sb, settings.MARKET_MOVERS_TABLE, "name, bucket")
        since = (now - dt.timedelta(days=SIGN_WINDOW + 2)).isoformat()
        sw_idx = await sb_store.cap_points_since(sb, "market_cap_history", since)
        own_idx = await sb_store.cap_points_since(sb, settings.MARKET_CAP_HISTORY_TABLE, since)
        quotes = await market.latest_quotes(state.pg, now - dt.timedelta(days=1))
        steam = {n: (q.price_usd, q.volume) for n, qs in quotes.items() for q in qs if q.source == "steam"}
        cooldown = int(await state.redis.get("cool:last:steamcommunity.com") or 0)
        row = report(day, own, sw, own_mv, sw_mv, own_idx, sw_idx, steam, cooldown)
        await sb_store.upsert(sb, settings.SHADOW_REPORT_TABLE, [row], "date")
        # trending_n, movers e index_sign_7d van al log, no a la tabla (sin DDL nuevo): se recalculan al vuelo.
        mv = [r["bucket"] for r in own_mv]
        return {**{k: row[k] for k in ("date", "n", "ratio_mediana", "ratio_p10", "overlap_trending_top50",
                                        "overlap_movers", "corr_delta7d", "ok", "fails")},
                "trending_n": len(own), "movers": (mv.count("hot"), mv.count("cold")),
                "index_sign_drift_7d": _index_check(own_idx, sw_idx, day)}
    return await ms._run(state, "shadow_compare", go)


@broker.task(task_name="shadow_compare", schedule=[{"cron": "25 0 * * *"}])
async def shadow_compare(context: ms.Ctx) -> dict[str, Any]:
    return await run_shadow_compare(context.state)
