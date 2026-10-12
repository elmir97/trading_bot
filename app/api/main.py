"""Процесс API Mini App: uvicorn --factory app.api.main:create_app.

Импорт модуля ничего не создаёт и не читает окружение: настройки, БД, Redis и
клиенты биржи — в lifespan. Планировщика, поллинга, reconciler и
восстановления открытий здесь нет (это процесс бота, docs/miniapp-plan.md §2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.deps import ApiError, ApiState
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging
from app.core.security import SecretCipher
from app.database.session import Database
from app.services.exchange_factory import ExchangeFactory

logger = get_logger(__name__)


def build_state(settings: Settings) -> ApiState:
    return ApiState(
        settings=settings,
        db=Database(settings),
        redis=Redis.from_url(settings.redis_url, decode_responses=True),
        factory=ExchangeFactory(
            settings, SecretCipher(settings.encryption_key.get_secret_value())
        ),
    )


def create_app(state: ApiState | None = None) -> FastAPI:
    """state — готовое состояние (тесты); иначе собирается из get_settings()
    при старте и закрывается при остановке."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if state is not None:
            yield
            return
        settings = get_settings()
        # Свой файл: чек-ап читает logs/bot.log, строки API туда не мешаются.
        setup_logging(level=settings.log_level, json_output=settings.log_json,
                      secrets=settings.secret_values, log_file="api.log")
        built = build_state(settings)
        app.state.api = built
        logger.info("API Mini App запущен", extra={
            "account_mode": settings.bingx_allowed_exchange_mode.value,
            "allowed_ids": len(settings.miniapp_allowed_ids),
        })
        try:
            yield
        finally:
            await built.redis.aclose()
            await built.db.dispose()

    app = FastAPI(
        title="trading_bot Mini App API", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.include_router(router)
    if state is not None:
        # Готовое состояние — сразу (httpx.ASGITransport lifespan не запускает).
        app.state.api = state

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Тело запроса в ответ не возвращаем (и в лог не пишем).
        return JSONResponse(
            {"code": "BAD_REQUEST", "message": "Неверный запрос — обнови приложение."},
            status_code=422,
        )

    return app


__all__ = ["build_state", "create_app"]
