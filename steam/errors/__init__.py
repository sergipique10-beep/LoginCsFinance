"""Errores y manejo de degradaciones de steam/ (CLEAN-14).

`domain_errors`: los errores tipados que lanzan clientes, adapters y services.
`handling`: `log_degraded` / `reason_of` (ex `steam/degraded.py`).
Se re-exporta todo: `from steam.errors import QuotaExhausted` sigue valiendo (lo usan
alerts/, tools/ y las rutas).
"""
from steam.errors.domain_errors import (
    UNEXPECTED_FORMAT, UPSTREAM_QUOTA_DETAIL, UPSTREAM_RATE_LIMIT_DETAIL, HistoryBusy, InvalidField,
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, UnexpectedPayload,
    UpstreamError,
)
from steam.errors.handling import Served, log_degraded, reason_of

__all__ = [
    "UNEXPECTED_FORMAT", "UPSTREAM_QUOTA_DETAIL", "UPSTREAM_RATE_LIMIT_DETAIL", "HistoryBusy",
    "InvalidField", "InvalidPayload", "QuotaExhausted", "RateLimited", "SourceTimeout",
    "SourceUnavailable", "UnexpectedPayload", "UpstreamError", "Served", "log_degraded", "reason_of",
]
