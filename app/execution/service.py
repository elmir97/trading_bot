"""Оркестратор исполнения (этап 15.4, раздел 1 ТЗ).

evaluate() собирает контекст (цена, баланс, инструмент, открытые позиции,
дневной PnL — всё, что guards.py и sizing.py сами не умеют достать),
вызывает run_guards(), затем calculate_size(), и возвращает либо готовый
OrderRequest (обёрнутый в ExecutionQuote вместе с тем, что нужно карточке
для отображения), либо ExecutionRefusal. Отправки на биржу здесь нет и в
этом шаге не будет — это appears только в 15.5.

Дешёвые guard-и (EXECUTION_DISABLED, NO_TRADING_KEY) проверяются до
похода в БД и на биржу: если результат уже предрешён, незачем тратить
запрос к бирже ради баланса, который всё равно не понадобится.

Раздел 12а ТЗ: каждый отказ (любого гварда, в т.ч. двух дешёвых выше)
здесь же пишет строку-наблюдение в execution_orders (status REFUSED) —
это единственное место, которое знает код гварда и то немногое, что уже
успело стать известным к моменту отказа (цена/дрейф, если тикер уже
запрошен). Успешный путь (ExecutionQuote) ничего не пишет: итог карточки
(подтверждена/отклонена/просрочена) решается позже и не в этом методе —
см. app/bot/handlers/execution.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.trading_plan import TradingPlan
from app.database.models.user import User
from app.database.repositories.execution_order import ExecutionOrderRepository
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeClient
from app.execution.guards import GuardInputs, check_execution_enabled, check_trading_key, run_guards
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode, OrderRequest
from app.execution.sizing import calculate_size
from app.market.data import MarketDataService
from app.trading.calculations import PERCENT_PRECISION, calculate_risk_reward
from app.trading.enums import OrderRole, OrderSide, OrderStatus, OrderType, TradeSide
from app.trading.risk import day_bounds, tz_offset_for

ZERO = Decimal(0)


def signal_reference_price(signal: SignalRecord) -> Decimal | None:
    """Опорная цена сигнала для дрейфа — середина entry-зоны (раздел 5 ТЗ:
    карточка показывает "сигнал был на ...")."""
    if signal.entry_low is not None and signal.entry_high is not None:
        return (signal.entry_low + signal.entry_high) / 2
    return signal.entry_low or signal.entry_high


def price_drift_percent(current_price: Decimal, signal: SignalRecord) -> Decimal | None:
    """Дрейф текущей цены от опорной цены сигнала — та же величина, что
    показана на карточке (раздел 5) и копится в сводке (раздел 12а)."""
    reference = signal_reference_price(signal)
    if reference is None or reference <= ZERO:
        return None
    return (abs(current_price - reference) / reference * Decimal(100)).quantize(
        PERCENT_PRECISION
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionQuote:
    """Успешный результат evaluate() — то, что нужно карточке подтверждения
    (раздел 5 ТЗ) сверх самого OrderRequest: срез "было до входа"."""

    order: OrderRequest
    open_positions_count: int
    current_total_risk_percent: Decimal

    @property
    def total_risk_after_percent(self) -> Decimal:
        return self.current_total_risk_percent + self.order.risk_percent


class ExecutionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        client: ExchangeClient,
        market: MarketDataService,
    ) -> None:
        self._settings = settings
        self._client = client
        self._market = market
        self._session = session
        self._trades = TradeRepository(session)
        self._orders = ExecutionOrderRepository(session)

    async def _refuse(
        self,
        refusal: ExecutionRefusal,
        *,
        user_id: int,
        signal_id: int,
        symbol: str,
        side: OrderSide,
        position_side: TradeSide,
        price: Decimal | None,
        drift: Decimal | None,
    ) -> ExecutionRefusal:
        """Раздел 12а ТЗ: одна строка-наблюдение на каждый отказ гварда,
        независимо от того, успел ли код добраться до цены/дрейфа."""
        row = build_observation_order(
            user_id=user_id,
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            position_side=position_side,
            status=OrderStatus.REFUSED,
            price=price,
            price_drift_percent=drift,
            error_code=refusal.code.value,
            error_message=refusal.message,
        )
        self._orders.add(row)
        await self._orders.flush()
        return refusal

    async def evaluate(
        self,
        *,
        user: User,
        signal: SignalRecord,
        plan: TradingPlan,
        has_trading_key: bool,
        key_can_trade_futures: bool,
        planned_price: Decimal | None = None,
        now: datetime | None = None,
    ) -> ExecutionQuote | ExecutionRefusal:
        """planned_price=None — первый показ карточки: дрейф сравнивается
        сам с собой (0) и не может отказать. При повторном вызове с "Да"
        planned_price — цена, зафиксированная на карточке (раздел 5)."""
        moment = now or datetime.now(UTC)

        if signal.direction is None or signal.stop_loss is None or signal.take_profit is None:
            # Кнопка показывается только под READY (см. handlers/execution.py),
            # а у READY эти поля всегда заполнены сканером — попадание сюда
            # значит, что где-то выше пропустили проверку уровня сигнала.
            raise ValueError(f"Сигнал {signal.id} не READY: нет направления/уровней")
        side = TradeSide(signal.direction.value)
        order_side = OrderSide.BUY if side is TradeSide.LONG else OrderSide.SELL

        async def refuse(
            refusal: ExecutionRefusal,
            *,
            price: Decimal | None = None,
            drift: Decimal | None = None,
        ) -> ExecutionRefusal:
            return await self._refuse(
                refusal,
                user_id=user.id,
                signal_id=signal.id,
                symbol=signal.symbol,
                side=order_side,
                position_side=side,
                price=price,
                drift=drift,
            )

        if refusal := check_execution_enabled(
            execution_enabled=self._settings.trading_execution_enabled
        ):
            return await refuse(refusal)
        if refusal := check_trading_key(
            has_key=has_trading_key, key_can_trade_futures=key_can_trade_futures
        ):
            return await refuse(refusal)

        ticker = await self._client.get_ticker(signal.symbol)
        current_price = ticker.last_price
        if planned_price is None:
            planned_price = current_price
        drift = price_drift_percent(current_price, signal)

        symbol_info = await self._market.get_symbol_info(signal.symbol)
        if symbol_info is None:
            return await refuse(
                ExecutionRefusal(
                    ExecutionRefusalCode.SYMBOL_NOT_ALLOWED,
                    f"{signal.symbol}: нет данных об инструменте на бирже.",
                ),
                price=current_price,
                drift=drift,
            )

        balance = (await self._client.get_balance()).equity

        open_trades = await self._trades.list_open(user.id)
        open_positions_count = len(open_trades)
        has_open_position = any(t.symbol == signal.symbol for t in open_trades)
        current_total_risk_percent = sum(
            (t.risk_percent for t in open_trades if t.risk_percent is not None), ZERO
        )

        day_start, day_end = day_bounds(moment, tz_offset_for(user.settings.timezone))
        day_pnl = await self._trades.sum_pnl_between(user.id, day_start, day_end)
        day_loss_percent = None
        if day_pnl is not None and balance > ZERO:
            day_loss_percent = max(ZERO, -day_pnl / balance * Decimal(100))

        leverage = plan.max_leverage

        guard_inputs = GuardInputs(
            execution_enabled=self._settings.trading_execution_enabled,
            has_trading_key=has_trading_key,
            key_can_trade_futures=key_can_trade_futures,
            signal_expires_at=signal.expires_at,
            now=moment,
            signal_trade_opened_at=signal.trade_opened_at,
            has_open_position=has_open_position,
            open_positions_count=open_positions_count,
            max_positions=self._settings.exec_max_open_positions,
            current_total_risk_percent=current_total_risk_percent,
            new_risk_percent=plan.risk_per_trade_percent,
            max_total_risk_percent=self._settings.exec_max_total_risk_percent,
            day_loss_percent=day_loss_percent,
            max_daily_loss_percent=plan.max_daily_loss_percent,
            planned_price=planned_price,
            current_price=current_price,
            max_price_drift_ratio=self._settings.exec_max_price_drift_ratio,
            entry_price=current_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            side=side,
            min_risk_reward=self._settings.exec_min_rr,
            account_balance=balance,
            leverage=leverage,
            symbol_info=symbol_info,
            symbol=signal.symbol,
            symbol_whitelist=self._settings.exec_symbol_whitelist_symbols,
        )

        if refusal := run_guards(guard_inputs):
            return await refuse(refusal, price=current_price, drift=drift)

        sizing = calculate_size(
            account_balance=balance,
            risk_percent=plan.risk_per_trade_percent,
            entry_price=current_price,
            stop_loss=signal.stop_loss,
            side=side,
            leverage=leverage,
            symbol_info=symbol_info,
        )
        if isinstance(sizing, ExecutionRefusal):
            # run_guards() выше уже прогнал calculate_size() с теми же
            # аргументами (раздел 7, п.11) — второй отказ здесь возможен
            # только при гонке (баланс/лот изменились между двумя
            # вызовами), не в штатном пути.
            return await refuse(sizing, price=current_price, drift=drift)

        risk_reward = calculate_risk_reward(
            entry_price=current_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            side=side,
        )
        risk_percent = (
            (sizing.risk_amount / balance * Decimal(100)).quantize(PERCENT_PRECISION)
            if balance > ZERO
            else ZERO
        )

        order = OrderRequest(
            user_id=user.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=OrderSide.BUY if side is TradeSide.LONG else OrderSide.SELL,
            position_side=side,
            quantity=sizing.quantity,
            entry_price=current_price,
            leverage=leverage,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            notional=sizing.notional,
            margin=sizing.margin,
            risk_amount=sizing.risk_amount,
            risk_percent=risk_percent,
            risk_reward=risk_reward,
        )

        return ExecutionQuote(
            order=order,
            open_positions_count=open_positions_count,
            current_total_risk_percent=current_total_risk_percent,
        )


def build_execution_orders(
    order: OrderRequest,
    status: OrderStatus,
    *,
    price_drift_percent: Decimal | None = None,
) -> list[ExecutionOrder]:
    """Раздел 8 ТЗ: один вход — три строки execution_orders (вход, стоп,
    тейк), каждая со своим client_order_id. На 15.4 status=DRY_RUN —
    строки фиксируют намерение, не отправку (появится в 15.5).

    Раздел 12а ТЗ: снимок расчёта (цена, дрейф, объём, маржа, плечо,
    риск, RR) пишется только на строку ENTRY — SL/TP-ноги того же входа
    его не повторяют, см. app/database/models/execution_order.py.
    """
    closing_side = OrderSide.SELL if order.side is OrderSide.BUY else OrderSide.BUY
    return [
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            client_order_id=order.entry_client_order_id,
            symbol=order.symbol,
            side=order.side,
            position_side=order.position_side,
            order_type=OrderType.MARKET,
            role=OrderRole.ENTRY,
            quantity=order.quantity,
            price=order.entry_price,
            status=status,
            price_drift_percent=price_drift_percent,
            notional=order.notional,
            margin=order.margin,
            leverage=order.leverage,
            risk_amount=order.risk_amount,
            risk_percent=order.risk_percent,
            risk_reward=order.risk_reward,
        ),
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            client_order_id=order.stop_loss_client_order_id,
            symbol=order.symbol,
            side=closing_side,
            position_side=order.position_side,
            order_type=OrderType.STOP_MARKET,
            role=OrderRole.STOP_LOSS,
            quantity=order.quantity,
            trigger_price=order.stop_loss,
            status=status,
        ),
        ExecutionOrder(
            user_id=order.user_id,
            signal_id=order.signal_id,
            client_order_id=order.take_profit_client_order_id,
            symbol=order.symbol,
            side=closing_side,
            position_side=order.position_side,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            role=OrderRole.TAKE_PROFIT,
            quantity=order.quantity,
            trigger_price=order.take_profit,
            status=status,
        ),
    ]


def build_observation_order(
    *,
    user_id: int,
    signal_id: int,
    symbol: str,
    side: OrderSide,
    position_side: TradeSide,
    status: OrderStatus,
    price: Decimal | None = None,
    price_drift_percent: Decimal | None = None,
    quantity: Decimal | None = None,
    notional: Decimal | None = None,
    margin: Decimal | None = None,
    leverage: int | None = None,
    risk_amount: Decimal | None = None,
    risk_percent: Decimal | None = None,
    risk_reward: Decimal | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ExecutionOrder:
    """Раздел 12а ТЗ: одна строка на исход попытки входа, которая не
    дошла до полноценной тройки build_execution_orders() — отказ гварда
    (REFUSED), отказ пользователя (DECLINED) или просрочка карточки
    (EXPIRED). Без client_order_id: это не намерение отправить что-то на
    биржу, а только наблюдение за тем, что произошло."""
    return ExecutionOrder(
        user_id=user_id,
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        position_side=position_side,
        order_type=OrderType.MARKET,
        role=OrderRole.ENTRY,
        status=status,
        price=price,
        price_drift_percent=price_drift_percent,
        quantity=quantity,
        notional=notional,
        margin=margin,
        leverage=leverage,
        risk_amount=risk_amount,
        risk_percent=risk_percent,
        risk_reward=risk_reward,
        error_code=error_code,
        error_message=error_message,
    )


def build_observation_order_from_quote(
    order: OrderRequest, *, status: OrderStatus, price_drift_percent: Decimal | None
) -> ExecutionOrder:
    """DECLINED/EXPIRED: карточка была показана и до неё дошёл полноценный
    ExecutionQuote — переиспользуем его числа вместо пересчёта."""
    return build_observation_order(
        user_id=order.user_id,
        signal_id=order.signal_id,
        symbol=order.symbol,
        side=order.side,
        position_side=order.position_side,
        status=status,
        price=order.entry_price,
        price_drift_percent=price_drift_percent,
        quantity=order.quantity,
        notional=order.notional,
        margin=order.margin,
        leverage=order.leverage,
        risk_amount=order.risk_amount,
        risk_percent=order.risk_percent,
        risk_reward=order.risk_reward,
    )
