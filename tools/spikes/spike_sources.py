"""COST-00 · Fuentes gratuitas, cuentas con clave, Supabase-ajeno e infraestructura.

    python -m tools.spikes.spike_sources [--only skinport,marketcsgo,...]

Bloques (todos por defecto):
  skinport    /v1/items y /v1/sales/history con Accept-Encoding: br — tamaño y cobertura
  marketcsgo  prices/USD.json — tamaño y cobertura
  csgotrader  buff163.json — tamaño, cobertura y MONEDA (3 ítems frente a priceoverview USD)
  players     GetPlayerSummaries con hasta 100 ids (STEAM_WEB_API_KEY, --ids fichero)
  keyed       CSFloat (CSFLOAT_API_KEY) y Waxpeer (WAXPEER_API_KEY): 10 llamadas, cabeceras
  infra       Hetzner GET /v1/pricing (HCLOUD_TOKEN) — precio de una IPv4 primaria; AAAA de Steam

Cobertura = % de market_hash_name del catálogo ByMykel (skins) que trae la fuente, más Jaccard.
Las claves solo se leen del entorno y nunca se registran. Salida: logs/spikes/sources-<ts>.jsonl.
"""
import argparse
import os
import socket
import time

import httpx

from tools.spikes.common import (Recorder, currency_ratio, guess_currency, name_coverage,
                                 parse_money, rate_headers)

BYMYKEL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/{}.json"
KNOWN = ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)",
         "M4A1-S | Hyper Beast (Field-Tested)"]


def catalog(client):
    skins = {s["market_hash_name"] for s in client.get(BYMYKEL.format("skins_not_grouped")).json()
             if s.get("market_hash_name")}
    return skins


def timed_get(client, url, **kw):
    t = time.monotonic()
    r = client.get(url, **kw)
    return r, round(time.monotonic() - t, 2)


def skinport(client, rec, cat):
    try:
        import brotli  # noqa: F401 — httpx solo descomprime br si está instalado
    except ImportError:
        return rec("skinport", skipped="pip install brotli (Skinport exige Accept-Encoding: br)")
    hdr = {"Accept-Encoding": "br"}
    for path in ("items", "sales/history"):
        r, s = timed_get(client, f"https://api.skinport.com/v1/{path}",
                         params={"app_id": 730, "currency": "USD"}, headers=hdr)
        data = r.json() if r.status_code == 200 else []
        rec("skinport", path=path, status=r.status_code, seconds=s,
            content_encoding=r.headers.get("content-encoding"), wire_bytes=r.num_bytes_downloaded,
            rows=len(data), rate=rate_headers(r.headers),
            **name_coverage([d.get("market_hash_name") for d in data], cat))


def marketcsgo(client, rec, cat):
    r, s = timed_get(client, "https://market.csgo.com/api/v2/prices/USD.json")
    items = r.json().get("items", []) if r.status_code == 200 else []
    rec("marketcsgo", status=r.status_code, seconds=s, wire_bytes=r.num_bytes_downloaded,
        rows=len(items), sample=items[:1], **name_coverage([i.get("market_hash_name") for i in items], cat))


def steam_usd(client, names):
    out = {}
    for n in names:
        r = client.get("https://steamcommunity.com/market/priceoverview/",
                       params={"appid": 730, "currency": 1, "market_hash_name": n})
        if r.status_code == 200:
            out[n] = parse_money(r.json().get("median_price") or r.json().get("lowest_price"))
        time.sleep(10)
    return out


def csgotrader(client, rec, cat):
    r, s = timed_get(client, "https://prices.csgotrader.app/latest/buff163.json")
    data = r.json() if r.status_code == 200 else {}
    buff = {}
    for n in KNOWN:
        e = data.get(n) or {}
        buff[n] = parse_money(((e.get("starting_at") or {}).get("price")) if isinstance(e, dict) else e)
    steam = steam_usd(client, KNOWN)
    ratio = currency_ratio(buff, steam)
    rec("csgotrader", status=r.status_code, seconds=s, wire_bytes=r.num_bytes_downloaded,
        rows=len(data), buff=buff, steam_usd=steam, ratio=ratio, currency=guess_currency(ratio),
        sample=next(iter(data.items()), None), **name_coverage(list(data), cat))


def players(client, rec, ids_file):
    key = os.environ.get("STEAM_WEB_API_KEY")
    if not key or not ids_file:
        return rec("players", skipped="falta STEAM_WEB_API_KEY o --ids")
    with open(ids_file, encoding="utf-8") as f:
        ids = [ln.strip() for ln in f if ln.strip()][:100]
    r = client.get("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                   params={"key": key, "steamids": ",".join(ids)})
    ps = r.json().get("response", {}).get("players", []) if r.status_code == 200 else []
    rec("players", status=r.status_code, asked=len(ids), returned=len(ps),
        with_personastate=sum(1 for p in ps if "personastate" in p),
        states=sorted({p.get("personastate") for p in ps if "personastate" in p}))


def keyed(client, rec):
    sources = {
        "csfloat": ("CSFLOAT_API_KEY", "https://csfloat.com/api/v1/listings",
                    lambda k: {"headers": {"Authorization": k}, "params": {"limit": 1}}),
        "waxpeer": ("WAXPEER_API_KEY", "https://api.waxpeer.com/v1/prices",
                    lambda k: {"params": {"api": k, "game": "csgo"}}),
    }
    for name, (env, url, kw) in sources.items():
        key = os.environ.get(env)
        if not key:
            rec("keyed", source=name, skipped=f"falta {env}")
            continue
        for i in range(10):
            r = client.get(url, **kw(key))
            rec("keyed", source=name, i=i, status=r.status_code, rate=rate_headers(r.headers))
            time.sleep(1)


def infra(client, rec):
    token = os.environ.get("HCLOUD_TOKEN")
    if token:
        r = client.get("https://api.hetzner.cloud/v1/pricing", headers={"Authorization": f"Bearer {token}"})
        p = r.json().get("pricing", {}) if r.status_code == 200 else {}
        rec("hetzner", status=r.status_code, currency=p.get("currency"),
            primary_ips=p.get("primary_ips"), floating_ips=p.get("floating_ips"))
    else:
        rec("hetzner", skipped="falta HCLOUD_TOKEN")
    for host in ("steamcommunity.com", "api.steampowered.com"):
        try:
            v6 = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET6)})
        except socket.gaierror as e:
            v6 = f"sin AAAA ({e})"
        rec("aaaa", host=host, v6=v6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="skinport,marketcsgo,csgotrader,players,keyed,infra")
    ap.add_argument("--ids", help="fichero con SteamID64 (uno por línea) para GetPlayerSummaries")
    a = ap.parse_args()
    only = set(a.only.split(","))
    rec = Recorder("sources")
    with httpx.Client(timeout=120, headers={"User-Agent": "cs-finance-spike/0.1 (COST-00)"}) as client:
        cat = catalog(client) if only & {"skinport", "marketcsgo", "csgotrader"} else set()
        rec("catalog", skins=len(cat))
        steps = {"skinport": lambda: skinport(client, rec, cat), "marketcsgo": lambda: marketcsgo(client, rec, cat),
                 "csgotrader": lambda: csgotrader(client, rec, cat), "players": lambda: players(client, rec, a.ids),
                 "keyed": lambda: keyed(client, rec), "infra": lambda: infra(client, rec)}
        for name, step in steps.items():
            if name not in only:
                continue
            try:
                step()
            except (httpx.HTTPError, ValueError) as e:
                rec("error", step=name, error=repr(e))
    rec.close()


if __name__ == "__main__":
    main()
