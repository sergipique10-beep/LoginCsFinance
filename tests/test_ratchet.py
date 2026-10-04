"""tools/ratchet.py: contadores de deuda que solo bajan (AOS-L3).

Se prueba contra un arbol temporal, no contra el repo real. `--no-tools` omite ruff y
mypy para que el test no dependa de que esten instalados.
"""
import json
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
        p.write_text("x = 1\n")
    for f in tested:
        mod = f[:-3].replace("/", ".")
        (tmp_path / "tests" / f"test_{mod.replace('.', '_')}.py").write_text(f"from {mod} import x\n")
    return tmp_path


def run(cwd, *args):
    return subprocess.run(  # noqa: S603 — argumentos fijos del propio test, sin entrada externa
        [sys.executable, str(RATCHET), "--root", str(cwd), "--no-tools", *args],
        capture_output=True, text=True,
    )


def test_bless_then_ok(tmp_path):
    t = _tree(tmp_path, untested=("steam/new.py",))
    assert run(t, "--bless").returncode == 0
    base = json.loads((t / "tools" / "ratchet-baseline.json").read_text())
    assert base["files_without_test"] == ["steam/new.py"]
    assert "coverage_floor" in base
    r = run(t)
    assert r.returncode == 0, r.stdout


def test_new_file_without_test_fails_naming_it(tmp_path):
    t = _tree(tmp_path)
    run(t, "--bless")
    (t / "steam").mkdir(exist_ok=True)
    (t / "steam" / "otro.py").write_text("y = 2\n")
    r = run(t)
    assert r.returncode == 1
    assert "files_without_test" in r.stdout and "steam/otro.py" in r.stdout


def test_paying_debt_without_bless_fails_and_says_bless(tmp_path):
    t = _tree(tmp_path, untested=("steam/new.py",))
    run(t, "--bless")
    (t / "tests" / "test_steam_new.py").write_text("from steam.new import x\n")
    r = run(t)
    assert r.returncode == 1 and "--bless" in r.stdout


def test_bless_refuses_when_worse(tmp_path):
    t = _tree(tmp_path)
    run(t, "--bless")
    (t / "steam").mkdir(exist_ok=True)
    (t / "steam" / "otro.py").write_text("y = 2\n")
    r = run(t, "--bless")
    assert r.returncode == 1 and "rechazado" in r.stdout


def test_bind_all_interfaces_counted(tmp_path):
    t = _tree(tmp_path)
    (t / "main.py").write_text('uvicorn.run(app, host="0.0.0.0")\n')
    run(t, "--bless")
    base = json.loads((t / "tools" / "ratchet-baseline.json").read_text())
    assert base["bind_all_interfaces"] == 1


def test_documented_file_leaves_undocumented_list(tmp_path):
    t = _tree(tmp_path)
    run(t, "--bless")
    (t / "docs" / "features" / "auth.md").write_text("---\nfiles: [auth/router.py]\n---\n")
    r = run(t)
    assert r.returncode == 1 and "undocumented_files" in r.stdout and "--bless" in r.stdout
    assert run(t, "--bless").returncode == 0
    base = json.loads((t / "tools" / "ratchet-baseline.json").read_text())
    assert base["undocumented_files"] == []


def test_missing_tool_is_red_not_green(tmp_path):
    """Sin ruff/mypy el ratchet debe ponerse en rojo con el motivo, nunca verde por omision."""
    import os
    t = _tree(tmp_path)
    env = dict(os.environ, RATCHET_PYTHON=str(tmp_path / "no-existe" / "python"))
    r = subprocess.run(  # noqa: S603 — argumentos fijos del propio test, sin entrada externa
        [sys.executable, str(RATCHET), "--root", str(t), "--bless"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1 and "prerrequisito ausente" in r.stdout
