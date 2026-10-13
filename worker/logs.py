"""Logging del worker: una línea por evento y ningún secreto en claro (COST-02).

`SecretFilter` sustituye por `***` los valores de las variables de `settings.SECRET_ENV`
(STEAM_WEB_API_KEY la primera) y cualquier `key=`/`api=`/`token=` de una URL.
"""
import logging
import os
import re

from worker import settings

_QUERY_SECRET = re.compile(r"(?i)\b(key|api|token|password)=([^&\s\"']+)")


class SecretFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        if secrets is None:
            secrets = [os.environ.get(n, "") for n in settings.SECRET_ENV]
        # Los más largos primero, por si uno contiene a otro; nada de menos de 6 caracteres.
        self.secrets = sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True)

    def mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return _QUERY_SECRET.sub(r"\1=***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask(record.getMessage())
        record.args = None
        return True


def configure(level: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SecretFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    # httpx registra cada URL a INFO, con su query: que pase por el filtro pero a WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
