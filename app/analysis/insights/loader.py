"""Адаптер: ORM-сделки → чистые TradeFact.

Единственное место, где домен анализа соприкасается с базой. Если поля
модели Trade называются иначе — правится только этот файл.

ВАЖНО: mistakes и strategy загружаются eager'ом. Ленивая коллекция или
relationship в async-сессии уронит анализ ровно так же, как ронял
сохранение сделки на этапе 4.

Trade не хранит risk_amount/realized_r готовыми колонками — только
risk_percent, risk_reward, entry_price/stop_loss/exit_price. Это
плановый R:R (risk_reward) и денежный риск/фактический R считаются здесь
по тем же формулам, что и при открытии/закрытии сделки
(app.trading.calculations).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analysis.insights.facts import TradeFact
from app.database.models.trade import Trade
from app.trading.calculations import CalculationError, calculate_realized_rr
from app.trading.enums import TradeStatus


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _risk_amount(trade: Trade) -> Decimal | None:
    """Денежный риск на сделку: баланс на входе × риск % от плана."""
    balance = trade.account_balance_at_entry
    risk_percent = trade.risk_percent
    if balance is None or risk_percent is None:
        return None
    return balance * risk_percent / Decimal(100)


def _realized_r(trade: Trade) -> Decimal | None:
    """Фактический результат в единицах риска — по цене входа/стопа/выхода.

    Требует entry_price, stop_loss и exit_price: без одного из них R не
    посчитать, а не «взять на глаз».
    """
    if trade.entry_price is None or trade.stop_loss is None or trade.exit_price is None:
        return None
    try:
        return calculate_realized_rr(
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
            exit_price=trade.exit_price,
            side=trade.side,
        )
    except CalculationError:
        # Стоп с неправильной стороны от входа — данные испорчены,
        # а не повод уронить весь разбор журнала.
        return None


def to_fact(trade: Trade) -> TradeFact:
    """Переводит ORM-сделку в плоский факт."""
    return TradeFact(
        id=trade.id,
        symbol=trade.symbol,
        side=trade.side.value if hasattr(trade.side, "value") else str(trade.side),
        strategy=trade.strategy.name if trade.strategy is not None else None,
        timeframe=trade.timeframe,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        net_pnl=_as_decimal(trade.pnl) or Decimal("0"),
        risk_amount=_risk_amount(trade),
        planned_rr=_as_decimal(trade.risk_reward),
        realized_r=_realized_r(trade),
        mistakes=tuple(
            m.mistake_type.code.value
            if hasattr(m.mistake_type.code, "value")
            else str(m.mistake_type.code)
            for m in (trade.mistakes or ())
        ),
    )


async def load_facts(
    session: AsyncSession,
    *,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
) -> list[TradeFact]:
    """Грузит закрытые сделки пользователя за период."""
    stmt = (
        select(Trade)
        .where(
            Trade.user_id == user_id,
            Trade.status == TradeStatus.CLOSED,
            Trade.closed_at >= period_start,
            Trade.closed_at <= period_end,
        )
        .options(selectinload(Trade.mistakes), selectinload(Trade.strategy))
        .order_by(Trade.closed_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [to_fact(row) for row in rows]
