"""Регистрация пользователя и наполнение стартовыми данными.

Отдельный слой services появился по необходимости: операция затрагивает
пять таблиц и обязана быть атомарной. Класть её в репозиторий нельзя
(репозиторий отвечает за одну сущность), в хендлер — тем более.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.mistake import SYSTEM_MISTAKES
from app.database.models.strategy import DEFAULT_STRATEGIES
from app.database.models.trading_plan import (
    DEFAULT_ALLOWED_SYMBOLS,
    DEFAULT_ALLOWED_TIMEFRAMES,
    TradingPlan,
)
from app.database.models.user import DEFAULT_NOTIFICATIONS, User, UserSettings
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.user import UserRepository

logger = get_logger(__name__)


class UserService:
    def __init__(
        self,
        users: UserRepository,
        strategies: StrategyRepository,
        mistakes: MistakeTypeRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._strategies = strategies
        self._mistakes = mistakes
        self._settings = settings

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        """Идемпотентно. Вызывается на каждом апдейте через middleware."""
        user = await self._users.get_by_telegram_id(telegram_id)
        if user is not None:
            return user

        user = self._users.create(telegram_id, username, first_name)
        await self._users.session.flush()  # нужен user.id для связанных записей


        self._users.add_settings(
            UserSettings(
                user_id=user.id,
                timezone=self._settings.default_timezone,
                notifications=dict(DEFAULT_NOTIFICATIONS),
            )
        )
        self._users.add_trading_plan(
            TradingPlan(
                user_id=user.id,
                risk_per_trade_percent=self._settings.default_risk_per_trade_percent,
                max_daily_loss_percent=self._settings.default_max_daily_loss_percent,
                max_weekly_loss_percent=self._settings.default_max_weekly_loss_percent,
                max_trades_per_day=self._settings.default_max_trades_per_day,
                min_risk_reward=self._settings.min_risk_reward,
                allowed_symbols=list(DEFAULT_ALLOWED_SYMBOLS),
                allowed_timeframes=list(DEFAULT_ALLOWED_TIMEFRAMES),
            )
        )

        for item in DEFAULT_STRATEGIES:
            self._strategies.create(
                user_id=user.id,
                name=item["name"],
                description=item["description"],
                rules=item["rules"],
            )

        await self._users.session.flush()
        logger.info("Создан пользователь", extra={"telegram_id": telegram_id})
        return user

    async def ensure_system_mistakes(self) -> int:
        """Наполняет общий справочник ошибок. Идемпотентно, вызов при старте.

        Справочник общий для всех пользователей, поэтому не привязан к
        регистрации — иначе первый пользователь создавал бы его, а при
        добавлении нового типа пришлось бы писать миграцию с данными.
        """
        existing = await self._mistakes.count_system()
        if existing >= len(SYSTEM_MISTAKES):
            return 0

        # Идемпотентность важна: метод вызывается и при старте, и при
        # регистрации каждого пользователя.

        created = 0
        for item in SYSTEM_MISTAKES:
            if await self._mistakes.get_by_code(item["code"], user_id=0) is None:
                self._mistakes.create_system(
                    code=item["code"],
                    title=item["title"],
                    description=item["description"],
                )
                created += 1
        await self._mistakes.session.flush()
        if created:
            logger.info("Добавлено системных типов ошибок", extra={"count": created})
        return created
