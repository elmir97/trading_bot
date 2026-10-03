"""Репозиторий сделок.

Все выборки идут с обязательным user_id и с LIMIT. Загружать всю историю
в память ради показа последних десяти сделок недопустимо: на нескольких
тысячах записей это заметно, на десятках тысяч — неприемлемо.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.trade import Trade, TradeFill
from app.database.models.user import User
from app.trading.enums import ExchangeKeyMode, TradeSource, TradeStatus


@dataclass(frozen=True, slots=True)
class PeriodPnl:
    """Итог периода по способу «процент от баланса на входе каждой сделки».

    percent — сумма процентов со знаком (убыток отрицательный), None — нет
    ни одной сделки с балансом на входе; uncounted — закрытых сделок без
    баланса на входе, в percent не вошли."""

    percent: Decimal | None
    counted: int
    uncounted: int


def _same_account(account_mode: ExchangeKeyMode | None) -> ColumnElement[bool]:
    if account_mode is None:
        return Trade.account_mode.is_(None)
    return Trade.account_mode == account_mode


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Чтение ------------------------------------------------------------

    async def get(self, trade_id: int, user_id: int) -> Trade | None:
        stmt = (
            select(Trade)
            .where(Trade.id == trade_id, Trade.user_id == user_id)
            .options(
                selectinload(Trade.fills),
                selectinload(Trade.strategy),
                selectinload(Trade.mistakes),
            )
        )
        return await self.session.scalar(stmt)

    async def get_by_notification_id(self, notification_id: int) -> Trade | None:
        """Шаг 15.5.4: сделка, записанная по уведомлению (не больше одной —
        uq_trades_notification_id)."""
        trade: Trade | None = await self.session.scalar(
            select(Trade).where(Trade.notification_id == notification_id)
        )
        return trade

    async def list_open(self, user_id: int, limit: int = 50) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.user_id == user_id, Trade.status == TradeStatus.OPEN)
            .options(selectinload(Trade.strategy))
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def list_recent(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.user_id == user_id)
            .options(selectinload(Trade.strategy))
            .order_by(Trade.opened_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await self.session.scalars(stmt))

    async def list_all_open(self) -> list[Trade]:
        """Все открытые позиции всех пользователей — для фонового монитора.

        В отличие от list_open, без фильтра по user_id: монитор TP/SL
        (этап 12) обходит биржу сразу для всех, а не по одному пользователю,
        чтобы не дублировать запросы цен на каждого. Настройки пользователя
        грузятся тем же запросом — монитор проверяет notification_enabled
        на каждой сделке, без них это был бы N+1.
        """
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.OPEN)
            .options(selectinload(Trade.user).selectinload(User.settings))
        )
        return list(await self.session.scalars(stmt))

    async def list_closed_between(
        self, user_id: int, start: datetime, end: datetime
    ) -> list[Trade]:
        """Закрытые сделки за период — основа всех отчётов.

        Фильтр по closed_at, а не opened_at: сделка, открытая вчера и
        закрытая сегодня, относится к сегодняшнему результату.
        """
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.closed_at >= start,
                Trade.closed_at < end,
            )
            .options(selectinload(Trade.strategy))
            .order_by(Trade.closed_at)
        )
        return list(await self.session.scalars(stmt))

    async def list_unannotated(self, user_id: int, limit: int = 20) -> list[Trade]:
        """Импортированные сделки, ждущие разметки."""
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.is_annotated.is_(False),
                Trade.status == TradeStatus.CLOSED,
            )
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def count_opened_between(
        self, user_id: int, start: datetime, end: datetime,
        *, account_mode: ExchangeKeyMode | None,
    ) -> int:
        """Для проверки лимита сделок в день — только сделки того же счёта
        (NULL — ручной журнал)."""
        stmt = (
            select(func.count())
            .select_from(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.opened_at >= start,
                Trade.opened_at < end,
                Trade.status != TradeStatus.CANCELLED,
                _same_account(account_mode),
            )
        )
        return await self.session.scalar(stmt) or 0

    async def pnl_percent_between(
        self, user_id: int, start: datetime, end: datetime,
        *, account_mode: ExchangeKeyMode | None,
    ) -> PeriodPnl:
        """PnL закрытых за период сделок одного счёта в процентах (03.10.2026).

        Процент каждой сделки — от её собственного баланса на входе
        (account_balance_at_entry), проценты складываются. Раньше сумма PnL
        в деньгах делилась на один баланс, введённый сейчас: демо-убыток
        2245 VST счёта ~85 000 против введённых 1000 давал «−224.54%».
        Сделка без баланса на входе в процент не входит — считается в
        uncounted, вызывающий показывает её отдельной строкой.
        """
        has_balance = Trade.account_balance_at_entry > 0
        stmt = select(
            func.sum(Trade.pnl / Trade.account_balance_at_entry * 100).filter(has_balance),
            func.count().filter(has_balance),
            func.count().filter(~has_balance | Trade.account_balance_at_entry.is_(None)),
        ).where(
            Trade.user_id == user_id,
            Trade.status == TradeStatus.CLOSED,
            Trade.closed_at >= start,
            Trade.closed_at < end,
            Trade.pnl.is_not(None),
            _same_account(account_mode),
        )
        total, counted, uncounted = (await self.session.execute(stmt)).one()
        return PeriodPnl(
            percent=Decimal(total) if counted else None,
            counted=int(counted),
            uncounted=int(uncounted),
        )

    async def find_by_external_position(
        self, user_id: int, exchange: str, external_position_id: str
    ) -> Trade | None:
        """Для дедупликации при импорте."""
        stmt = (
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.exchange == exchange,
                Trade.external_position_id == external_position_id,
            )
            .options(selectinload(Trade.fills))
        )
        return await self.session.scalar(stmt)

    async def existing_fill_ids(
        self, user_id: int, exchange: str, external_ids: list[str]
    ) -> set[str]:
        """Какие из переданных fill уже импортированы.

        Один запрос вместо проверки по одному: импорт за год может
        принести тысячи исполнений.
        """
        if not external_ids:
            return set()
        stmt = select(TradeFill.external_fill_id).where(
            TradeFill.user_id == user_id,
            TradeFill.exchange == exchange,
            TradeFill.external_fill_id.in_(external_ids),
        )
        return {row for row in await self.session.scalars(stmt) if row}

    # --- Запись ------------------------------------------------------------

    def add(self, trade: Trade) -> Trade:
        self.session.add(trade)
        return trade

    def add_fill(self, fill: TradeFill) -> TradeFill:
        self.session.add(fill)
        return fill

    async def list_open_for_reconcile(self, user_id: int) -> list[Trade]:
        """Шаг 15.6: все открытые сделки пользователя с исполнениями —
        reconciler сверяет с биржей сделки бота, а открытые сделки любого
        источника нужны, чтобы не назвать чужую позицию «без сделки»."""
        stmt = (
            select(Trade)
            .where(Trade.user_id == user_id, Trade.status == TradeStatus.OPEN)
            .options(selectinload(Trade.fills))
            .execution_options(populate_existing=True)
        )
        return list(await self.session.scalars(stmt))

    async def lock_for_reconcile(self, trade_id: int) -> Trade | None:
        """Строка сделки под FOR UPDATE — запись выхода reconciler'ом не
        пересекается с ручным закрытием и параллельной сверкой."""
        stmt = (
            select(Trade)
            .where(Trade.id == trade_id)
            .with_for_update()
            .options(selectinload(Trade.fills))
            .execution_options(populate_existing=True)
        )
        trade: Trade | None = await self.session.scalar(stmt)
        return trade

    async def bot_fill_external_ids(self, user_id: int) -> set[str]:
        """external_fill_id исполнений сделок бота (SIGNAL_EXECUTION): вход
        и выходы, записанные ботом и reconciler'ом. Импорт истории их не
        заводит второй раз — ручное закрытие позиции бота на бирже
        reconciler уже записал выходом этой сделки."""
        stmt = (
            select(TradeFill.external_fill_id)
            .join(Trade, Trade.id == TradeFill.trade_id)
            .where(
                Trade.user_id == user_id,
                Trade.source == TradeSource.SIGNAL_EXECUTION,
                TradeFill.external_fill_id.is_not(None),
            )
        )
        return {value for value in await self.session.scalars(stmt) if value}

    async def flush(self) -> None:
        await self.session.flush()

    async def delete(self, trade: Trade) -> None:
        await self.session.delete(trade)
