from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.service import require_jwt
from . import repo

router = APIRouter()

# Los providers que el selector del frontend ofrece para el portfolio. Se valida
# contra una lista cerrada porque un provider libre dejaría escribir filas basura:
# la PK es (steam_id, provider, date), así que cada cadena distinta es una serie
# nueva. Ojo: NO es `_VALID_MARKETS` de steam/routes/market.py, que lista los 12
# mercados de steamwebapi pero no incluye "steam" — aquí manda lo que el front usa
# en `priceForItem()` (inventory.ts).
VALID_PROVIDERS = frozenset({"steam", "csfloat", "buff"})


class SnapshotBody(BaseModel):
    # El dueño NO va en el body: sale del sub del JWT, como en alerts. Un steam_id
    # que mande el cliente se ignora — si se aceptara, cualquiera escribiría en la
    # serie de otro usuario.
    provider: str = Field(min_length=1, max_length=32)
    value: float = Field(ge=0, le=100_000_000)


def _check_provider(provider: str) -> str:
    provider = provider.lower().strip()
    if provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{provider}'. Valid: {', '.join(sorted(VALID_PROVIDERS))}",
        )
    return provider


@router.get("/portfolio/history", summary="Histórico del valor de la cartera del usuario")
async def get_history(provider: str, payload: dict = Depends(require_jwt)):
    """Serie del usuario para ese provider, de más antigua a más reciente (UX-16)."""
    return {"history": await repo.list_for_user(payload["sub"], _check_provider(provider))}


@router.post("/portfolio/snapshot", status_code=201, summary="Graba el valor de la cartera de hoy")
async def post_snapshot(body: SnapshotBody, payload: dict = Depends(require_jwt)):
    """Upsert del punto de hoy. Repetirlo el mismo día sobrescribe, no duplica.

    La fecha la pone el servidor (ver repo.upsert_today): un reloj mal puesto en el
    dispositivo no debe poder sembrar la serie con fechas futuras.
    """
    return await repo.upsert_today(payload["sub"], _check_provider(body.provider), body.value)
