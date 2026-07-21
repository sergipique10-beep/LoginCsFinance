"""Tests del modelo de tendencia y su backtest walk-forward. Cálculo puro, sin red."""

from predict.backtest import walk_forward, MIN_TRAIN, MIN_FOLDS
from predict.trend import calcular, MODEL_VERSION, HORIZONTE_MAX


def _serie(precios):
    return [{"date": f"2026-01-{i+1:02d}", "price": p} for i, p in enumerate(precios)]


# ── Contrato de salida ─────────────────────────────────────────────────────────

def test_tendencia_alcista_devuelve_intervalo_y_metadatos():
    pts = _serie([10 + i * 0.5 for i in range(30)])  # subida limpia
    out = calcular(pts, horizon_days=7)

    assert out["tendencia"] == "sube"
    assert out["model_version"] == MODEL_VERSION
    assert out["horizon_days"] == 7
    # La estimación puntual NUNCA va sola: siempre con intervalo que la contiene.
    assert out["intervalo"]["min"] <= out["precio_estimado"] <= out["intervalo"]["max"]
    assert "backtest" in out


def test_tendencia_bajista():
    pts = _serie([50 - i * 0.4 for i in range(30)])
    assert calcular(pts)["tendencia"] == "baja"


def test_serie_plana_es_estable():
    pts = _serie([20.0] * 30)
    assert calcular(pts)["tendencia"] == "estable"


def test_horizonte_se_acota_al_maximo():
    pts = _serie([10 + i * 0.5 for i in range(30)])
    assert calcular(pts, horizon_days=999)["horizon_days"] == HORIZONTE_MAX


def test_horizonte_mayor_ensancha_el_intervalo():
    pts = _serie([10 + i * 0.5 + (i % 3) * 0.2 for i in range(40)])
    corto = calcular(pts, horizon_days=1)["intervalo"]
    largo = calcular(pts, horizon_days=30)["intervalo"]
    assert (largo["max"] - largo["min"]) > (corto["max"] - corto["min"])


# ── Salvaguardas ───────────────────────────────────────────────────────────────

def test_datos_insuficientes_devuelve_desconocida():
    out = calcular(_serie([10, 11, 12]))
    assert out["tendencia"] == "desconocida"
    assert out["model_version"] == MODEL_VERSION  # los metadatos siguen presentes


def test_sin_datos_no_lanza():
    assert calcular([])["tendencia"] == "desconocida"


def test_precios_no_positivos_se_descartan():
    pts = _serie([0, -5] + [10 + i * 0.5 for i in range(20)])
    assert calcular(pts)["tendencia"] in {"sube", "baja", "estable"}


# ── Backtest walk-forward ──────────────────────────────────────────────────────

def test_walk_forward_serie_corta_devuelve_none():
    # Menos puntos que MIN_TRAIN + horizonte + MIN_FOLDS → no evaluable.
    assert walk_forward([10.0] * (MIN_TRAIN + 1), horizon=7) is None


def test_walk_forward_bate_a_naive_en_tendencia_lineal():
    # En una recta perfecta el modelo lineal acierta y el naive se queda atrás.
    precios = [10 + i * 0.5 for i in range(60)]
    bt = walk_forward(precios, horizon=7)
    assert bt is not None
    assert bt["folds"] >= MIN_FOLDS
    assert bt["beats_naive"] is True
    assert bt["mae_model"] < bt["mae_naive"]


def test_walk_forward_no_bate_a_naive_en_random_walk():
    # Random walk: el último valor es el estimador óptimo del siguiente, así que
    # extrapolar la pendiente local sobreajusta y pierde contra naive. Es el caso
    # que justifica el gate — semilla fija para que el test sea determinista.
    import random
    rng = random.Random(42)
    precio = 100.0
    precios = []
    for _ in range(80):
        precio = max(1.0, precio + rng.gauss(0, 3))
        precios.append(precio)

    bt = walk_forward(precios, horizon=7)
    assert bt is not None
    assert bt["beats_naive"] is False
    assert bt["mae_model"] > bt["mae_naive"]


def test_gate_degrada_confianza_si_no_bate_a_naive():
    # Serie ruidosa pero con deriva suficiente para no ser "estable".
    precios = [10 + i * 0.3 + (4 if i % 2 else -4) for i in range(60)]
    out = calcular(_serie(precios), horizon_days=7)
    if out["backtest"]["evaluado"] and not out["backtest"]["supera_naive"]:
        assert out["confianza"] == "baja"


def test_backtest_reporta_mae_de_ambos():
    out = calcular(_serie([10 + i * 0.5 for i in range(60)]), horizon_days=7)
    bt = out["backtest"]
    assert bt["evaluado"] is True
    assert bt["mae_modelo"] >= 0 and bt["mae_naive"] >= 0
    assert bt["supera_naive"] is True
