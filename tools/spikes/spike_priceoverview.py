"""COST-00 · Rate limit de /market/priceoverview desde la IP del VPS.

    python -m tools.spikes.spike_priceoverview [--names fichero.txt]

100 nombres a 6/min y luego a 10/min. Por petición: status, si `volume` es parseable y
Retry-After. Sin --names toma 100 skins del catálogo ByMykel (las de arma, un desgaste fijo).
Salida: logs/spikes/priceoverview-<ts>.jsonl.
"""
import argparse
import time

import httpx

from tools.spikes.common import Recorder, parse_money, parse_volume

URL = "https://steamcommunity.com/market/priceoverview/"
CATALOG = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins_not_grouped.json"


def default_names(client, n=100):
    names = [s["market_hash_name"] for s in client.get(CATALOG).json()
             if s.get("market_hash_name") and "(Field-Tested)" in s["market_hash_name"]
             and not s["market_hash_name"].startswith(("StatTrak", "Souvenir", "★"))]
    step = max(1, len(names) // n)
    return names[::step][:n]


def run(client, names, per_min, rec):
    period = 60 / per_min
    ok = parsed = 0
    for i, name in enumerate(names):
        t = time.monotonic()
        r = client.get(URL, params={"appid": 730, "currency": 1, "market_hash_name": name})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        vol = parse_volume(body.get("volume"))
        ok += r.status_code == 200
        parsed += vol is not None
        rec("price", per_min=per_min, i=i, name=name, status=r.status_code,
            lowest=parse_money(body.get("lowest_price")), median=parse_money(body.get("median_price")),
            volume=vol, retry_after=r.headers.get("Retry-After"))
        if r.status_code == 429:
            rec("first_429", per_min=per_min, at=i)
            return False
        time.sleep(max(0.0, period - (time.monotonic() - t)))
    rec("summary", per_min=per_min, n=len(names), ok=ok, volume_parsed=parsed)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", help="un market_hash_name por línea")
    ap.add_argument("--rates", default="6,10")
    ap.add_argument("--cooldown-s", type=int, default=600, help="pausa entre ritmos")
    a = ap.parse_args()
    rec = Recorder("priceoverview")
    with httpx.Client(timeout=30, headers={"User-Agent": "cs-finance-spike/0.1 (COST-00)"}) as client:
        if a.names:
            with open(a.names, encoding="utf-8") as f:
                names = [ln.strip() for ln in f if ln.strip()]
        else:
            names = default_names(client)
        rec("names", n=len(names))
        for k, per_min in enumerate(int(x) for x in a.rates.split(",")):
            if k:
                time.sleep(a.cooldown_s)
            if not run(client, names, per_min, rec):
                break
    rec.close()


if __name__ == "__main__":
    main()
