"""Сделка в журнале из факта исполнения (шаг 15.5.4).

Журнал — не только история: гварды POSITION_EXISTS / MAX_POSITIONS /
MAX_TOTAL_RISK считают открытые позиции по trades. Позиция на бирже без
строки в журнале — гварды слепы, возможен второй вход по тому же символу.
Поэтому сделка пишется после read-back при ЛЮБОМ исходе входа, кроме
отказа (REJECTED/REFUSED/LEVERAGE_FAILED) и сухого прогона:

- исполнение прочитано (FILLED) — реальные цифры: avgPrice, executedQty,
  комиссия; fill_confirmed = true;
- не прочитано (ReadbackIncomplete, SUBMITTED без FILLED, UNKNOWN) —
  предварительная сделка: плановая цена и объём, fill_confirmed = false,
  пометка в notes. Ложная блокировка символа до сверки (reconciler 15.6)
  безопаснее второй позиции.

Стоп и тейк в сделке — те, что РЕАЛЬНО стоят на бирже (найденные или
спасённые read-back); иначе плановый уровень и пометка «не подтверждён».

Идемпотентность: одна сделка на одно уведомление — execution_orders.
trade_id у ENTRY (приложение) и uq_trades_notification_id (БД).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal_notification import SignalNotification
from app.database.models.trade import Trade
from app.database.repositories.strategy import StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.execution.models import OrderRequest
from app.execution.readback import ConditionalOutcome, ConditionalState, ReadbackResult
from app.trading.enums import OrderStatus, TradeSide, TradeSource
from app.trading.journal import TradeJournal

logger = get_logger(__name__)

# Исходы входа, при которых позиция могла открыться — сделка пишется.
_RECORDABLE = (OrderStatus.FILLED, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN)
_ON_EXCHANGE = (ConditionalOutcome.FOUND, ConditionalOutcome.RESCUED)

NOTE_FILL_UNCONFIRMED = "исполнение не подтверждено — цена и объём плановые"
NOTE_STOP_UNCONFIRMED = "стоп на бирже не подтверждён"
NOTE_TAKE_UNCONFIRMED = "тейк на бирже не подтверждён"
NOTE_ENTRY_PAST_STOP = "фактическая цена входа за уровнем стопа"


@dataclass(frozen=True, slots=True)
class JournalOutcome:
    trade: Trade
    # Предварительная запись (fill_confirmed = false).
    provisional: bool
    # Вход UNKNOWN и поиск ордер не нашёл — позиции может не быть вовсе.
    entry_not_found: bool
    # «Вход исполнен за уровнем стопа» — тревога пользователю.
    alarm: str | None = None


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _level(state: ConditionalState | None, planned: Decimal) -> tuple[Decimal, bool]:
    """(уровень для журнала, стоит ли он на бирже)."""
    if state is not None and state.outcome in _ON_EXCHANGE and state.trigger_price is not None:
        return state.trigger_price, True
    return planned, False


def _entry_past_stop(side: TradeSide, entry_price: Decimal, stop_loss: Decimal) -> bool:
    return entry_price <= stop_loss if side is TradeSide.LONG else entry_price >= stop_loss


async def record_entry_trade(
    *,
    session: AsyncSession,
    entry_row: ExecutionOrder,
    order: OrderRequest,
    notification: SignalNotification,
    timeframe: str | None,
    readback: ReadbackResult,
    account_balance: Decimal | None,
    now: datetime,
) -> JournalOutcome | None:
    """См. докстринг модуля. None — сделка не пишется (отказ, сухой прогон)."""
    trades = TradeRepository(session)

    if entry_row.trade_id is not None:
        existing = await trades.get(entry_row.trade_id, order.user_id)
        if existing is not None:
            return _outcome(existing, entry_row)
    if entry_row.status not in _RECORDABLE:
        return None

    fill = readback.fill
    confirmed = fill is not None and entry_row.status is OrderStatus.FILLED
    if confirmed:
        assert fill is not None
        entry_price, quantity, fee = fill.avg_price, fill.executed_qty, fill.fee
    else:
        entry_price, quantity, fee = order.entry_price, order.quantity, Decimal(0)

    notes: list[str] = []
    if not confirmed:
        notes.append(NOTE_FILL_UNCONFIRMED)
    stop_level, stop_on_exchange = _level(readback.stop, order.stop_loss)
    take_level, take_on_exchange = _level(readback.take, order.take_profit)
    if not stop_on_exchange:
        notes.append(NOTE_STOP_UNCONFIRMED)
    if not take_on_exchange:
        notes.append(NOTE_TAKE_UNCONFIRMED)

    alarm: str | None = None
    journal_stop: Decimal | None = stop_level
    if confirmed and _entry_past_stop(order.position_side, entry_price, stop_level):
        # Проскальзывание дальше стопа: TradeJournal.open_trade отказал бы
        # такой сделке — пишем без стопа, но громко (убыток больше заявленного).
        journal_stop = None
        notes.append(f"{NOTE_ENTRY_PAST_STOP}: стоп {_num(stop_level)}")
        alarm = (
            f"⚠️ Вход исполнен за уровнем стопа: {order.symbol} "
            f"{order.position_side.value}, цена {_num(entry_price)}, стоп "
            f"{_num(stop_level)}. Убыток больше заявленного — проверь позицию в BingX"
        )
        logger.error(
            "Вход исполнен за уровнем стопа",
            extra={
                "symbol": order.symbol,
                "notification_id": order.notification_id,
                "entry_price": str(entry_price),
                "stop_loss": str(stop_level),
            },
        )

    strategy = await StrategyRepository(session).get_by_name(order.user_id, notification.setup)
    journal = TradeJournal(trades)
    try:
        async with session.begin_nested():
            trade = await journal.open_trade(
                user_id=order.user_id,
                symbol=order.symbol,
                side=order.position_side,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=journal_stop,
                take_profit=take_level,
                leverage=order.leverage,
                strategy_id=strategy.id if strategy is not None else None,
                timeframe=timeframe,
                entry_reason=(
                    f"Сигнал «{notification.setup}» · {timeframe or '—'} · "
                    f"уведомление #{notification.id}"
                ),
                notes="; ".join(notes) or None,
                fee=fee,
                account_balance=account_balance,
                opened_at=(fill.filled_at if confirmed and fill is not None else None) or now,
                source=TradeSource.SIGNAL_EXECUTION,
                signal_id=order.signal_id,
                notification_id=order.notification_id,
                fill_confirmed=confirmed,
            )
    except IntegrityError as exc:
        # Параллельная запись (reconciler 15.6 против хендлера) уже создала
        # сделку по этому уведомлению — берём её, дубль не пишем.
        if "uq_trades_notification_id" not in str(exc):
            raise
        found = await trades.get_by_notification_id(order.notification_id)
        assert found is not None, "UNIQUE сработал, а сделки нет"
        entry_row.trade_id = found.id
        await session.commit()
        return _outcome(found, entry_row)

    # Риск и RR — как у ордера (расчёт по плану входа), а не пересчёт
    # open_trade от фактической цены: это то, что пропустили гварды.
    trade.risk_percent = order.risk_percent
    trade.risk_reward = order.risk_reward
    trade.is_annotated = strategy is not None
    entry_row.trade_id = trade.id
    await session.commit()
    logger.info(
        "Сделка бота записана в журнал",
        extra={
            "trade_id": trade.id,
            "notification_id": order.notification_id,
            "fill_confirmed": confirmed,
        },
    )
    return JournalOutcome(
        trade=trade,
        provisional=not confirmed,
        entry_not_found=entry_row.status is OrderStatus.UNKNOWN,
        alarm=alarm,
    )


def _outcome(trade: Trade, entry_row: ExecutionOrder) -> JournalOutcome:
    return JournalOutcome(
        trade=trade,
        provisional=not trade.fill_confirmed,
        entry_not_found=entry_row.status is OrderStatus.UNKNOWN,
    )
