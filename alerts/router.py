import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth.service import require_jwt
from settings import ALERTS_TICK_TOKEN
from . import repo, service

router = APIRouter()


class CreateAlertBody(BaseModel):
    # El dueño NO va en el body: sale del sub del JWT. Cualquier steam_id que
    # mande el cliente se ignora (extra="ignore" es el default de Pydantic).
    market_hash_name: str = Field(min_length=1, max_length=service.MAX_NAME_LEN)
    direction: Literal["above", "below"]
    threshold: float = Field(gt=0, le=1_000_000)


@router.get("/alerts", summary="Alertas de precio del usuario")
async def list_alerts(payload: dict = Depends(require_jwt)):
    return {"alerts": await repo.list_for_user(payload["sub"])}


@router.post("/alerts", status_code=201, summary="Crea una alerta de precio (un solo disparo)")
async def create_alert(body: CreateAlertBody, request: Request, payload: dict = Depends(require_jwt)):
    try:
        return await service.create_alert(
            request.app.state.http_client,
            payload["sub"],
            body.market_hash_name.strip(),
            body.direction,
            body.threshold,
        )
    except service.LimitReached as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except service.Duplicate as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except service.UnknownItem as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except service.PriceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/alerts/{alert_id}", summary="Borra una alerta propia")
async def delete_alert(alert_id: int, payload: dict = Depends(require_jwt)):
    # 404 tanto si no existe como si es de otro usuario: no se revela cuál.
    if not await repo.delete(alert_id, payload["sub"]):
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "ok"}


@router.post("/internal/alerts-tick", summary="Evalúa alertas de precio y envía push (cron)")
async def alerts_tick(request: Request, x_alerts_tick_token: str = Header(default="")):
    # compare_digest sobre bytes: sobre str lanza TypeError con un no-ASCII y
    # eso es un 500 alcanzable sin credenciales (SEC-04/05).
    if not ALERTS_TICK_TOKEN or not secrets.compare_digest(
        x_alerts_tick_token.encode(), ALERTS_TICK_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Token inválido")
    return await service.evaluate_alerts(request.app.state.http_client)
