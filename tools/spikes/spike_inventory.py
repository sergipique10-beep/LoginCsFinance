"""COST-00 · Rate limit real del inventario de Steam y cobertura de asset_properties.

Se ejecuta DESDE EL VPS (nunca desde Render ni desde el portátil: el límite es por IP):

    python -m tools.spikes.spike_inventory <steamid64> <steamid64> <steamid64_con_2000+>

1. Cobertura: descarga cada inventario entero (paginado) y mide el % de skins con
   Wear Rating / Pattern Template / Item Certificate; decodifica 20 certificados.
2. Ritmo: cadencias 4 s → 2 s → 1 s, 30 peticiones cada una, rotando inventarios. Al primer
   429 para, anota Retry-After y sondea cada 10 min hasta el primer 200 (duración del bloqueo).
   Si el 429 llega en la primera cadencia, repite a 8 s antes de seguir (nota del issue).

Salida: logs/spikes/inventory-<ts>.jsonl y un resumen al final.
"""
import argparse
import time

import httpx

from tools.spikes.common import Recorder, compare_certificates, property_coverage

URL = "https://steamcommunity.com/inventory/{sid}/730/2"
UA = {"User-Agent": "cs-finance-spike/0.1 (COST-00)"}


def fetch_page(client, sid, start=None):
    params = {"l": "english", "count": 2000}
    if start:
        params["start_assetid"] = start
    return client.get(URL.format(sid=sid), params=params)


def fetch_full(client, sid, rec):
    """Inventario entero fusionando páginas (assets, descriptions, asset_properties)."""
    merged = {"assets": [], "descriptions": [], "asset_properties": []}
    start = None
    while True:
        r = fetch_page(client, sid, start)
        rec("coverage_page", sid=sid, status=r.status_code, bytes=len(r.content))
        if r.status_code != 200:
            return None
        j = r.json()
        for k in merged:
            merged[k] += j.get(k) or []
        if not j.get("more_items"):
            return merged
        start = j.get("last_assetid")
        time.sleep(4)


def wait_unblock(client, sid, rec, poll_s):
    t0 = time.monotonic()
    while True:
        time.sleep(poll_s)
        r = fetch_page(client, sid)
        rec("unblock_poll", status=r.status_code, elapsed_s=round(time.monotonic() - t0))
        if r.status_code == 200:
            return round(time.monotonic() - t0)


def run_cadence(client, sids, period, n, rec):
    """Devuelve el nº de petición del primer 429 (o None si aguanta las n)."""
    for i in range(n):
        t = time.monotonic()
        r = fetch_page(client, sids[i % len(sids)])
        rec("rate", period_s=period, i=i, status=r.status_code,
            retry_after=r.headers.get("Retry-After"), ms=round(1000 * (time.monotonic() - t)))
        if r.status_code == 429:
            return i
        time.sleep(max(0.0, period - (time.monotonic() - t)))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("steamids", nargs=3)
    ap.add_argument("--per-cadence", type=int, default=30)
    ap.add_argument("--poll-s", type=int, default=600)
    ap.add_argument("--skip-rate", action="store_true", help="solo cobertura")
    a = ap.parse_args()
    rec = Recorder("inventory")
    with httpx.Client(headers=UA, timeout=30) as client:
        all_rows = []
        for sid in a.steamids:
            inv = fetch_full(client, sid, rec)
            if inv is None:
                continue
            rec("coverage", sid=sid, **property_coverage(inv))
            if len(all_rows) < 20:
                all_rows += compare_certificates(inv, limit=20 - len(all_rows))
            time.sleep(4)
        ok = [r for r in all_rows if r.get("diff") is not None]
        rec("certificates", decoded=len(ok), errors=len(all_rows) - len(ok),
            max_diff=max((r["diff"] for r in ok), default=None),
            checksum_ok=sum(1 for r in ok if r["checksum_ok"]), rows=all_rows)
        if a.skip_rate:
            return rec.close()
        cadences = [4, 2, 1]
        for period in cadences:
            first_429 = run_cadence(client, a.steamids, period, a.per_cadence, rec)
            if first_429 is None:
                continue
            blocked = wait_unblock(client, a.steamids[0], rec, a.poll_s)
            rec("blocked", period_s=period, first_429_at=first_429, block_s=blocked)
            if period == cadences[0]:
                again = run_cadence(client, a.steamids, 8, a.per_cadence, rec)
                rec("fallback_8s", first_429_at=again)
            break
    rec.close()


if __name__ == "__main__":
    main()
