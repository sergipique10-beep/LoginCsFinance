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
