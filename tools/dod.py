#!/usr/bin/env python
"""tools/dod.py — Definition of Done del backend (AOS-L5 §5.1, EDS-3 §3.18).

Encadena los gates de mas barato a mas caro y para en el primero rojo. Los logs
completos van a logs/dod/<gate>.log; el veredicto nunca se trunca.

    python tools/dod.py          todos los gates
    python tools/dod.py --fast   solo los de segundos (pre-push)

Un prerrequisito ausente (ruff, pytest-cov...) es ROJO con el motivo, nunca verde.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs" / "dod"
PY = sys.executable
try:
    FLOOR = json.loads((ROOT / "tools" / "ratchet-baseline.json").read_text(encoding="utf-8"))["coverage_floor"]
except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
    print(f"DOD: prerrequisito ausente: tools/ratchet-baseline.json con coverage_floor ({e}). Restauralo desde git.")
    sys.exit(1)

GATES = [  # (nombre, comando, rapido)
    # -x anclada a separadores: `venv|docs` a secas excluiria tambien rag/docs_loader.py (M-7).
    ("compile", [PY, "-m", "compileall", "-q", "-x", r"[\\/](venv|docs|\.superpowers)([\\/]|$)", "."], True),
    ("ratchet", [PY, "tools/ratchet.py"], True),
    ("pytest+coverage", [PY, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
                         "--cov=.", "--cov-report=term-missing:skip-covered",
                         f"--cov-fail-under={FLOOR}"], False),
]


def run(name, cmd):
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    t = time.time()
    with log.open("w", encoding="utf-8") as fh:
        try:
            rc = subprocess.call(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)  # noqa: S603 — comandos fijos
        except FileNotFoundError as e:
            fh.write(f"prerrequisito ausente: {e}\n")
            rc = 127
    text = log.read_text(encoding="utf-8", errors="ignore")
    if "No module named" in text:
        rc = rc or 127  # pytest-cov o ruff sin instalar: rojo con motivo (EDS-3 §3.18)
    ok = rc == 0
    print(f"==> GATE: {name}\n    {'OK' if ok else 'FAILED'}  {time.time() - t:4.0f}s  log: {log.relative_to(ROOT).as_posix()}")
    if not ok:
        print("".join(text.splitlines(True)[-30:]))
    return ok


def main():
    fast = "--fast" in sys.argv
    gates = [(n, c) for n, c, quick in GATES if quick or not fast]
    for i, (name, cmd) in enumerate(gates, 1):
        if not run(f"{i:02d}-{name}", cmd):
            print(f"DOD: GATES FAILED — fix causes, do not suppress. ({i}/{len(gates)})")
            return 1
    print(f"DOD: ALL {len(gates)} GATES GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
