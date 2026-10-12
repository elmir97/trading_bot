"""Состояние процесса API и зависимости запросов: сессия БД, текущий пользователь."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import InitDataError, validate_init_data
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import mask_telegram_id
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.market.cache import TTLCache

logger = get_logger(__name__)

SCHEME = "tma "


class ApiError(Exception):
    """Ответ {code, message} с HTTP-статусом; message — тем же текстом, что в чате."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class ApiState:
    settings: Settings
    db: Database
    redis: Any
    factory: Any                      # ExchangeFactory (в тестах — фейк)
    bot: Any = None                   # aiogram.Bot только для sendMessage (подэтап 1.5)
    market_cache: TTLCache = field(default_factory=TTLCache)


def get_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.api
    return state


State = Annotated[ApiState, Depends(get_state)]


async def get_session(state: State) -> AsyncIterator[AsyncSession]:
    async with state.db.session() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, state: State, session: Session) -> User:
    """initData → telegram_id → список разрешённых → users. Пустой список —
    403 всем (по умолчанию API закрыт)."""
    header = request.headers.get("authorization", "")
    if not header.startswith(SCHEME):
        raise ApiError(401, "UNAUTHORIZED", "Нет доступа — открой приложение из бота.")
    settings = state.settings
    try:
        data = validate_init_data(
            header[len(SCHEME):], settings.bot_token.get_secret_value(),
            ttl_seconds=settings.miniapp_init_data_ttl_seconds,
        )
    except InitDataError as exc:
        logger.warning("Mini App: initData отклонён", extra={"reason": exc.reason})
        raise ApiError(401, "UNAUTHORIZED", exc.message) from None
    masked = mask_telegram_id(data.telegram_id)
    if data.telegram_id not in settings.miniapp_allowed_ids:
        logger.warning("Mini App: пользователь не в списке разрешённых",
                       extra={"telegram_id": masked})
        raise ApiError(403, "FORBIDDEN", "Mini App тебе недоступен.")
    user = await UserRepository(session).get_by_telegram_id(data.telegram_id)
    if user is None:
        logger.warning("Mini App: пользователя нет в базе", extra={"telegram_id": masked})
        raise ApiError(403, "FORBIDDEN", "Сначала открой бота и нажми /start.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


__all__ = ["ApiError", "ApiState", "CurrentUser", "Session", "State", "get_state"]
