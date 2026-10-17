"""COST-04 · Cuota real de /market/priceoverview para UN cubo (IP + Accept-Encoding del worker).

    nohup /home/marc/spike-venv/bin/python -m tools.spikes.spike_priceoverview_quota > logs/spikes/quota.log 2>&1 &

Tres fases, todas con el UA y el `Accept-Encoding: gzip, deflate` del worker (el cubo que usa en
producción). NO ejecutar con el job steam_priceoverview activo (STEAM_PRICEOVERVIEW_PER_TICK=0):
compartirían cubo y la medida no valdría.

  A  recuperación: una sonda cada PROBE_S hasta el primer 200 (cuánto dura el bloqueo vigente)
  B  cuota: una petición cada RATE_S con nombres distintos hasta el primer 429 (cuántas caben)
  C  recuperación tras agotarlo: una sonda cada PROBE_S hasta el primer 200

Salida: logs/spikes/priceoverview-quota-<ts>.jsonl (una línea por petición) y resumen al final.
Medido el 2026-10-17: ~113 peticiones a 2,3/min → 429, y bloqueo ≥ 10 h mientras se sondeaba cada hora.
"""
import datetime as dt
import json
import os
import pathlib
import time
from collections.abc import Callable

import httpx

from tools.spikes.spike_priceoverview import URL, default_names

UA = "cs-finance-worker/1.0 python-httpx/0.28.1"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Connection": "close"}
PROBE_S = 15 * 60      # fases A y C
RATE_S = 120           # fase B
MAX_B = 400            # tope de la fase B por si Steam nunca corta
LOCK = pathlib.Path("logs/spikes/quota.pid")


def _alive(pid: int) -> bool:
    return pathlib.Path(f"/proc/{pid}").exists()   # el spike solo corre en el VPS (Linux)


def single_instance(lock: pathlib.Path = LOCK, alive: Callable[[int], bool] = _alive) -> None:
    """Dos spikes a la vez comparten cubo y la medida no vale (EL-23, 2026-10-17: se lanzó dos veces).
    Si el pid del fichero sigue vivo, no se arranca; si murió, se pisa el fichero."""
    if lock.exists():
        pid = lock.read_text(encoding="utf-8").strip()
        if pid.isdigit() and alive(int(pid)):
            raise SystemExit(f"ya hay un spike corriendo (pid {pid}, {lock}): no se lanza otro")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def main() -> None:
    single_instance()
    out_dir = pathlib.Path("logs/spikes")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"priceoverview-quota-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}.jsonl"
    client = httpx.Client(timeout=30, headers=HEADERS)
    names = default_names(client, n=MAX_B)
    summary: dict[str, object] = {}

    def hit(phase: str, name: str) -> int:
        r = client.get(URL, params={"appid": 730, "currency": 1, "market_hash_name": name})
        row = {"ts": now(), "phase": phase, "status": r.status_code, "name": name,
               "retry_after": r.headers.get("Retry-After"), "body": r.text[:60]}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(row["ts"], phase, r.status_code, name[:40], flush=True)
        return r.status_code

    def wait_for_200(phase: str) -> float:
        t0 = time.monotonic()
        while hit(phase, names[0]) != 200:
            time.sleep(PROBE_S)
        return (time.monotonic() - t0) / 60

    summary["A_min_hasta_200"] = round(wait_for_200("A"), 1)
    t0, n = time.monotonic(), 0
    for name in names[1:]:
        if hit("B", name) == 429:
            break
        n += 1
        time.sleep(RATE_S)
    summary["B_peticiones_ok_antes_del_429"] = n
    summary["B_min_hasta_429"] = round((time.monotonic() - t0) / 60, 1)
    time.sleep(PROBE_S)
    summary["C_min_hasta_200"] = round(wait_for_200("C"), 1)
    print("RESUMEN", json.dumps(summary), flush=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "phase": "resumen", **summary}) + "\n")


if __name__ == "__main__":
    main()
