"""CLEAN-11: reglas de capas de steam/. Los services no saben de FastAPI (lanzan los
errores de steam/errors.py) y las rutas no hablan con las APIs externas: todo pasa
por un service.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # Relativo dentro de steam/: ..clients → steam.clients
            base = ("steam." * bool(node.level)) + (node.module or "")
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return found


def test_services_no_importan_fastapi():
    offenders = [
        p.name for p in (ROOT / "steam" / "services").glob("*.py")
        if any(m.split(".")[0] in {"fastapi", "starlette"} for m in _imports(p))
    ]
    assert offenders == []


def test_rutas_no_importan_los_clientes():
    offenders = [
        p.name for p in (ROOT / "steam" / "routes").glob("*.py")
        if any(m.startswith(("steam.clients", "steam.api")) for m in _imports(p))
    ]
    assert offenders == []


# CLEAN-13: el orden de dependencias de CLAUDE.md, capa por capa. Cada entrada es
# (directorio, prefijos de `steam.` que NO puede importar). `stores` es la caché de la
# raíz: solo services/routes/cache pueden tocarla.
FORBIDDEN = {
    "domain":   ("steam.mappers", "steam.services", "steam.clients", "steam.api", "steam.adapters",
                 "steam.cache", "steam.routes", "stores"),
    "mappers":  ("steam.services", "steam.clients", "steam.api", "steam.adapters", "steam.cache",
                 "steam.routes", "stores"),
    "adapters": ("steam.mappers", "steam.services", "steam.clients", "steam.api", "steam.cache",
                 "steam.routes", "stores"),
    "api":      ("steam.mappers", "steam.services", "steam.adapters", "steam.cache", "steam.routes",
                 "steam.domain", "stores"),
    "clients":  ("steam.mappers", "steam.services", "steam.adapters", "steam.cache", "steam.routes",
                 "steam.domain", "stores"),
    "cache":    ("steam.mappers", "steam.services", "steam.clients", "steam.api", "steam.adapters",
                 "steam.routes", "steam.domain"),
    # CLEAN-17: utils/ no importa nada interno (domain/ sí puede importar de utils/).
    "utils":    ("steam.mappers", "steam.services", "steam.clients", "steam.api", "steam.adapters",
                 "steam.cache", "steam.routes", "steam.domain", "steam.errors", "stores", "settings"),
}


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_orden_de_dependencias(layer):
    folder = ROOT / "steam" / layer
    if not folder.is_dir():
        pytest.skip(f"steam/{layer} aún no existe")
    offenders = sorted(
        f"{p.name} → {m}"
        for p in folder.glob("*.py")
        for m in _imports(p)
        if any(m == bad or m.startswith(bad + ".") for bad in FORBIDDEN[layer])
    )
    assert offenders == []


def test_la_guardia_detecta_un_import_relativo(tmp_path):
    fake = tmp_path / "x.py"
    fake.write_text("from ..clients import steamwebapi\n", encoding="utf-8")
    assert "steam.clients" in _imports(fake)
