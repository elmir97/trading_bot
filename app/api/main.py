"""Процесс API Mini App: uvicorn --factory app.api.main:create_app.

Импорт модуля ничего не создаёт и не читает окружение: настройки, БД, Redis и
клиенты биржи — в lifespan. Планировщика, поллинга, reconciler и
восстановления открытий здесь нет (это процесс бота, docs/miniapp-plan.md §2);
есть только быстрые повторы защиты после тревоги своего «Открыть» (B.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
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
from app.services.exchange_factory import ExchangeFactory, SharedHttp
from app.workers.openings import OpeningsWorker

logger = get_logger(__name__)


CONFIRM_DRAIN_SECONDS = 30.0


def build_state(settings: Settings) -> tuple[ApiState, OpeningsWorker]:
    """Bot — только отправка (итог открытия в чат, снятие кнопок): поллинга
    нет, второго getUpdates не возникает. Воркер открытий — только быстрые
    повторы защиты после тревоги (B.1); его цикл (run) здесь не запускается."""
    cipher = SecretCipher(settings.encryption_key.get_secret_value())
    db = Database(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    # Один httpx-клиент на хост на весь процесс: и чтения Mini App, и «Открыть».
    factory = ExchangeFactory(settings, cipher,
                              http=SharedHttp(timeout=settings.http_timeout_seconds))
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None,
    )
    worker = OpeningsWorker(bot, db, settings, cipher, redis, factory=factory)
    state = ApiState(settings=settings, db=db, redis=redis, factory=factory, bot=bot,
                     alarm_retry=worker.retry_alarm)
    return state, worker


async def drain(state: ApiState, timeout: float = CONFIRM_DRAIN_SECONDS) -> None:
    """Остановка: дать начатым «Открыть» дойти до итога. Не успели — их
    довосстановит цикл бота (состояние — в trade_openings)."""
    tasks = [t for t in state.confirm_tasks.values() if not t.done()]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning("Остановка API: «Открыть» не завершилось — доведёт цикл бота",
                       extra={"opening_ids": sorted(state.confirm_tasks)})


async def read_bot_username(bot: Bot) -> str | None:
    """Имя бота для ссылки «⚙️ Действия в чате». Telegram не ответил — API всё
    равно поднимается: кнопки в Mini App нет, вместо неё подсказка."""
    try:
        me = await bot.get_me()
    except Exception as exc:
        logger.warning("API Mini App: getMe не прочитан — ссылки в чат не будет",
                       extra={"error": type(exc).__name__})
        return None
    return me.username


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
        built, worker = build_state(settings)
        built.bot_username = await read_bot_username(built.bot)
        app.state.api = built
        logger.info("API Mini App запущен", extra={
            "account_mode": settings.bingx_allowed_exchange_mode.value,
            "allowed_ids": len(settings.miniapp_allowed_ids),
        })
        try:
            yield
        finally:
            await drain(built)
            await worker.close()   # повторы защиты — дальше цикл бота
            await built.bot.session.close()
            await built.factory.aclose()
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


__all__ = ["build_state", "create_app", "drain", "read_bot_username"]
