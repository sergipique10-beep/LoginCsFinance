"""Rutas de /market y de los ticks del mercado (CLEAN-11): auth, rate limit, llamar al
service (`steam/services/`) y traducir sus errores a HTTP. La lógica vive en los services.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from settings import CAP_TICK_TOKEN, PRICE_TICK_TOKEN
from auth.service import market_rate_limit, require_jwt, token_matches
from ..domain.catalog import VALID_MARKETS
from ..errors import (
    UPSTREAM_QUOTA_DETAIL, QuotaExhausted, SourceTimeout, SourceUnavailable, UnexpectedPayload,
    UpstreamError,
)
from ..services import fx as fx_service
from ..services import market as market_service
from ..services import providers as providers_service
from steam.price_capture import capture as price_capture_run

router = APIRouter()


def _require_cap_token(x_cap_token: str | None) -> None:
    if not token_matches(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")


def _search_error(exc: Exception) -> HTTPException:
    """Errores de la búsqueda en /items (/market/items y /market/price). Un timeout
    da 502, no 504, como siempre en estas dos rutas."""
    if isinstance(exc, QuotaExhausted):   # SEC-16: sin caché que servir
        return HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL)
    if isinstance(exc, (SourceTimeout, SourceUnavailable)):
        return HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")
    if isinstance(exc, UpstreamError):
        return HTTPException(status_code=502, detail=f"Steam returned {exc.status}")
    return HTTPException(status_code=502, detail=str(exc))   # UnexpectedPayload


@router.get("/market/movers", dependencies=[Depends(market_rate_limit)], summary="Top movers del mercado CS2 (hot & cold 24 h)")
async def get_market_movers(request: Request, user: dict = Depends(require_jwt)):
    return await market_service.get_movers()


@router.get("/market/items", dependencies=[Depends(market_rate_limit)], summary="Busca items en el mercado CS2 por nombre")
async def get_market_items(
    request: Request,
    q: str,
    user: dict = Depends(require_jwt),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="q is required")
    try:
        return (await market_service.search_market(request.app.state.http_client, query)).data
    except (UpstreamError, UnexpectedPayload) as exc:
        raise _search_error(exc) from exc


@router.get("/market/price", dependencies=[Depends(market_rate_limit)], summary="Datos completos (con liquidez) de un item CS2 por nombre")
async def get_market_price(
    request: Request,
    name: str,
    user: dict = Depends(require_jwt),
):
    """Item único con el shape completo de _map_item — incluye liquidityScore,
    liquidityBreakdown y el bloque de volumen.

    Existe porque /market/trending y /market/movers sirven snapshots de Supabase
    vía _row_to_item, que NO transporta esos campos. El detail sheet del frontend
    llama aquí al abrirse sobre un item de trending para no depender del snapshot.
    """
    query = name.strip()
    if not query:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        item = (await market_service.get_item_full(request.app.state.http_client, query)).data
    except (UpstreamError, UnexpectedPayload) as exc:
        raise _search_error(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail=f"Item '{query}' not found")
    return item


@router.get("/market/trending", dependencies=[Depends(market_rate_limit)], summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    return await market_service.get_trending()


@router.get("/market/index", dependencies=[Depends(market_rate_limit)], summary="Índice de mercado global CS2")
async def get_market_index(
    request: Request,
    tf: str = "24h",
    user: dict = Depends(require_jwt),
):
    try:
        return (await market_service.get_market_index(request.app.state.http_client, tf)).data
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Market index request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except QuotaExhausted:
        raise HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL) from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
    except UnexpectedPayload as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/internal/cap-tick", summary="Captura un snapshot del índice de precio CS2 (cron interno)")
async def cap_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    _require_cap_token(x_cap_token)
    try:
        return await market_service.capture_cap_snapshot(request.app.state.http_client)
    except (SourceTimeout, SourceUnavailable) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
    except UnexpectedPayload as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/market/cap-history", dependencies=[Depends(market_rate_limit)], summary="Historial del índice de precio CS2 (snapshots horarios)")
async def get_market_cap_history(
    tf: str = "7d",
    user: dict = Depends(require_jwt),
):
    if tf not in market_service._CAP_TF_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tf '{tf}'. Valid values: {', '.join(market_service._CAP_TF_MAP)}",
        )
    return await market_service.get_cap_history(tf)


@router.post("/internal/trending-tick", summary="Captura el ranking trending del mercado CS2 (cron interno)")
async def trending_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    _require_cap_token(x_cap_token)
    return await market_service.capture_trending(request.app.state.http_client)


@router.post("/internal/enrich-tick", summary="Enriquece deltas del trending con histórico csfloat (cron interno)")
async def enrich_tick(request: Request, x_cap_token: str | None = Header(default=None)):
    _require_cap_token(x_cap_token)
    return await market_service.enrich_trending(request.app.state.http_client)


@router.post("/internal/movers-tick", summary="Captura el ranking hot/cold del mercado CS2 (cron interno)")
async def movers_tick(request: Request, x_cap_token: str | None = Header(default=None)):
    _require_cap_token(x_cap_token)
    return await market_service.capture_movers(request.app.state.http_client)


@router.post("/internal/price-tick", summary="Captura diaria de precios por-skin (cron)")
async def price_tick(
    request: Request,
    x_price_tick_token: str = Header(default=""),
):
    if not token_matches(x_price_tick_token, PRICE_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Token inválido")
    return await price_capture_run(request.app.state.http_client)


@router.get("/market/fx", dependencies=[Depends(market_rate_limit)], summary="Tipo de cambio USD→EUR (BCE, cacheado 24 h)")
async def get_fx_rate(request: Request, user: dict = Depends(require_jwt)):
    """Sirve el USD/EUR. El backend NO convierte precios: la conversion es
    presentacion y vive en el cliente (UX-08). `stale=true` significa que la fuente
    no respondio y se esta reutilizando el ultimo valor conocido."""
    fx = await fx_service.fetch_fx_rate(request.app.state.http_client)
    if fx.data is None:
        # Sin tasa el cliente se queda en USD; no es un error del servidor.
        return {"base": "USD", "rates": {}, "stale": True}
    return {"base": "USD", "rates": {"EUR": fx.data}, "stale": fx.status != "ok"}


@router.get("/market/providers", dependencies=[Depends(market_rate_limit)], summary="Lista de markets soportados como price providers")
async def get_market_providers(request: Request, user: dict = Depends(require_jwt)):
    return (await providers_service.fetch_market_providers(request.app.state.http_client)).data


@router.get("/market/prices", dependencies=[Depends(market_rate_limit)], summary="Precios en tiempo real de un item por mercado")
async def get_market_prices(
    request: Request,
    market: str,
    name: str | None = None,
    currency: str | None = None,
    user: dict = Depends(require_jwt),
):
    market = market.lower().strip()
    if market not in VALID_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market '{market}'. Valid: {', '.join(sorted(VALID_MARKETS))}",
        )
    try:
        return (await market_service.get_market_prices(
            request.app.state.http_client, market, name, currency)).data
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Market prices request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except QuotaExhausted:
        raise HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL) from None
    except UpstreamError as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail=f"Market '{market}' not found or no prices available") from exc
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
