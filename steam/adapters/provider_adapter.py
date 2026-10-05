"""Adapter de `/info/markets` de steamwebapi → `ProviderInfo`."""
from typing import Any

from steam.adapters._common import mappings, require_list
from steam.domain.models import ProviderInfo
from steam.domain.validators import as_str

SOURCE = "steamwebapi"
OP = "info_markets"
# steamwebapi no documenta el campo del logo; estos son los nombres que se han visto o
# que la API podría usar. El primero con valor gana.
_LOGO_KEYS = ("logo", "logoUrl", "logo_url", "image", "imageUrl", "image_url", "icon", "iconUrl",
              "icon_url", "thumbnail")


def adapt_markets(raw: Any) -> list[ProviderInfo]:
    """Un mercado sin ningún identificador (`id`, `key`, `name`) se descarta: no hay
    con qué casarlo con PROVIDER_IDS."""
    providers: list[ProviderInfo] = []
    for m in mappings(require_list(raw, source=SOURCE, op=OP), source=SOURCE, op=OP):
        mid = as_str(m.get("id") or m.get("key") or m.get("name"), field="id", source=SOURCE, op=OP)
        if not mid:
            continue
        logo = next((v for k in _LOGO_KEYS if (v := as_str(m.get(k), field=k, source=SOURCE, op=OP))), None)
        providers.append(ProviderInfo(id=mid.lower(), name=as_str(m.get("name"), field="name", source=SOURCE, op=OP),
                                      logo=logo))
    return providers
