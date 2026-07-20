"""Tests de predict/trend.py — cálculo puro de tendencia."""

import pytest

from predict.trend import calcular, HORIZONTE_DIAS, UMBRAL_PCT, MIN_PUNTOS


def _make_series(start_price: float, daily_change: float, n: int = 30) -> list[dict]:
    """Genera una serie sintética con cambio lineal constante."""
    return [
        {"date": f"2026-01-{i+1:02d}", "price": start_price + daily_change * i}
        for i in range(n)
    ]


class TestClasificacion:
    def test_serie_al_alza(self):
        pts = _make_series(10.0, 0.5)  # +$0.50/día → ~+35% en 7d
        out = calcular(pts)
        assert out["tendencia"] == "sube"
        assert out["cambio_estimado_pct"] > UMBRAL_PCT

    def test_serie_a_la_baja(self):
        pts = _make_series(20.0, -0.5)  # -$0.50/día → ~-17.5% en 7d
        out = calcular(pts)
        assert out["tendencia"] == "baja"
        assert out["cambio_estimado_pct"] < -UMBRAL_PCT

    def test_serie_plana(self):
        pts = _make_series(15.0, 0.0)  # sin cambio
        out = calcular(pts)
        assert out["tendencia"] == "estable"
        assert abs(out["cambio_estimado_pct"]) <= UMBRAL_PCT

    def test_cambio_ligero(self):
        # Cambio pequeño dentro del umbral
        pts = _make_series(100.0, 0.01)  # +$0.01/día = +0.07% en 7d
        out = calcular(pts)
        assert out["tendencia"] == "estable"


class TestConfianza:
    def test_serie_lineal_r2_alto(self):
        # Perfectamente lineal → R² = 1.0
        pts = _make_series(10.0, 0.5)
        out = calcular(pts)
        assert out["confianza"] == "alta"

    def test_serie_ruidosa_r2_bajo(self):
        import random
        random.seed(42)
        pts = [
            {"date": f"2026-01-{i+1:02d}", "price": 10.0 + random.uniform(-3, 3)}
            for i in range(30)
        ]
        out = calcular(pts)
        # Con ruido alto, R² será bajo
        assert out["confianza"] in ("media", "baja")

    def test_estable_baja_confianza(self):
        # Serie plana con ruido: R² bajo → confianza baja aunque sea "estable"
        import random
        random.seed(99)
        pts = [
            {"date": f"2026-01-{i+1:02d}", "price": 10.0 + random.uniform(-0.1, 0.1)}
            for i in range(30)
        ]
        out = calcular(pts)
        assert out["tendencia"] == "estable"
        assert out["confianza"] == "baja"


class TestSalvaguardas:
    def test_lista_vacia(self):
        out = calcular([])
        assert out["tendencia"] == "desconocida"
        assert "sin datos" in out["motivo"]

    def test_un_punto(self):
        out = calcular([{"date": "2026-01-01", "price": 10.0}])
        assert out["tendencia"] == "desconocida"

    def test_menos_de_14_puntos(self):
        pts = _make_series(10.0, 0.5, n=MIN_PUNTOS - 1)
        out = calcular(pts)
        assert out["tendencia"] == "desconocida"
        assert "insuficientes" in out["motivo"]

    def test_precios_cero_filtrados(self):
        pts = [{"date": f"d{i}", "price": 0} for i in range(20)]
        out = calcular(pts)
        assert out["tendencia"] == "desconocida"

    def test_outliers_descartados(self):
        # Serie normal + un outlier de 1000×
        pts = _make_series(10.0, 0.1, n=30)
        pts[15] = {"date": "outlier", "price": 10000.0}
        out = calcular(pts)
        # El outlier debe ser descartado; la serie sube
        assert out["tendencia"] in ("sube", "estable")
        assert out["dias_analizados"] <= 30

    def test_nunca_lanza(self):
        # Datos malos no deben causar excepción
        bad = [{"date": "x", "price": -5}, {"date": "y", "price": None}]
        out = calcular(bad)
        assert out["tendencia"] == "desconocida"


class TestMetadatos:
    def test_dias_analizados(self):
        pts = _make_series(10.0, 0.1, n=25)
        out = calcular(pts)
        assert out["dias_analizados"] == 25

    def test_precio_actual(self):
        pts = _make_series(10.0, 0.5, n=30)
        out = calcular(pts)
        assert out["precio_actual"] == pytest.approx(24.5, abs=0.01)

    def test_cambio_estimado_redondeado(self):
        pts = _make_series(10.0, 0.5)
        out = calcular(pts)
        assert isinstance(out["cambio_estimado_pct"], float)


class TestMinimosCuadrados:
    def test_pendiente_esperada(self):
        """Verificación directa de la fórmula de mínimos cuadrados."""
        # Serie perfecta: y = 2x + 5 → b=2, a=5
        pts = [{"date": f"d{i}", "price": 2 * i + 5} for i in range(20)]
        out = calcular(pts)
        assert out["tendencia"] == "sube"
        # Con b=2, cambio estimado = 2*7 = 14 sobre precio_actual=43 → ~32.56%
        assert out["cambio_estimado_pct"] == pytest.approx(32.56, abs=0.5)
