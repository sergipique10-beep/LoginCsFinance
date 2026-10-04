from fastapi import APIRouter, HTTPException, Request

from auth.service import _get_client_ip, _rate_limit
from ..errors import SourceTimeout, SourceUnavailable, UpstreamError
from ..services import news as news_service

router = APIRouter()


@router.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
async def get_cs2_news(request: Request, count: int = 5):
    _rate_limit(_get_client_ip(request), bucket="news")  # SEC-16: fuera del cupo de auth
    try:
        return await news_service.get_cs2_news(request.app.state.http_client, count)
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Steam news request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
