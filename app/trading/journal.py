"""Сервис торгового журнала.

Единственное место, где сделка меняет состояние. Все агрегаты Trade
пересчитываются из fills после каждого изменения — поля никогда не
редактируются напрямую, иначе они разойдутся с исполнениями.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.database.models.mistake import TradeMistake
from app.database.models.trade import Trade, TradeFill
from app.database.repositories.trade import TradeRepository
from app.trading.calculations import (
    CalculationError,
    FillData,
    aggregate_fills,
    calculate_pnl,
    calculate_pnl_percent,
    calculate_pnl_percent_of_balance,
    calculate_realized_rr,
    calculate_risk_reward,
    stop_distance,
)
from app.trading.enums import FillSide, TradeSide, TradeSource, TradeStatus

logger = get_logger(__name__)

ZERO = Decimal(0)


class JournalError(RuntimeError):
    """Недопустимая операция над сделкой."""


class TradeJournal:
    def __init__(self, trades: TradeRepository) -> None:
        self._trades = trades

    # --- Открытие ----------------------------------------------------------

    async def open_trade(
        self,
        *,
        user_id: int,
        symbol: str,
        side: TradeSide,
        entry_price: Decimal,
        quantity: Decimal,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        leverage: int = 1,
        strategy_id: int | None = None,
        timeframe: str | None = None,
        entry_reason: str | None = None,
        notes: str | None = None,
        fee: Decimal = ZERO,
        account_balance: Decimal | None = None,
        opened_at: datetime | None = None,
        exchange: str = "bingx",
        source: TradeSource = TradeSource.MANUAL,
        external_position_id: str | None = None,
    ) -> Trade:
        """Создаёт сделку вместе с первым исполнением.

        Валидация стопа выполняется здесь, а не в хендлере: сделка с
        некорректным стопом не должна существовать в базе ни при вводе
        руками, ни при импорте.
        """
        if quantity <= ZERO:
            raise JournalError("Объём должен быть положительным")
        if entry_price <= ZERO:
            raise JournalError("Цена входа должна быть положительной")

        if stop_loss is not None:
            # Бросает CalculationError со стороной ошибки в тексте.
            stop_distance(
                entry_price=entry_price, stop_loss=stop_loss, side=side
            )

        risk_reward: Decimal | None = None
        if stop_loss is not None and take_profit is not None:
            risk_reward = calculate_risk_reward(
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                side=side,
            )

        risk_percent: Decimal | None = None
        if stop_loss is not None and account_balance and account_balance > ZERO:
            risk_amount = (
                stop_distance(
                    entry_price=entry_price, stop_loss=stop_loss, side=side
                )
                * quantity
            )
            risk_percent = (risk_amount / account_balance * Decimal(100)).quantize(
                Decimal("0.0001")
            )

        moment = opened_at or datetime.now(UTC)

        trade = Trade(
            user_id=user_id,
            exchange=exchange,
            symbol=symbol.upper(),
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            fees=fee,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage,
            risk_reward=risk_reward,
            risk_percent=risk_percent,
            account_balance_at_entry=account_balance,
            strategy_id=strategy_id,
            timeframe=timeframe,
            entry_reason=entry_reason,
            notes=notes,
            status=TradeStatus.OPEN,
            source=source,
            is_annotated=source is TradeSource.MANUAL,
            external_position_id=external_position_id,
            opened_at=moment,
        )
        # Первое исполнение задаётся в конструкторе, до flush.
        # Пока объект transient, обращение к коллекции не требует IO;
        # после flush тот же append вызвал бы ленивую загрузку и упал
        # с MissingGreenlet. Cascade сохранит fill вместе со сделкой.
        trade.fills = [
            TradeFill(
                user_id=user_id,
                exchange=exchange,
                fill_side=FillSide.ENTRY,
                price=entry_price,
                quantity=quantity,
                fee=fee,
                executed_at=moment,
            )
        ]
        # По той же причине инициализируем и mistakes: у новой сделки их
        # нет, но без явного присваивания первое же обращение (например
        # при отрисовке карточки) уйдёт в ленивую загрузку и упадёт.
        trade.mistakes = []

        self._trades.add(trade)
        await self._trades.flush()

        logger.info(
            "Сделка открыта",
            extra={"trade_id": trade.id, "symbol": trade.symbol, "side": side.value},
        )
        return trade

    # --- Добавление исполнения --------------------------------------------

    async def add_fill(
        self,
        trade: Trade,
        *,
        fill_side: FillSide,
        price: Decimal,
        quantity: Decimal,
        fee: Decimal = ZERO,
        executed_at: datetime | None = None,
        external_fill_id: str | None = None,
    ) -> Trade:
        """Добавляет исполнение и пересчитывает агрегаты.

        Единственный способ изменить объём или цены сделки.
        """
        if trade.status is TradeStatus.CANCELLED:
            raise JournalError("Нельзя добавить исполнение в отменённую сделку")
        if quantity <= ZERO or price <= ZERO:
            raise JournalError("Цена и объём должны быть положительными")

        await self._ensure_fills_loaded(trade)
        trade.fills.append(
            TradeFill(
                user_id=trade.user_id,
                exchange=trade.exchange,
                fill_side=fill_side,
                price=price,
                quantity=quantity,
                fee=fee,
                executed_at=executed_at or datetime.now(UTC),
                external_fill_id=external_fill_id,
            )
        )
        self.recalculate(trade)
        await self._trades.flush()
        return trade

    # --- Закрытие ----------------------------------------------------------

    async def close_trade(
        self,
        trade: Trade,
        *,
        exit_price: Decimal,
        quantity: Decimal | None = None,
        fee: Decimal = ZERO,
        exit_reason: str | None = None,
        closed_at: datetime | None = None,
    ) -> Trade:
        """Закрывает сделку целиком или частично.

        quantity=None означает «закрыть весь оставшийся объём» — самый
        частый случай, поэтому он и является поведением по умолчанию.
        """
        if trade.status is TradeStatus.CLOSED:
            raise JournalError("Сделка уже закрыта")

        aggregate = self._aggregate(trade)
        remaining = aggregate.open_quantity
        if remaining <= ZERO:
            raise JournalError("Нет открытого объёма для закрытия")

        closing_qty = quantity if quantity is not None else remaining
        if closing_qty > remaining:
            raise JournalError(
                f"Нельзя закрыть {closing_qty}: открыто только {remaining}"
            )

        moment = closed_at or datetime.now(UTC)
        await self.add_fill(
            trade,
            fill_side=FillSide.EXIT,
            price=exit_price,
            quantity=closing_qty,
            fee=fee,
            executed_at=moment,
        )

        if exit_reason:
            trade.exit_reason = exit_reason

        if self._aggregate(trade).open_quantity <= ZERO:
            trade.status = TradeStatus.CLOSED
            trade.closed_at = moment

        await self._trades.flush()
        logger.info(
            "Сделка закрыта",
            extra={
                "trade_id": trade.id,
                "pnl": str(trade.pnl),
                "status": trade.status.value,
            },
        )
        return trade

    async def cancel_trade(self, trade: Trade) -> Trade:
        if trade.status is TradeStatus.CLOSED:
            raise JournalError("Закрытую сделку нельзя отменить")
        trade.status = TradeStatus.CANCELLED
        await self._trades.flush()
        return trade

    # --- Разметка ----------------------------------------------------------

    async def annotate(
        self,
        trade: Trade,
        *,
        strategy_id: int | None = None,
        timeframe: str | None = None,
        entry_reason: str | None = None,
        exit_reason: str | None = None,
        notes: str | None = None,
        mistake_type_ids: list[int] | None = None,
        mark_annotated: bool = True,
    ) -> Trade:
        """Проставляет субъективные поля импортированной сделке.

        Биржа отдаёт цены и комиссии, но не знает стратегию и причину
        входа. До разметки сделка не участвует в статистике по
        стратегиям и ошибкам, иначе срезы окажутся смещёнными.
        """
        if strategy_id is not None:
            trade.strategy_id = strategy_id
        if timeframe is not None:
            trade.timeframe = timeframe
        if entry_reason is not None:
            trade.entry_reason = entry_reason
        if exit_reason is not None:
            trade.exit_reason = exit_reason
        if notes is not None:
            trade.notes = notes

        if mistake_type_ids is not None:
            existing = {m.mistake_type_id for m in trade.mistakes}
            for mistake_id in mistake_type_ids:
                if mistake_id not in existing:
                    self._trades.session.add(
                        TradeMistake(trade_id=trade.id, mistake_type_id=mistake_id)
                    )

        if mark_annotated:
            trade.is_annotated = True

        await self._trades.flush()
        return trade

    # --- Пересчёт ----------------------------------------------------------

    async def _ensure_fills_loaded(self, trade: Trade) -> None:
        """Подгружает fills, если объект пришёл без них.

        Явная проверка вместо безусловного refresh: refresh на каждом
        вызове означал бы лишний SELECT там, где коллекция уже в памяти.
        """
        from sqlalchemy import inspect as sa_inspect

        if "fills" in sa_inspect(trade).unloaded:
            await self._trades.session.refresh(trade, ["fills"])

    @staticmethod
    def _aggregate(trade: Trade):  # type: ignore[no-untyped-def]
        return aggregate_fills(
            FillData(
                fill_side=f.fill_side,
                price=f.price,
                quantity=f.quantity,
                fee=f.fee,
            )
            for f in trade.fills
        )

    def recalculate(self, trade: Trade) -> Trade:
        """Пересчитывает все производные поля из fills.

        Идемпотентна: повторный вызов на тех же данных даёт тот же
        результат. Это позволяет безопасно вызывать её после любого
        изменения и использовать для починки исторических записей.
        """
        aggregate = self._aggregate(trade)

        trade.entry_price = aggregate.entry_price
        trade.exit_price = aggregate.exit_price
        trade.quantity = aggregate.entry_quantity
        trade.fees = aggregate.total_fees

        if aggregate.entry_price is None or aggregate.exit_quantity <= ZERO:
            trade.pnl = None
            trade.pnl_percent = None
            return trade

        assert aggregate.exit_price is not None

        try:
            pnl = calculate_pnl(
                entry_price=aggregate.entry_price,
                exit_price=aggregate.exit_price,
                quantity=aggregate.exit_quantity,
                side=trade.side,
                fees=aggregate.total_fees,
            )
        except CalculationError:
            logger.exception("Не удалось пересчитать PnL", extra={"trade_id": trade.id})
            return trade

        trade.pnl = pnl

        # PnL % считаем от депозита, если он известен на момент входа:
        # эта метрика сопоставима между сделками и складывается в просадку.
        # Иначе — от вложенной маржи.
        if trade.account_balance_at_entry and trade.account_balance_at_entry > ZERO:
            trade.pnl_percent = calculate_pnl_percent_of_balance(
                pnl=pnl, account_balance=trade.account_balance_at_entry
            )
        else:
            trade.pnl_percent = calculate_pnl_percent(
                pnl=pnl,
                entry_price=aggregate.entry_price,
                quantity=aggregate.exit_quantity,
                leverage=trade.leverage,
            )

        if trade.stop_loss is not None:
            try:
                trade.risk_reward = calculate_realized_rr(
                    entry_price=aggregate.entry_price,
                    stop_loss=trade.stop_loss,
                    exit_price=aggregate.exit_price,
                    side=trade.side,
                )
            except CalculationError:
                trade.risk_reward = None

        return trade
