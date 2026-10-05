"""CLEAN-14: el paquete steam/errors/ (ex errors.py + degraded.py)."""
import pytest

from steam import errors
from steam.errors.domain_errors import InvalidField, QuotaExhausted, UnexpectedPayload, UpstreamError
from steam.errors.handling import reason_of


def test_el_paquete_reexporta_lo_publico():
    for name in ("QuotaExhausted", "RateLimited", "HistoryBusy", "InvalidPayload", "UnexpectedPayload",
                 "InvalidField", "UPSTREAM_QUOTA_DETAIL", "log_degraded", "reason_of"):
        assert hasattr(errors, name), name


def test_invalid_field_es_unexpected_payload_con_contexto():
    exc = InvalidField("steamwebapi", "items", "pricelatestsell", {"x": 1})
    assert isinstance(exc, UnexpectedPayload)
    assert not isinstance(exc, UpstreamError)
    assert "steamwebapi.items" in str(exc) and "pricelatestsell" in str(exc) and "dict" in str(exc)
    assert (exc.source, exc.operation, exc.field) == ("steamwebapi", "items", "pricelatestsell")


@pytest.mark.parametrize("exc, reason", [
    (InvalidField("s", "op", "f", 1), "invalid_field"),
    (UnexpectedPayload("x"), "unexpected_format"),
    (QuotaExhausted(), "quota"),
    (UpstreamError(500, "boom"), "http_500"),
    (KeyError("k"), "KeyError"),
])
def test_reason_of(exc, reason):
    assert reason_of(exc) == reason


# ── CLEAN-15: traducción única a HTTP ─────────────────────────────────────────

from fastapi import HTTPException  # noqa: E402 — bloque añadido al final del fichero, como el resto

from steam.domain.enums import FetchStatus, Served  # noqa: E402
from steam.errors.domain_errors import (  # noqa: E402
    HistoryBusy, InvalidPayload, RateLimited, SourceTimeout, SourceUnavailable,
)
from steam.errors.handling import SOURCE_ERRORS, http_error_for, served_to_status, user_message  # noqa: E402


@pytest.mark.parametrize("exc, status, code", [
    (QuotaExhausted(), 503, "upstream_quota"),
    (RateLimited(), 503, "upstream_rate_limit"),
    (HistoryBusy("x"), 503, "upstream_rate_limit"),
    (SourceUnavailable("down"), 502, None),
    (UpstreamError(500, "boom"), 502, None),
    (InvalidPayload("<html>"), 502, None),
    (UnexpectedPayload("dict donde va lista"), 502, None),
    (InvalidField("s", "op", "f", 1), 502, None),
])
def test_http_error_for(exc, status, code):
    err = http_error_for(exc)
    assert isinstance(err, HTTPException) and err.status_code == status
    if code:
        assert err.detail["code"] == code
    else:
        assert isinstance(err.detail, str) and err.detail


def test_http_error_for_timeout_segun_la_ruta():
    assert http_error_for(SourceTimeout("t")).status_code == 502
    assert http_error_for(SourceTimeout("t"), timeout_status=504).status_code == 504


def test_http_error_for_retry_after():
    assert http_error_for(RateLimited()).headers["Retry-After"] == "60"
    assert http_error_for(RateLimited(7.2)).headers["Retry-After"] == "8"
    assert http_error_for(HistoryBusy("x")).headers["Retry-After"] == "60"


def test_source_errors_cubre_lo_que_lanzan_api_y_adapters():
    for exc in (QuotaExhausted(), RateLimited(), SourceTimeout(), SourceUnavailable(), UpstreamError(500),
                InvalidPayload(), UnexpectedPayload(), InvalidField("s", "o", "f", 1), HistoryBusy()):
        assert isinstance(exc, SOURCE_ERRORS), type(exc).__name__
    assert not isinstance(KeyError("bug"), SOURCE_ERRORS)   # un bug sale como 500, no como 502


@pytest.mark.parametrize("exc, fragment", [
    (QuotaExhausted(), "cuota mensual"),
    (RateLimited(), "límite por minuto"),
    (UpstreamError(403, ""), "privado"),
    (UpstreamError(500, ""), "500"),
    (SourceUnavailable("x"), "contactar"),
    (InvalidPayload("<html>"), "formato inesperado"),
    (RuntimeError("red"), "RuntimeError"),
])
def test_user_message_es_legible(exc, fragment):
    assert fragment in user_message(exc)


def test_served_to_status_es_la_unica_correspondencia():
    served: tuple[Served, ...] = ("stale", "empty", "fallback", "error")
    esperado: tuple[FetchStatus, ...] = ("stale", "error", "partial", "error")
    assert tuple(served_to_status(s) for s in served) == esperado
