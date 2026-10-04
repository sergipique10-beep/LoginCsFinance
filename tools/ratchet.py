#!/usr/bin/env python
"""tools/ratchet.py — contadores de deuda que solo bajan (AOS-L3 §3.3).

    python tools/ratchet.py                      verifica contra tools/ratchet-baseline.json
    python tools/ratchet.py --bless              reescribe la baseline (solo si nada empeora)
    python tools/ratchet.py --init               crea la baseline la PRIMERA vez (se niega si existe)
    python tools/ratchet.py --compare-baseline <json>
                                                 compara la baseline del arbol con otra (CI: la del
                                                 commit anterior) y falla si algo subio o el suelo bajo
    --root <dir>   arbol a medir (tests);  --no-tools  omite ruff/mypy (tests)
    RATCHET_PYTHON  interprete con el que se lanzan ruff/mypy (por defecto, este)

Falla en las DOS direcciones: si una metrica sube (regresion) y si baja sin blessear
(la baseline debe reflejar la realidad en el mismo commit; regla A-8 de GUARDRAILS).
`--bless` exige una baseline completa: borrar el fichero o una clave no sirve para
sortearlo (revision 2026-10-04, I-3); el primer arranque es `--init`, explicito.

Metricas:
  ruff                  violaciones de ruff (ruff.toml)            # ponytail: conteo, no lista;
  mypy                  errores de mypy                              permite "quitar una, meter otra"
  bind_all_interfaces   literales '0.0.0.0' / "0.0.0.0" en el codigo (A-7)
  files_without_test    lista: ningun tests/test_*.py importa el modulo o su paquete (AST, EDS-3 §3.2)
  undocumented_files    lista: ningun docs/features/*.md lo cita en el frontmatter `files:` (AOS-DOC, A-9)
  coverage_floor        suelo de cobertura (lo lee tools/dod.py; manual, solo sube; lo vigila --compare-baseline)

Fuentes: todos los *.py de la raiz y de cada paquete (directorio con __init__.py), por
descubrimiento, no por lista (I-1). Fuera: tests/, venv/, docs/, el tooling del DoD y los
scripts manuales de EXCLUDE. Un __init__.py cuenta solo si tiene codigo.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXCLUDE_DIRS = {"tests", "venv", "docs", "logs", ".superpowers", ".claude", ".git", "__pycache__"}
EXCLUDE = {"tools/ratchet.py", "tools/dod.py", "verify_weapon_category.py"}
PY = os.environ.get("RATCHET_PYTHON", sys.executable)
BIND_RE = re.compile(r"""['"]0\.0\.0\.0['"]""")


def rel(root, p):
    return p.relative_to(root).as_posix()


def source_files(root):
    out = [p for p in root.glob("*.py")]
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in EXCLUDE_DIRS):
        if (d / "__init__.py").exists():
            out += [p for p in d.rglob("*.py") if "__pycache__" not in p.parts]
    keep = []
    for p in sorted(set(out)):
        r = rel(root, p)
        if r in EXCLUDE:
            continue
        if p.name == "__init__.py" and not p.read_text(encoding="utf-8", errors="ignore").strip():
            continue
        keep.append(p)
    return keep


def imported_modules(tests_dir):
    """Modulos importados por los tests, via AST (un import dentro de un docstring no cuenta)."""
    mods = set()
    for p in tests_dir.glob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
                mods.update(f"{node.module}.{a.name}" for a in node.names)
    return mods


