"""tools/ratchet.py: contadores de deuda que solo bajan (AOS-L3).

Se prueba contra un arbol temporal, no contra el repo real. `--no-tools` omite ruff y
mypy para que el test no dependa de que esten instalados. Cada aserion sobre el
veredicto mira la LINEA de la metrica concreta (`RATCHET <metrica>: ...`), no el
resumen final, que nombra todas las metricas siempre (revision del 2026-10-04, I-6).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

RATCHET = Path(__file__).resolve().parents[1] / "tools" / "ratchet.py"


def _tree(tmp_path, tested=("auth/router.py",), untested=()):
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs" / "features").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    for f in tested + untested:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.parent / "__init__.py").touch() if p.parent != tmp_path else None
        p.write_text("x = 1\n")
    for f in tested:
        mod = f[:-3].replace("/", ".")
        (tmp_path / "tests" / f"test_{mod.replace('.', '_')}.py").write_text(f"from {mod} import x\n")
    return tmp_path


def run(cwd, *args, env=None):
    return subprocess.run(  # noqa: S603 — argumentos fijos del propio test, sin entrada externa
        [sys.executable, str(RATCHET), "--root", str(cwd), "--no-tools", *args],
        capture_output=True, text=True, env=env,
    )


def baseline(t):
    return json.loads((t / "tools" / "ratchet-baseline.json").read_text())


def metric_line(out, metric):
    m = re.search(rf"^RATCHET {metric}: (.*)$", out, re.M)
    return m.group(1) if m else ""


# --- arranque y las dos direcciones ------------------------------------------------

def test_init_then_ok(tmp_path):
    t = _tree(tmp_path, untested=("steam/new.py",))
    r = run(t, "--init")
    assert r.returncode == 0, r.stdout
    assert baseline(t)["files_without_test"] == ["steam/new.py"]
    assert baseline(t)["coverage_floor"] == 0
    assert run(t).returncode == 0


def test_new_file_without_test_fails_on_that_metric(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    (t / "steam").mkdir()
    (t / "steam" / "__init__.py").touch()
    (t / "steam" / "otro.py").write_text("y = 2\n")
    r = run(t)
    assert r.returncode == 1
    assert "REGRESION" in metric_line(r.stdout, "files_without_test")
    assert "steam/otro.py" in metric_line(r.stdout, "files_without_test")


def test_paying_debt_without_bless_fails_and_says_bless(tmp_path):
    t = _tree(tmp_path, untested=("steam/new.py",))
    run(t, "--init")
    (t / "tests" / "test_steam_new.py").write_text("from steam.new import x\n")
    r = run(t)
    assert r.returncode == 1 and "--bless" in metric_line(r.stdout, "files_without_test")
    assert run(t, "--bless").returncode == 0
    assert baseline(t)["files_without_test"] == []


def test_bless_refuses_when_worse(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    (t / "jobs.py").write_text("y = 2\n")
    r = run(t, "--bless")
    assert r.returncode == 1 and "rechazado" in r.stdout


# --- descubrimiento de fuentes (I-1) y frontmatter exacto (I-2) ---------------------

def test_new_root_module_and_new_package_are_discovered(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    (t / "jobs.py").write_text("y = 2\n")
    (t / "watchlist").mkdir()
    (t / "watchlist" / "__init__.py").touch()
    (t / "watchlist" / "service.py").write_text("z = 3\n")
    r = run(t)
    line = metric_line(r.stdout, "files_without_test")
    assert r.returncode == 1 and "jobs.py" in line and "watchlist/service.py" in line


def test_non_empty_init_counts_as_source(tmp_path):
    t = _tree(tmp_path)
    (t / "auth" / "__init__.py").write_text("from .router import x\n")
    run(t, "--init")
    # importar auth.router ejecuta auth/__init__.py: cuenta como testeado, pero no como documentado
    assert "auth/__init__.py" not in baseline(t)["files_without_test"]
    assert "auth/__init__.py" in baseline(t)["undocumented_files"]


def test_tests_venv_docs_and_tooling_are_not_sources(tmp_path):
    t = _tree(tmp_path)
    (t / "venv" / "lib").mkdir(parents=True)
    (t / "venv" / "lib" / "x.py").write_text("v = 1\n")
    (t / "docs" / "snippet.py").write_text("d = 1\n")
    (t / "tools" / "ratchet.py").write_text("r = 1\n")
    (t / "tools" / "dod.py").write_text("r = 1\n")
    run(t, "--init")
    assert baseline(t)["files_without_test"] == []


def test_documented_only_via_frontmatter_files(tmp_path):
    t = _tree(tmp_path, tested=("auth/router.py", "stores.py"))
    run(t, "--init")
    (t / "docs" / "features" / "auth.md").write_text(
        "---\nfeature: auth\nfiles: [auth/router.py]\n---\nVer tambien stores.py en price_stores.py.\n"
    )
    r = run(t)
    assert "auth/router.py" in metric_line(r.stdout, "undocumented_files")
    assert run(t, "--bless").returncode == 0
    assert baseline(t)["undocumented_files"] == ["stores.py"]   # la mencion en prosa no documenta


def test_frontmatter_files_multiline_list(tmp_path):
    t = _tree(tmp_path, tested=("auth/router.py", "stores.py"))
    run(t, "--init")
    (t / "docs" / "features" / "auth.md").write_text(
        "---\nfiles:\n  - auth/router.py\n  - stores.py\n---\n"
    )
    run(t, "--bless")
    assert baseline(t)["undocumented_files"] == []


# --- la baseline no se puede sortear (I-3) -------------------------------------------

def test_bless_without_baseline_is_refused(tmp_path):
    t = _tree(tmp_path)
    r = run(t, "--bless")
    assert r.returncode == 1 and "--init" in r.stdout
    assert not (t / "tools" / "ratchet-baseline.json").exists()


def test_init_refuses_to_overwrite(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    r = run(t, "--init")
    assert r.returncode == 1 and "ya existe" in r.stdout


def test_bless_refuses_baseline_with_missing_key(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    b = baseline(t)
    del b["files_without_test"]
    (t / "tools" / "ratchet-baseline.json").write_text(json.dumps(b))
    (t / "jobs.py").write_text("y = 2\n")
    r = run(t, "--bless")
    assert r.returncode == 1 and "files_without_test" in r.stdout
    r = run(t)
    assert r.returncode == 1


def test_bless_keeps_coverage_floor(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    b = baseline(t)
    b["coverage_floor"] = 68
    (t / "tools" / "ratchet-baseline.json").write_text(json.dumps(b))
    assert run(t, "--bless").returncode == 0
    assert baseline(t)["coverage_floor"] == 68


def test_compare_baseline_fails_if_counts_rise_or_floor_drops(tmp_path):
    t = _tree(tmp_path)
    run(t, "--init")
    prev = baseline(t)
    prev["coverage_floor"] = 70
    (t / "prev.json").write_text(json.dumps(prev))
    cur = dict(prev, coverage_floor=68)
    (t / "tools" / "ratchet-baseline.json").write_text(json.dumps(cur))
    r = run(t, "--compare-baseline", str(t / "prev.json"))
    assert r.returncode == 1 and "coverage_floor" in r.stdout
    cur = dict(prev, files_without_test=["a.py"])
    (t / "tools" / "ratchet-baseline.json").write_text(json.dumps(cur))
    r = run(t, "--compare-baseline", str(t / "prev.json"))
    assert r.returncode == 1 and "files_without_test" in r.stdout
    (t / "tools" / "ratchet-baseline.json").write_text(json.dumps(prev))
    assert run(t, "--compare-baseline", str(t / "prev.json")).returncode == 0


# --- deteccion de imports y de binds (M-1, M-2) --------------------------------------

def test_import_in_docstring_does_not_count_but_package_import_does(tmp_path):
    t = _tree(tmp_path, untested=("steam/new.py", "steam/router.py"))
    (t / "tests" / "test_a.py").write_text('"""\nfrom steam.new import z\n"""\n')
    (t / "tests" / "test_b.py").write_text("def f():\n    from steam import router\n    return router\n")
    run(t, "--init")
    assert baseline(t)["files_without_test"] == ["steam/new.py"]


def test_bind_all_interfaces_counts_both_quote_styles(tmp_path):
    t = _tree(tmp_path)
    (t / "main.py").write_text("uvicorn.run(app, host=\"0.0.0.0\")\nother(host='0.0.0.0')\n")
    run(t, "--init")
    assert baseline(t)["bind_all_interfaces"] == 2


# --- prerrequisitos ausentes (Review Focus 3, I-7) -----------------------------------

def test_missing_interpreter_is_red(tmp_path):
    t = _tree(tmp_path)
    env = dict(os.environ, RATCHET_PYTHON=str(tmp_path / "no-existe" / "python"))
    r = subprocess.run(  # noqa: S603 — argumentos fijos del propio test
        [sys.executable, str(RATCHET), "--root", str(t), "--init"], capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1 and "prerrequisito ausente" in r.stdout


def test_interpreter_without_ruff_is_red(tmp_path):
    """Un Python real sin ruff instalado: la rama 'No module named'."""
    venv = tmp_path / "v"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)  # noqa: S603
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    t = _tree(tmp_path)
    env = dict(os.environ, RATCHET_PYTHON=str(py))
    r = subprocess.run(  # noqa: S603 — argumentos fijos del propio test
        [sys.executable, str(RATCHET), "--root", str(t), "--init"], capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1 and "prerrequisito ausente: ruff" in r.stdout
