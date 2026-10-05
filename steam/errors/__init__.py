"""Errores y manejo de degradaciones de steam/.

`domain_errors`: los errores tipados que lanzan clientes, adapters y services.
`handling`: `log_degraded` / `degraded` / `reason_of` / `http_error_for`.
Se re-exporta todo: `from steam.errors import QuotaExhausted` sigue valiendo (lo usan
alerts/, tools/ y las rutas).
"""
from steam.errors.domain_errors import (
    UNEXPECTED_FORMAT, UPSTREAM_QUOTA_DETAIL, UPSTREAM_RATE_LIMIT_DETAIL, HistoryBusy, InvalidField,
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, StorageError,
    UnexpectedPayload, UpstreamError,
)
from steam.errors.handling import Served, degraded, log_degraded, reason_of

__all__ = [
    "UNEXPECTED_FORMAT", "UPSTREAM_QUOTA_DETAIL", "UPSTREAM_RATE_LIMIT_DETAIL", "HistoryBusy",
    "InvalidField", "InvalidPayload", "QuotaExhausted", "RateLimited", "SourceTimeout",
    "SourceUnavailable", "StorageError", "UnexpectedPayload", "UpstreamError", "Served", "degraded",
    "log_degraded", "reason_of",
]
