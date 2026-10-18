"""Comparación diaria de la convivencia (spec § 6.3): steamwebapi vs propio → Supabase `shadow_report`.

  shadow_compare   00:25   market_trending / market_movers / market_cap_history (steamwebapi, los escribe
                           Render) frente a sus tablas sombra `_own` (COST-05), el volumen de Steam del
                           top-N (`market_latest`, source='steam') y el último cooldown de Steam (Valkey)
                           → una fila por día con las métricas y si cumple los objetivos del corte.

El corte (COST-06) solo se ejecuta con 7 filas seguidas con `ok = true`. Hasta hoy la comparación eran
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
# Objetivos para cortar (spec § 6.3). `pct` = máximo; el resto = mínimo.
TARGETS = {"mediana_desv_pct": 8.0, "p90_desv_pct": 25.0, "overlap_trending_top50": 0.6,
           "overlap_movers": 0.6, "corr_delta7d": 0.7}
SOLD_RATIO = (0.8, 1.25)  # sold24h Steam propio / steamwebapi
COOLDOWN_MAX_S = 3600     # «0 cooldowns de > 1 h»

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


def _day_sign(points: list[Row], day: dt.date) -> int | None:
    """Signo de la variación del índice en `day`: último punto de `day` frente al último del día anterior."""
    last: dict[dt.date, float] = {}
    for p in sorted(points, key=lambda p: str(p["ts"])):
        v = _f(p.get("priceindex"))
        if v is not None:
            last[dt.datetime.fromisoformat(str(p["ts"]).replace("Z", "+00:00")).date()] = v
    prev = day - dt.timedelta(days=1)
    if day not in last or prev not in last:
        return None
    d = last[day] - last[prev]
    return (d > 0) - (d < 0)


def report(day: dt.date, own: list[Row], sw: list[Row], own_mv: list[Row], sw_mv: list[Row],
           own_idx: list[Row], sw_idx: list[Row], steam_vol: dict[str, int], cooldown_last_s: int) -> Row:
    sw_by = {r["name"]: r for r in sw if (_f(r.get("price_latest")) or 0.0) >= MIN_PRICE}
    pairs = [(o_p, s_p) for o in own
             if o["name"] in sw_by and (o_p := _f(o.get("price_latest"))) and (s_p := _f(sw_by[o["name"]]["price_latest"]))]
    devs = sorted(abs(a / b - 1) * 100 for a, b in pairs)
    ratios = sorted(a / b for a, b in pairs)
    d7 = [(o_d, s_d) for o in own if o["name"] in sw_by
          and (o_d := _f(o.get("price_delta_7d"))) is not None and (s_d := _f(sw_by[o["name"]].get("price_delta_7d"))) is not None]
    sold = sorted(v / s for n, v in steam_vol.items()
                  if n in sw_by and (s := _f(sw_by[n].get("sold_24h"))) and v)
    own_sign, sw_sign = _day_sign(own_idx, day), _day_sign(sw_idx, day)
    out: Row = {
        "date": day.isoformat(), "n": len(pairs),
        "mediana_desv_pct": round(statistics.median(devs), 2) if devs else None,
        "p90_desv_pct": round(statistics.quantiles(devs, n=10)[8], 2) if len(devs) >= 10 else None,
        "ratio_mediana": round(statistics.median(ratios), 4) if ratios else None,
        "corr_delta7d": _corr(d7), "n_delta7d": len(d7),
        "overlap_trending_top50": _overlap(_top(sw), _top(own)),
        "overlap_movers": _overlap({(r["name"], r["bucket"]) for r in sw_mv}, {(r["name"], r["bucket"]) for r in own_mv}),
        "ratio_sold24h_mediana": round(statistics.median(sold), 3) if sold else None, "n_sold": len(sold),
        "index_sign_match": None if own_sign is None or sw_sign is None else own_sign == sw_sign,
        "steam_cooldown_last_s": int(cooldown_last_s),
    }
    fails = [k for k, lim in TARGETS.items()
             if out[k] is None or (out[k] > lim if k.endswith("_pct") else out[k] < lim)]
    r = out["ratio_sold24h_mediana"]
    if r is not None and not SOLD_RATIO[0] <= r <= SOLD_RATIO[1]:
        fails.append("ratio_sold24h")
    if out["index_sign_match"] is False:
        fails.append("index_sign")
    if cooldown_last_s > COOLDOWN_MAX_S:
        fails.append("steam_cooldown")
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
        since = (now - dt.timedelta(days=3)).isoformat()
        sw_idx = await sb_store.cap_points_since(sb, "market_cap_history", since)
        own_idx = await sb_store.cap_points_since(sb, settings.MARKET_CAP_HISTORY_TABLE, since)
        quotes = await market.latest_quotes(state.pg, now - dt.timedelta(days=1))
        steam_vol = {n: q.volume for n, qs in quotes.items() for q in qs if q.source == "steam" and q.volume}
        cooldown = int(await state.redis.get("cool:last:steamcommunity.com") or 0)
        row = report(day, own, sw, own_mv, sw_mv, own_idx, sw_idx, steam_vol, cooldown)
        await sb_store.upsert(sb, settings.SHADOW_REPORT_TABLE, [row], "date")
        return {k: row[k] for k in ("date", "n", "mediana_desv_pct", "p90_desv_pct", "overlap_trending_top50",
                                     "overlap_movers", "corr_delta7d", "ok", "fails")}
    return await ms._run(state, "shadow_compare", go)


@broker.task(task_name="shadow_compare", schedule=[{"cron": "25 0 * * *"}])
async def shadow_compare(context: ms.Ctx) -> dict[str, Any]:
    return await run_shadow_compare(context.state)