def documented_files(features_dir):
    """Rutas listadas en el frontmatter `files:` de docs/features/*.md (inline o multilinea)."""
    files = set()
    for p in features_dir.glob("*.md"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = re.match(r"\s*---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        fm = m.group(1)
        inline = re.search(r"^files:\s*\[(.*?)\]", fm, re.M | re.S)
        if inline:
            files.update(x.strip().strip("'\"") for x in inline.group(1).split(",") if x.strip())
            continue
        block = re.search(r"^files:\s*\n((?:\s+-\s+.*\n?)+)", fm, re.M)
        if block:
            files.update(re.sub(r"^\s*-\s+", "", ln).strip().strip("'\"") for ln in block.group(1).splitlines() if ln.strip())
    return files


def tool_count(root, cmd, pred, ok_codes):
    try:
        r = subprocess.run([PY, "-m", *cmd], cwd=root, capture_output=True, text=True)  # noqa: S603 — comandos fijos
    except FileNotFoundError:
        print(f"RATCHET: prerrequisito ausente: interprete {PY} ({cmd[0]})")
        sys.exit(1)
    if "No module named" in r.stderr:
        print(f"RATCHET: prerrequisito ausente: {cmd[0]} (pip install -r requirements-dev.txt)")
        sys.exit(1)
    if r.returncode not in ok_codes:
        print(f"RATCHET: {cmd[0]} termino con rc={r.returncode} (crash, no veredicto):\n{r.stderr[-800:]}")
        sys.exit(1)
    return pred(r.stdout)


def measure(root, tools=True):
    files = source_files(root)
    mods = imported_modules(root / "tests") if (root / "tests").is_dir() else set()
    docs = documented_files(root / "docs" / "features") if (root / "docs" / "features").is_dir() else set()
    m = {}
    without_test = []
    for f in files:
        r = rel(root, f)
        mod = r[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        # importado el modulo, algo de dentro, o el paquete que lo contiene (`from steam import router`)
        if not (mod in mods or any(x.startswith(mod + ".") for x in mods)):
            without_test.append(r)
    m["files_without_test"] = without_test
    m["undocumented_files"] = [rel(root, f) for f in files if rel(root, f) not in docs]
    m["bind_all_interfaces"] = sum(len(BIND_RE.findall(p.read_text(encoding="utf-8", errors="ignore"))) for p in files)
    if tools:
        m["ruff"] = tool_count(root, ["ruff", "check", ".", "--output-format", "json", "--exit-zero"],
                               lambda out: len(json.loads(out or "[]")), ok_codes={0})
        m["mypy"] = tool_count(root, ["mypy", ".", "--ignore-missing-imports", "--exclude", "venv",
                                      "--no-error-summary"],
                               lambda out: sum(1 for ln in out.splitlines() if ": error:" in ln), ok_codes={0, 1})
    return m


def compare(base, cur):
    """Lista de (metrica, mensaje). Vacia = OK."""
    bad = []
    for k, v in cur.items():
        if k not in base:
            bad.append((k, "falta en la baseline: la baseline debe estar completa (--init si es la primera vez)"))
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
        if k not in base:
            return True
        b = base[k]
        if isinstance(v, list) and set(v) - set(b):
            return True
        if not isinstance(v, list) and v > b:
            return True
    return False


def summary(m):
    return {k: (len(v) if isinstance(v, list) else v) for k, v in m.items()}


def write_baseline(bp, data):
    bp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def compare_baselines(prev, cur):
    """CI: la baseline nueva frente a la del commit anterior. Nada puede subir; el suelo no puede bajar."""
    bad = []
    for k, v in cur.items():
        if k == "coverage_floor":
            if k in prev and v < prev[k]:
                bad.append(f"coverage_floor bajo: {prev[k]} -> {v} (solo sube)")
            continue
        if k not in prev:
            continue
        if isinstance(v, list):
            nuevos = sorted(set(v) - set(prev[k]))
            if nuevos:
                bad.append(f"{k} subio: nuevos {', '.join(nuevos)}")
        elif v > prev[k]:
            bad.append(f"{k} subio: {prev[k]} -> {v}")
    for k in prev:
        if k not in cur:
            bad.append(f"{k} desaparecio de la baseline")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bless", action="store_true")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--compare-baseline", metavar="JSON")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--no-tools", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    bp = root / "tools" / "ratchet-baseline.json"
    base = json.loads(bp.read_text(encoding="utf-8")) if bp.exists() else None

    if a.compare_baseline:
        if base is None:
            print("RATCHET: no hay tools/ratchet-baseline.json que comparar")
            return 1
        prev = json.loads(Path(a.compare_baseline).read_text(encoding="utf-8"))
        bad = compare_baselines(prev, base)
        for msg in bad:
            print(f"RATCHET baseline: {msg}")
        print("RATCHET BASELINE OK (nada subio, el suelo no bajo)" if not bad else "RATCHET BASELINE FAILED")
        return 1 if bad else 0

    cur = measure(root, tools=not a.no_tools)

    if a.init:
        if base is not None:
            print(f"RATCHET: {bp.relative_to(root).as_posix()} ya existe; --init es solo para la primera vez. Usa --bless.")
            return 1
        cur["coverage_floor"] = 0
        write_baseline(bp, cur)
        print("RATCHET: baseline creada:", summary(cur))
        return 0

    if base is None:
        print("RATCHET: sin baseline. Primera vez: --init (y commitea tools/ratchet-baseline.json)")
        return 1
    if "coverage_floor" not in base:
        print("RATCHET: la baseline no tiene coverage_floor: baseline incompleta, restaurala desde git")
        return 1
    metrics_base = {k: v for k, v in base.items() if k != "coverage_floor"}

    if a.bless:
        missing = [k for k in cur if k not in metrics_base]
        if missing:
            print(f"RATCHET: --bless rechazado, faltan claves en la baseline: {', '.join(missing)}. Restaurala desde git.")
            return 1
        if worse(metrics_base, cur):
            for k, msg in compare(metrics_base, cur):
                print(f"RATCHET {k}: {msg}")
            print("RATCHET: --bless rechazado, hay regresiones. Arregla causas, no blessees.")
            return 1
        cur["coverage_floor"] = base["coverage_floor"]
        write_baseline(bp, cur)
        print("RATCHET: baseline actualizada:", summary(cur))
        return 0

    bad = compare(metrics_base, cur)
    for k, msg in bad:
        print(f"RATCHET {k}: {msg}")
    print("RATCHET OK" if not bad else "RATCHET FAILED", summary(cur))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
