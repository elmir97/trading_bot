"""Тесты app/execution/service.py (этап 15.4, раздел 1 ТЗ).

Против настоящей БД (нужны TradeRepository/сигнал/план — как остальные
интеграционные тесты в наборе), но биржа замокана полностью: FakeExchangeClient
реализует только то, чем пользуется ExecutionService (тикер, баланс,
список инструментов), ни один тест не ходит на BingX (раздел 13 ТЗ).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import Settings
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    Balance,
    ExchangeClient,
    Fill,
    Kline,
    Position,
    SymbolInfo,
    Ticker,
)
from app.execution.models import ExecutionRefusal
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.service import ExecutionQuote, ExecutionService, build_execution_orders
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.user_service import UserService
from app.trading.enums import (
    ExchangeKeyMode,
    OrderRole,
    OrderStatus,
    SignalDirection,
    SignalLevel,
    TradeSide,
    TradeStatus,
)

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)


class FakeExchangeClient(ExchangeClient):
    """Реализует только то, чем реально пользуется ExecutionService.evaluate()."""

    name = "fake"

    def __init__(self, *, price: Decimal, balance: Decimal, symbol_info: SymbolInfo) -> None:
        self.price = price
        self.balance = balance
        self.symbol_info = symbol_info
        self.closed = False

    async def get_ticker(self, symbol: str) -> Ticker:
        return Ticker(
            symbol=symbol, last_price=self.price, volume_24h=D("0"),
            price_change_percent=D("0"), timestamp=NOW,
        )

    async def get_klines(self, symbol, interval, limit=500, end_time=None) -> list[Kline]:
        raise NotImplementedError

    async def get_symbols(self) -> list[SymbolInfo]:
        return [self.symbol_info]

    async def get_funding_rate(self, symbol: str) -> Decimal | None:
        return None

    async def get_balance(self) -> Balance:
        return Balance(
            asset="USDT", available=self.balance, used_margin=D("0"),
            unrealized_pnl=D("0"), equity=self.balance,
        )

    async def get_positions(self) -> list[Position]:
        return []

    async def get_fills(self, start_time, end_time, symbol=None) -> list[Fill]:
        return []

    async def set_leverage(self, symbol, leverage, *, position_side=None) -> int:
        raise NotImplementedError

    async def place_market_order(self, **kwargs) -> object:  # type: ignore[override]
        raise NotImplementedError

    async def get_order(self, symbol, client_order_id) -> object:  # type: ignore[override]
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


def _symbol_info() -> SymbolInfo:
    return SymbolInfo(
        symbol="BTC-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), max_leverage=50, min_notional=D("5"),
    )


def _signal(user_id: int, **overrides: object) -> SignalRecord:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "BTC-USDT",
        "timeframe": "4h",
        "level": SignalLevel.READY,
        "direction": SignalDirection.LONG,
        "setup": "Пробой с ретестом",
        "fingerprint": "fp",
        "detail": "detail",
        "entry_low": D("100"),
        "entry_high": D("101"),
        "stop_loss": D("97"),
        "take_profit": D("110"),
        "expires_at": NOW + timedelta(hours=4),
    }
    fields.update(overrides)
    return SignalRecord(**fields)  # type: ignore[arg-type]


def _open_trade(user_id: int, **overrides: object) -> Trade:
    fields: dict[str, object] = {
        "user_id": user_id,
        "symbol": "ETH-USDT",
        "side": TradeSide.LONG,
        "status": TradeStatus.OPEN,
        "risk_percent": D("1"),
        "opened_at": NOW,
    }
    fields.update(overrides)
    return Trade(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def ctx():  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        telegram_id = 900_000 + int(datetime.now(UTC).timestamp() * 1000) % 90_000
        user = await user_service.get_or_create(telegram_id=telegram_id)
        client = FakeExchangeClient(price=D("100"), balance=D("1000"), symbol_info=_symbol_info())
        market = MarketDataService(client, TTLCache())
        yield session, user, client, market
    await db.dispose()


def _service(session, settings: Settings, client, market) -> ExecutionService:
    return ExecutionService(session=session, settings=settings, client=client, market=market)


async def test_execution_disabled_refuses_without_touching_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    """Settings() по умолчанию TRADING_EXECUTION_ENABLED=false — guard #1
    обязан сработать раньше любого обращения к бирже."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    service = ExecutionService(session=session, settings=Settings(), client=client, market=market)  # type: ignore[call-arg]

    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.EXECUTION_DISABLED
    assert client.balance == D("1000")  # get_balance не вызывался — цена/баланс не тронуты


