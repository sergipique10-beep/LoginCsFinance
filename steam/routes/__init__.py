from fastapi import APIRouter

from .items import router as items_router
from .market import router as market_router
from .news import router as news_router

router = APIRouter()
router.include_router(items_router)
router.include_router(market_router)
router.include_router(news_router)
