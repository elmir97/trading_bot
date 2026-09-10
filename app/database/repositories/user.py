"""Репозиторий пользователя и связанных с ним настроек."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User, UserSettings


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Грузит связанные настройки сразу.

        selectinload вместо ленивой загрузки: в async-режиме SQLAlchemy
        обращение к незагруженному отношению вне сессии падает с
        MissingGreenlet, а не подгружается молча.
        """
        stmt = (
            select(User)
            .where(User.telegram_id == telegram_id)
            .options(
                selectinload(User.settings),
                selectinload(User.trading_plan),
            )
        )
        return await self.session.scalar(stmt)

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def list_active_with_plan(self) -> list[User]:
        """Все активные пользователи с планом и настройками — для фоновых
        задач (этап 12): сканеру и дневным уведомлениям нужны сразу оба,
        а поштучные запросы на каждого означали бы N+1 на каждом тике.
        """
        stmt = (
            select(User)
            .where(User.is_active.is_(True))
            .options(
                selectinload(User.trading_plan),
                selectinload(User.settings),
            )
        )
        return list(await self.session.scalars(stmt))

    def create(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> User:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        self.session.add(user)
        return user

    def add_settings(self, settings: UserSettings) -> UserSettings:
        self.session.add(settings)
        return settings

    def add_trading_plan(self, plan: TradingPlan) -> TradingPlan:
        self.session.add(plan)
        return plan

    async def get_trading_plan(self, user_id: int) -> TradingPlan | None:
        return await self.session.scalar(
            select(TradingPlan).where(TradingPlan.user_id == user_id)
        )

    async def get_settings(self, user_id: int) -> UserSettings | None:
        return await self.session.scalar(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
