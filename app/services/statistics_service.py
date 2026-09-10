"""Сервис статистики: мост между базой и чистыми расчётами.

Из базы тянутся только лёгкие проекции (PnL, дата, символ, стратегия), а
не сделки целиком с исполнениями. Загружать полные объекты ради подсчёта
win rate — прямой путь к тому, что отчёт за год перестанет открываться.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.mistake import MistakeType, TradeMistake
from app.database.models.strategy import Strategy
from app.database.models.trade import Trade
from app.trading.enums import TradeStatus
from app.trading.statistics import TradeSnapshot

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class MistakeImpact:
    """Финансовое влияние одной ошибки.

    Показывать количество без денег бесполезно: пользователю нужно
    понимать, какая привычка стоит дороже всего, а не какая чаще.
    """

    code: str
    title: str
    occurrences: int
    total_pnl: Decimal
    average_pnl: Decimal
    win_rate: Decimal
    wins: int


class StatisticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base_query(self, user_id: int) -> Select:
        """Только закрытые сделки: у открытых нет финального результата."""
        return (
            select(
                Trade.pnl,
                Trade.closed_at,
                Trade.symbol,
                Trade.side,
                Strategy.name.label("strategy"),
                Trade.timeframe,
                Trade.risk_reward,
                Trade.account_balance_at_entry,
            )
            .outerjoin(Strategy, Trade.strategy_id == Strategy.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.pnl.is_not(None),
            )
        )

    async def load_snapshots(
        self,
        user_id: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        annotated_only: bool = False,
    ) -> list[TradeSnapshot]:
        """Снимки закрытых сделок за период.

        annotated_only нужен для срезов по стратегиям и ошибкам:
        импортированные, но не размеченные сделки исказили бы их.
        """
        query = self._base_query(user_id)
        if start is not None:
            query = query.where(Trade.closed_at >= start)
        if end is not None:
            query = query.where(Trade.closed_at < end)
        if annotated_only:
            query = query.where(Trade.is_annotated.is_(True))

        rows = await self._session.execute(query.order_by(Trade.closed_at))
        return [
            TradeSnapshot(
                pnl=row.pnl,
                closed_at=row.closed_at,
                symbol=row.symbol,
                side=row.side.value if hasattr(row.side, "value") else str(row.side),
                strategy=row.strategy,
                timeframe=row.timeframe,
                risk_reward=row.risk_reward,
                account_balance=row.account_balance_at_entry,
            )
            for row in rows
        ]

    async def mistake_impact(
        self, user_id: int, *, min_occurrences: int = 1
    ) -> list[MistakeImpact]:
        """Влияние каждой ошибки на результат, агрегацией на стороне БД.

        Сортировка по сумме PnL по возрастанию: самая дорогая ошибка —
        первой, потому что именно она требует внимания.
        """
        query = (
            select(
                MistakeType.code,
                MistakeType.title,
                func.count(Trade.id).label("occurrences"),
                func.coalesce(func.sum(Trade.pnl), ZERO).label("total_pnl"),
                func.coalesce(func.avg(Trade.pnl), ZERO).label("average_pnl"),
                func.count(Trade.id).filter(Trade.pnl > ZERO).label("wins"),
            )
            .select_from(TradeMistake)
            .join(Trade, TradeMistake.trade_id == Trade.id)
            .join(MistakeType, TradeMistake.mistake_type_id == MistakeType.id)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.pnl.is_not(None),
            )
            .group_by(MistakeType.code, MistakeType.title)
            .having(func.count(Trade.id) >= min_occurrences)
            .order_by(func.sum(Trade.pnl))
        )

        rows = await self._session.execute(query)
        result = []
        for row in rows:
            win_rate = (
                Decimal(row.wins) / Decimal(row.occurrences) * Decimal(100)
                if row.occurrences
                else ZERO
            )
            result.append(
                MistakeImpact(
                    code=row.code,
                    title=row.title,
                    occurrences=row.occurrences,
                    total_pnl=Decimal(row.total_pnl).quantize(Decimal("0.01")),
                    average_pnl=Decimal(row.average_pnl).quantize(Decimal("0.01")),
                    win_rate=win_rate.quantize(Decimal("0.01")),
                    wins=row.wins,
                )
            )
        return result

    async def overall_average_pnl(self, user_id: int) -> Decimal | None:
        """Средний результат по всем сделкам — база для сравнения с ошибками.

        Утверждение «сделки с FOMO хуже обычных» требует этой величины;
        без неё цифры по ошибке не с чем сопоставить.
        """
        value = await self._session.scalar(
            select(func.avg(Trade.pnl)).where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.pnl.is_not(None),
            )
        )
        return Decimal(value).quantize(Decimal("0.01")) if value is not None else None

    async def starting_equity(self, user_id: int) -> Decimal:
        """Баланс на входе в самую раннюю закрытую сделку.

        Без него просадка считается от нулевой эквити, и убыток 200 USDT
        на депозите 10 000 показывается как 77% — цифра, по которой
        невозможно принять решение. С известным депозитом это 1.95%.

        Берётся именно самая ранняя сделка: её баланс ближе всего к
        стартовому капиталу рассматриваемой серии.
        """
        value = await self._session.scalar(
            select(Trade.account_balance_at_entry)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.account_balance_at_entry.is_not(None),
            )
            .order_by(Trade.opened_at)
            .limit(1)
        )
        return Decimal(value) if value is not None else ZERO

    async def count_unannotated(self, user_id: int) -> int:
        return await self._session.scalar(  # type: ignore[return-value]
            select(func.count())
            .select_from(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.CLOSED,
                Trade.is_annotated.is_(False),
            )
        ) or 0


def period_bounds(
    period: str, now: datetime | None = None, tz_offset_hours: int = 5
) -> tuple[datetime, datetime]:
    """Границы периода отчёта в UTC с учётом локального дня пользователя."""
    moment = now or datetime.now(UTC)
    local = moment + timedelta(hours=tz_offset_hours)
    local_day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_day_start - timedelta(hours=tz_offset_hours)

    if period == "today":
        return day_start, day_start + timedelta(days=1)
    if period == "week":
        start = day_start - timedelta(days=local.weekday())
        return start, start + timedelta(days=7)
    if period == "month":
        local_month_start = local_day_start.replace(day=1)
        start = local_month_start - timedelta(hours=tz_offset_hours)
        return start, moment + timedelta(days=1)
    # all
    return datetime(2000, 1, 1, tzinfo=UTC), moment + timedelta(days=1)
