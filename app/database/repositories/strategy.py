"""Репозитории стратегий и типов ошибок."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.mistake import MistakeType
from app.database.models.strategy import Strategy


class StrategyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self, user_id: int) -> list[Strategy]:
        stmt = (
            select(Strategy)
            .where(Strategy.user_id == user_id, Strategy.is_active.is_(True))
            .order_by(Strategy.name)
        )
        return list(await self.session.scalars(stmt))

    async def get_by_name(self, user_id: int, name: str) -> Strategy | None:
        return await self.session.scalar(
            select(Strategy).where(
                Strategy.user_id == user_id, Strategy.name == name
            )
        )

    def create(
        self, user_id: int, name: str, description: str | None, rules: str | None
    ) -> Strategy:
        strategy = Strategy(
            user_id=user_id, name=name, description=description, rules=rules
        )
        self.session.add(strategy)
        return strategy


class MistakeTypeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_available(self, user_id: int) -> list[MistakeType]:
        """Системные типы плюс собственные типы пользователя."""
        stmt = (
            select(MistakeType)
            .where(
                or_(MistakeType.user_id == user_id, MistakeType.is_system.is_(True)),
                MistakeType.is_active.is_(True),
            )
            .order_by(MistakeType.is_system.desc(), MistakeType.title)
        )
        return list(await self.session.scalars(stmt))

    async def get_by_code(self, code: str, user_id: int) -> MistakeType | None:
        stmt = select(MistakeType).where(
            MistakeType.code == code,
            or_(MistakeType.user_id == user_id, MistakeType.is_system.is_(True)),
        )
        return await self.session.scalar(stmt)

    async def count_system(self) -> int:
        from sqlalchemy import func

        return await self.session.scalar(  # type: ignore[return-value]
            select(func.count()).select_from(MistakeType).where(
                MistakeType.is_system.is_(True)
            )
        )

    def create_system(self, code: str, title: str, description: str) -> MistakeType:
        mistake = MistakeType(
            user_id=None,
            code=code,
            title=title,
            description=description,
            is_system=True,
        )
        self.session.add(mistake)
        return mistake
