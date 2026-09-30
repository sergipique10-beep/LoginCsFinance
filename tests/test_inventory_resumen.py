"""PERF-12 — la tool de inventario no puede devolver el inventario entero.

~370 filas eran ~48 KB de functionResponse; a partir de ~20 KB Gemini tarda o da
503 (ver llm/gemini.py). Se agrupa por nombre y se acota a un top por valor.
"""
import json

from tools.inventory_tools import MAX_ITEMS, _resumen_inventario


def _item(name, price, d24=None):
    return {"name": name, "priceLatest": price, "priceDelta24h": d24,
            "priceDelta7d": None, "liquidityScore": 50}


def test_agrupa_duplicados_y_suma_totales():
    items = [_item("Case", 1.0)] * 30 + [_item("AK", 100.0), _item("Sticker", None)]
    r = _resumen_inventario(items)
    assert r["total_items"] == 32
    assert r["distintos"] == 3
    assert r["valor_total"] == 130.0
    assert r["sin_precio"] == 1
    case = next(i for i in r["items"] if i["name"] == "Case")
    assert case["cantidad"] == 30


def test_ordena_por_valor_y_acota():
    items = [_item(f"skin {n}", float(n)) for n in range(200)]
    r = _resumen_inventario(items)
    assert len(r["items"]) == MAX_ITEMS
    assert r["items"][0]["name"] == "skin 199"
    assert r["omitidos"] == 200 - MAX_ITEMS
    # Totales sobre TODO el inventario, no solo lo devuelto.
    assert r["total_items"] == 200


def test_buscar_filtra_sin_distinguir_mayusculas():
    items = [_item("AK-47 | Redline", 10.0), _item("M4A1-S | Printstream", 50.0)]
    r = _resumen_inventario(items, buscar="ak-47")
    assert [i["name"] for i in r["items"]] == ["AK-47 | Redline"]
    assert r["total_items"] == 2


def test_inventario_grande_cabe_holgado_bajo_el_umbral_de_gemini():
    nombre = "StatTrak™ M4A1-S | Printstream (Field-Tested)"
    items = [_item(f"{nombre} {n}", 12.3456, 1.234) for n in range(400)]
    size = len(json.dumps(_resumen_inventario(items), ensure_ascii=False))
    assert size < 5_000, size
