from fastapi import APIRouter, Request

from auth.service import _get_client_ip, _rate_limit
from ..errors.handling import SOURCE_ERRORS, http_error_for
from ..services import news as news_service

router = APIRouter()


@router.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
async def get_cs2_news(request: Request, count: int = 5):
    _rate_limit(_get_client_ip(request), bucket="news")  # SEC-16: fuera del cupo de auth
    try:
        return (await news_service.get_cs2_news(request.app.state.http_client, count)).data
    except SOURCE_ERRORS as exc:
        raise http_error_for(exc, timeout_status=504) from exc