async def test_execution_disabled_writes_refused_observation_without_price(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 12а ТЗ: отказ гварда пишется сам собой, ещё до похода на
    биржу за ценой — price/price_drift_percent у такой строки NULL."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    service = ExecutionService(session=session, settings=Settings(), client=client, market=market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)

    rows = list(
        await session.scalars(select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id))
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status is OrderStatus.REFUSED
    assert row.role is OrderRole.ENTRY
    assert row.error_code == Code.EXECUTION_DISABLED.value
    assert row.client_order_id is None
    assert row.price is None
    assert row.quantity is None


async def test_max_positions_refused_observation_captures_price_and_drift(ctx) -> None:  # type: ignore[no-untyped-def]
    """Гварды после захода на биржу (раздел 7, пп. 3-12) уже знают цену и
    дрейф на момент отказа — они попадают в строку-наблюдение."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, exec_max_open_positions=2, bingx_trading_mode="live"
    )
    for i in range(2):
        session.add(_open_trade(user.id, symbol=f"ALT{i}-USDT"))
    await session.flush()

    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.MAX_POSITIONS

    rows = list(
        await session.scalars(select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id))
    )
    assert len(rows) == 1
    assert rows[0].error_code == Code.MAX_POSITIONS.value
    assert rows[0].price == D("100")  # цена тикера FakeExchangeClient
    assert rows[0].price_drift_percent is not None  # сигнал на 100.5, цена 100 — не 0


async def test_no_trading_key_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=False, key_can_trade_futures=False,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.NO_TRADING_KEY


async def test_mode_not_allowed_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    """Этап 15.4в: конфиг разрешает только demo (Settings() по умолчанию),
    а в настройках выбран LIVE — вход отказан раньше похода на биржу."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.MODE_NOT_ALLOWED
    assert client.balance == D("1000")  # до биржи не дошло — как и NO_TRADING_KEY


async def test_valid_ready_signal_returns_quote(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    settings = Settings(trading_execution_enabled=True, bingx_trading_mode="live")  # type: ignore[call-arg]
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionQuote)
    order = result.order
    assert order.symbol == "BTC-USDT"
    assert order.position_side is TradeSide.LONG
    assert order.quantity > D("0")
    assert order.entry_price == D("100")
    assert order.stop_loss == D("97")
    assert order.take_profit == D("110")
    assert order.risk_reward > D("0")
    assert result.open_positions_count == 0
    assert result.total_risk_after_percent == order.risk_percent


async def test_signal_already_used_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id, trade_opened_at=NOW)
    session.add(signal)
    await session.flush()

    settings = Settings(trading_execution_enabled=True, bingx_trading_mode="live")  # type: ignore[call-arg]
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SIGNAL_ALREADY_USED


async def test_existing_position_on_symbol_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    session.add(_open_trade(user.id, symbol="BTC-USDT"))
    await session.flush()

    settings = Settings(trading_execution_enabled=True, bingx_trading_mode="live")  # type: ignore[call-arg]
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.POSITION_EXISTS


async def test_max_positions_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, exec_max_open_positions=2, bingx_trading_mode="live"
    )
    for i in range(2):
        session.add(_open_trade(user.id, symbol=f"ALT{i}-USDT"))
    await session.flush()

    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.MAX_POSITIONS


async def test_price_drift_refuses_on_second_evaluation(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 5 ТЗ: карточка показана по цене 100, к "Да" цена ушла на
    30%+ дистанции до стопа (дистанция 3 → допустимо 0.9) — отказ."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    settings = Settings(trading_execution_enabled=True, bingx_trading_mode="live")  # type: ignore[call-arg]
    service = _service(session, settings, client, market)

    first = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(first, ExecutionQuote)
    planned_price = first.order.entry_price

    client.price = D("105")  # дрейф 5 > 0.3 * 3 = 0.9
    second = await service.evaluate(
        user=user, signal=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE,
        planned_price=planned_price, now=NOW,
    )
    assert isinstance(second, ExecutionRefusal)
    assert second.code is Code.PRICE_DRIFT


def test_build_execution_orders_creates_entry_stop_take() -> None:
    from app.execution.models import OrderRequest
    from app.trading.enums import OrderSide

    order = OrderRequest(
        user_id=1, signal_id=2, symbol="BTC-USDT", side=OrderSide.BUY,
        position_side=TradeSide.LONG, quantity=D("0.01"), entry_price=D("100"),
        leverage=10, stop_loss=D("97"), take_profit=D("110"), notional=D("1"),
        margin=D("0.1"), risk_amount=D("2"), risk_percent=D("2"), risk_reward=D("3"),
    )
    rows = build_execution_orders(order, OrderStatus.DRY_RUN)
    assert {r.role for r in rows} == {OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}
    assert all(r.status is OrderStatus.DRY_RUN for r in rows)
    assert len({r.client_order_id for r in rows}) == 3  # все три различны

    entry = next(r for r in rows if r.role is OrderRole.ENTRY)
    stop = next(r for r in rows if r.role is OrderRole.STOP_LOSS)
    take = next(r for r in rows if r.role is OrderRole.TAKE_PROFIT)
    assert entry.side is OrderSide.BUY  # вход — как задано в OrderRequest
    assert stop.side is OrderSide.SELL and take.side is OrderSide.SELL  # закрытие лонга — SELL
    assert stop.trigger_price == D("97")
    assert take.trigger_price == D("110")
