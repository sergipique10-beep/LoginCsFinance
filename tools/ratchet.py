#!/usr/bin/env python
"""tools/ratchet.py — contadores de deuda que solo bajan (AOS-L3 §3.3).

    python tools/ratchet.py            verifica contra tools/ratchet-baseline.json
    python tools/ratchet.py --bless    reescribe la baseline (solo si nada empeora)
    --root <dir>   arbol a medir (tests);  --no-tools  omite ruff/mypy (tests)
    RATCHET_PYTHON  interprete con el que se lanzan ruff/mypy (por defecto, este)

Falla en las DOS direcciones: si una metrica sube (regresion) y si baja sin blessear
(la baseline debe reflejar la realidad en el mismo commit; regla A-8 de GUARDRAILS).

Metricas:
  ruff                  violaciones de ruff (ruff.toml)            # ponytail: conteo, no lista;
  mypy                  errores de mypy                              permite "quitar una, meter otra"
  bind_all_interfaces   literales "0.0.0.0" en el codigo (A-7)       hasta llegar a 0 y pasar a gate duro
  files_without_test    lista: ningun tests/test_*.py importa el modulo (EDS-3 §3.2)
  undocumented_files    lista: ningun docs/features/*.md lo cita en `files:` (AOS-DOC, A-9)
  coverage_floor        suelo de cobertura (lo lee tools/dod.py; el unico campo manual, solo sube)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SRC_PKGS = ["alerts", "auth", "chat", "llm", "notifications", "portfolio", "predict", "rag",
            "stats", "steam", "tools"]
SRC_TOP = ["main.py", "middleware.py", "settings.py", "stores.py"]
EXCLUDE = {"tools/ratchet.py", "tools/dod.py"}
PY = os.environ.get("RATCHET_PYTHON", sys.executable)


def rel(root, p):
    return p.relative_to(root).as_posix()


def source_files(root):
    out = [root / f for f in SRC_TOP if (root / f).exists()]
    for pkg in SRC_PKGS:
        if (root / pkg).is_dir():
            out += [p for p in (root / pkg).rglob("*.py")
                    if p.name != "__init__.py" and "__pycache__" not in p.parts]
    return sorted(p for p in out if rel(root, p) not in EXCLUDE)


def read_all(paths):
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in paths)


def tool_count(root, cmd, pred):
    try:
        # noqa S603: `cmd` son listas fijas de este fichero y PY un interprete, no entrada externa
        r = subprocess.run([PY, "-m", *cmd], cwd=root, capture_output=True, text=True)  # noqa: S603
    except FileNotFoundError:
        print(f"RATCHET: prerrequisito ausente: {PY} ({cmd[0]})")
        sys.exit(1)
    if "No module named" in r.stderr:
        print(f"RATCHET: prerrequisito ausente: {cmd[0]} (pip install -r requirements-dev.txt)")
        sys.exit(1)
    return pred(r.stdout)


def measure(root, tools=True):
    files = source_files(root)
    tests = read_all((root / "tests").glob("test_*.py")) if (root / "tests").is_dir() else ""
    docs = read_all((root / "docs" / "features").glob("*.md")) if (root / "docs" / "features").is_dir() else ""
    m = {}
    # ponytail: "testeado" = algun test importa el modulo; techo: no mide profundidad.
    without_test = []
    for f in files:
        mod = re.escape(rel(root, f)[:-3].replace("/", "."))
        if not re.search(rf"^(from\s+{mod}\s+import|import\s+{mod}\b)", tests, re.M):
            without_test.append(rel(root, f))
    m["files_without_test"] = without_test
    # A-9 (A-doc): fichero citado en el frontmatter `files:` de algun docs/features/*.md
    m["undocumented_files"] = [rel(root, f) for f in files if rel(root, f) not in docs]
    # A-7: bind a todas las interfaces
    m["bind_all_interfaces"] = sum(p.read_text(encoding="utf-8", errors="ignore").count('"0.0.0.0"')
                                   for p in files)
    if tools:
        m["ruff"] = tool_count(root, ["ruff", "check", ".", "--output-format", "json", "--exit-zero"],
                               lambda out: len(json.loads(out or "[]")))
        m["mypy"] = tool_count(root, ["mypy", ".", "--ignore-missing-imports", "--exclude", "venv",
                                      "--no-error-summary"],
                               lambda out: sum(1 for ln in out.splitlines() if ": error:" in ln))
    return m


def compare(base, cur):
    """Lista de (metrica, mensaje). Vacia = OK."""
    bad = []
    for k, v in cur.items():
        if k not in base:
            bad.append((k, "metrica nueva: ejecuta --bless"))
            continue
        b = base[k]
        if isinstance(v, list):
            nuevos = sorted(set(v) - set(b))
            pagados = sorted(set(b) - set(v))
            if nuevos:
                bad.append((k, f"REGRESION, nuevos: {', '.join(nuevos)}"))
            if pagados:
                bad.append((k, f"deuda pagada sin blessear ({', '.join(pagados)}): ejecuta --bless en este commit"))
        elif v > b:
            bad.append((k, f"REGRESION: {b} -> {v}"))
        elif v < b:
            bad.append((k, f"mejora sin blessear: {b} -> {v}. Ejecuta --bless en este commit"))
    return bad


def worse(base, cur):
    for k, v in cur.items():
        b = base.get(k)
        if b is None:
            continue
        if isinstance(v, list) and set(v) - set(b):
            return True
        if not isinstance(v, list) and v > b:
            return True
    return False


def summary(m):
    return {k: (len(v) if isinstance(v, list) else v) for k, v in m.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bless", action="store_true")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--no-tools", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    bp = root / "tools" / "ratchet-baseline.json"
    base = json.loads(bp.read_text()) if bp.exists() else {}
    cur = measure(root, tools=not a.no_tools)
    metrics_base = {k: v for k, v in base.items() if k != "coverage_floor"}
    if a.bless:
        if metrics_base and worse(metrics_base, cur):
            for k, msg in compare(metrics_base, cur):
                print(f"RATCHET {k}: {msg}")
            print("RATCHET: --bless rechazado, hay regresiones. Arregla causas, no blessees.")
            return 1
        cur["coverage_floor"] = base.get("coverage_floor", 0)
        bp.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n")
        print("RATCHET: baseline actualizada:", summary(cur))
        return 0
    if not metrics_base:
        print("RATCHET: sin baseline, ejecuta --bless")
        return 1
    bad = compare(metrics_base, cur)
    for k, msg in bad:
        print(f"RATCHET {k}: {msg}")
    print("RATCHET OK" if not bad else "RATCHET FAILED", summary(cur))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
