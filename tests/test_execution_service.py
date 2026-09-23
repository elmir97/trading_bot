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
from app.database.models.signal_notification import SignalNotification
from app.database.models.trade import Trade
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ApiRestrictions,
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
from app.trading.calculations import calculate_risk_reward
from app.trading.enums import (
    ExchangeKeyMode,
    OrderRole,
    OrderStatus,
    SignalDirection,
    SignalLevel,
    SignalRecordStatus,
    TradeSide,
    TradeStatus,
)
from tests.conftest import cleanup_user

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
        # Раздел 8 ТЗ: чем evaluate() реально вызвало эти методы — по этому
        # список тесты сверяют fail-fast на подтверждении (max_retries=1)
        # против обычного пути карточки (max_retries=None).
        self.ticker_retries_seen: list[int | None] = []
        self.balance_retries_seen: list[int | None] = []
        self.symbols_retries_seen: list[int | None] = []

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        self.ticker_retries_seen.append(max_retries)
        return Ticker(
            symbol=symbol, last_price=self.price, volume_24h=D("0"),
            price_change_percent=D("0"), timestamp=NOW,
        )

    async def get_klines(self, symbol, interval, limit=500, end_time=None) -> list[Kline]:
        raise NotImplementedError

    async def get_symbols(self, *, max_retries: int | None = None) -> list[SymbolInfo]:
        self.symbols_retries_seen.append(max_retries)
        return [self.symbol_info]

    async def get_funding_rate(self, symbol: str) -> Decimal | None:
        return None

    async def get_balance(self, *, max_retries: int | None = None) -> Balance:
        self.balance_retries_seen.append(max_retries)
        return Balance(
            asset="USDT", available=self.balance, used_margin=D("0"),
            unrealized_pnl=D("0"), equity=self.balance,
        )

    async def get_positions(self) -> list[Position]:
        return []

    async def get_api_restrictions(self) -> ApiRestrictions:
        raise NotImplementedError

    async def get_fills(self, start_time, end_time, symbol=None) -> list[Fill]:
        return []

    async def get_leverage(self, symbol, *, max_retries=None) -> object:  # type: ignore[override]
        raise NotImplementedError

    async def get_position_mode(self, *, max_retries=None) -> bool:
        raise NotImplementedError

    async def set_leverage(self, symbol, leverage, *, position_side=None) -> int:
        raise NotImplementedError

    async def place_market_order(self, **kwargs) -> object:  # type: ignore[override]
        raise NotImplementedError

    async def get_order(self, symbol, client_order_id) -> object:  # type: ignore[override]
        raise NotImplementedError

    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def place_conditional_order(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_open_orders(self, symbol=None, *, max_retries=None) -> list:
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


def _symbol_info() -> SymbolInfo:
    return SymbolInfo(
        symbol="BTC-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), min_notional=D("5"),
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


async def _snapshot(session, signal: SignalRecord, **overrides: object) -> SignalNotification:  # type: ignore[no-untyped-def]
    """Шаг 15.5.2а: снимок уведомления по текущему состоянию слота — то,
    что сканер пишет в момент отправки; evaluate() строит вход из него."""
    notification = SignalNotification.snapshot_of(
        signal, notified_at=NOW, expires_at=NOW + timedelta(hours=4)
    )
    for key, value in overrides.items():
        setattr(notification, key, value)
    session.add(notification)
    await session.flush()
    return notification


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
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        client = FakeExchangeClient(price=D("100"), balance=D("1000"), symbol_info=_symbol_info())
        market = MarketDataService(client, TTLCache())
        yield session, user, client, market
        await cleanup_user(session, user)
    await db.dispose()


def _service(session, settings: Settings, client, market) -> ExecutionService:
    return ExecutionService(session=session, settings=settings, client=client, market=market)


async def test_execution_disabled_refuses_without_touching_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    """trading_execution_enabled=False явно в конструкторе — guard #1 обязан
    сработать раньше любого обращения к бирже.

    Явный аргумент, а не Settings() без параметров: последнее зависело бы
    от TRADING_EXECUTION_ENABLED в окружении запуска, а не от того, что
    тест проверяет по существу."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = ExecutionService(
        session=session,
        settings=Settings(trading_execution_enabled=False),  # type: ignore[call-arg]
        client=client,
        market=market,
    )

    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
    notification = await _snapshot(session, signal)

    service = ExecutionService(
        session=session,
        settings=Settings(trading_execution_enabled=False),  # type: ignore[call-arg]
        client=client,
        market=market,
    )
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
        trading_execution_enabled=True, exec_max_open_positions=2,
        bingx_trading_mode="live", exec_allow_live_mode_orders=True,
    )
    for i in range(2):
        session.add(_open_trade(user.id, symbol=f"ALT{i}-USDT"))
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
    notification = await _snapshot(session, signal)

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=False, key_can_trade_futures=False,
        dual_side_position=True,
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
    notification = await _snapshot(session, signal)

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.MODE_NOT_ALLOWED
    assert client.balance == D("1000")  # до биржи не дошло — как и NO_TRADING_KEY


async def test_live_orders_not_allowed_refuses_before_trading_key_check(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 16 ТЗ, шаг 15.5.1: сразу после EXECUTION_DISABLED, до похода
    за правами ключа — has_trading_key=False тоже, но отказ должен быть
    именно этим кодом, не NO_TRADING_KEY."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=False,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=False, key_can_trade_futures=False,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.LIVE_ORDERS_NOT_ALLOWED
    assert client.balance == D("1000")  # до биржи не дошло


async def test_valid_ready_signal_returns_quote(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal, trade_opened_at=NOW)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.POSITION_EXISTS


async def test_existing_position_refuses_opposite_side_too(ctx) -> None:  # type: ignore[no-untyped-def]
    """ЗАМОК ПОВЕДЕНИЯ, не регрессионный тест на починку — проходит и на
    коде до раздела 16 ТЗ (шаг 15.5.1), и после: check_no_existing_position
    сознательно НЕ трогали, сторона сигнала в сравнении не участвует, и
    это должно остаться так. Если этот тест когда-нибудь начнут "чинить"
    под сравнение по (symbol, side) — это и есть та самая порча поведения,
    от которой тест защищает.

    Почему сторона не участвует, даже в хедж-режиме (где по символу
    технически могут висеть LONG и SHORT одновременно):
      - в one-way встречный ордер НЕТТО-ЗАКРЫВАЕТ (или переворачивает)
        открытую позицию — сравнение по символу это и ловит;
      - в хедже встречный вход при уже открытой позиции — это
        ХЕДЖИРОВАНИЕ, которого в ТЗ нет (раздел 15: усреднение и докупки
        сознательно не делаем). Открытая LONG, сигнал SHORT по тому же
        символу — это разворот, разворот требует сначала закрыть позицию,
        а закрытие из бота в этот этап не входит.
    MAX_TOTAL_RISK по встречным позициям со встречными стопами тоже
    считал бы величину, лишённую смысла. Открытая LONG, сигнал на SHORT
    по тому же символу — POSITION_EXISTS в обоих режимах, не тихий
    проход. См. также docstring check_no_existing_position в guards.py —
    тот же замок описан на стороне гварда."""
    session, user, client, market = ctx
    signal = _signal(user.id, direction=SignalDirection.SHORT)
    session.add(signal)
    session.add(_open_trade(user.id, symbol="BTC-USDT", side=TradeSide.LONG))
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.POSITION_EXISTS


async def test_max_positions_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, exec_max_open_positions=2,
        bingx_trading_mode="live", exec_allow_live_mode_orders=True,
    )
    for i in range(2):
        session.add(_open_trade(user.id, symbol=f"ALT{i}-USDT"))
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
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
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)

    first = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(first, ExecutionQuote)
    planned_price = first.order.entry_price

    client.price = D("105")  # дрейф 5 > 0.3 * 3 = 0.9
    second = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE,
        planned_price=planned_price, now=NOW,
    )
    assert isinstance(second, ExecutionRefusal)
    assert second.code is Code.PRICE_DRIFT


async def test_signal_stale_refuses_on_first_evaluation(ctx) -> None:  # type: ignore[no-untyped-def]
    """Пакет B: до этого гварда цена, ушедшая далеко от сигнала ЕЩЁ ДО
    первого показа карточки, не ловилась ничем — PRICE_DRIFT на первом
    вызове структурно бессилен (planned_price := current_price), а
    check_valid_levels молчит, пока стоп не пробит и RR не просел.
    Тейк далеко (200), чтобы RR остался в норме и не замаскировал причину
    под INVALID_LEVELS."""
    session, user, client, market = ctx
    signal = _signal(user.id, take_profit=D("200"))
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)

    # reference = (100+101)/2 = 100.5, стоп 97 → дистанция 3.5, допустимо
    # (ratio по умолчанию 1.0) 3.5. Цена 106 — уход на 5.5, за порогом.
    client.price = D("106")
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SIGNAL_STALE


async def test_signal_stale_does_not_refuse_move_toward_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    """Одностороннее: движение к стопу (не к тейку) SIGNAL_STALE не трогает,
    даже далеко за тем же порогом — отказать не за что, вход стал выгоднее."""
    session, user, client, market = ctx
    signal = _signal(user.id, take_profit=D("200"))
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)

    client.price = D("98")  # к стопу, дальше допустимых 3.5 от reference 100.5
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionQuote)


async def test_permissions_untrustworthy_refuses_before_trading_key_check(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: права проверить не удалось — отказ этим кодом, а не
    NO_TRADING_KEY по устаревшему key_can_trade_futures (он обновляется
    только при успешном refresh_permissions — здесь он мог остаться
    "не может торговать" из старой проверки, хотя реальный вопрос сейчас
    в другом: мы просто не знаем текущее состояние прав)."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=False,
        permissions_trustworthy=False,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.PERMISSIONS_UNKNOWN
    assert client.balance == D("1000")  # до биржи не дошло

    rows = list(
        await session.scalars(select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id))
    )
    assert len(rows) == 1
    assert rows[0].status is OrderStatus.REFUSED
    assert rows[0].error_code == Code.PERMISSIONS_UNKNOWN.value
    assert rows[0].price is None
    assert rows[0].client_order_id is None


async def test_position_mode_unknown_refuses_before_trading_key_check(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 16 ТЗ, шаг 15.5.1 — по образцу теста PERMISSIONS_UNKNOWN
    выше: dual_side_position=None (не передан/не удалось узнать) —
    отказ этим кодом, до похода на биржу."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, Settings(trading_execution_enabled=True), client, market)  # type: ignore[call-arg]
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=None,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.POSITION_MODE_UNKNOWN
    assert client.balance == D("1000")  # до биржи не дошло


async def test_valid_signal_carries_dual_side_position_on_quote(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 16 ТЗ, шаг 15.5.1: значение несётся дальше в ExecutionQuote —
    на «Да» подхватывается оттуда, не перезапрашивается (см. handlers/
    execution.py:_build_quote, known_dual_side_position)."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=False,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionQuote)
    assert result.dual_side_position is False


async def test_symbol_data_unavailable_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    """Символ в вайтлисте (вайтлист по умолчанию пуст — разрешено всё), но
    биржа не отдала по нему SymbolInfo — отдельный код от SYMBOL_NOT_ALLOWED,
    у которого причина другая (символ осознанно вне вайтлиста)."""
    session, user, client, market = ctx
    signal = _signal(user.id)  # symbol="BTC-USDT"
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    # Биржа знает только про ETH-USDT — по BTC-USDT данных инструмента нет.
    client.symbol_info = SymbolInfo(
        symbol="ETH-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), min_notional=D("5"),
    )

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    service = _service(session, settings, client, market)
    result = await service.evaluate(
        user=user, notification=notification, slot=signal, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW,
    )
    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SYMBOL_DATA_UNAVAILABLE

    rows = list(
        await session.scalars(select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id))
    )
    assert len(rows) == 1
    assert rows[0].error_code == Code.SYMBOL_DATA_UNAVAILABLE.value
    assert rows[0].price == D("100")  # тикер уже запрошен на этом шаге
    assert rows[0].client_order_id is None


def test_build_execution_orders_creates_entry_stop_take() -> None:
    from app.execution.models import OrderRequest
    from app.trading.enums import OrderSide

    order = OrderRequest(
        user_id=1, signal_id=2, notification_id=3, symbol="BTC-USDT", side=OrderSide.BUY,
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


async def test_refusal_stage_card_without_planned_price_confirm_with_it(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 12а: planned_price=None — построение карточки (stage=card),
    planned_price задан — вызов на «Да» (stage=confirm)."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)
    service = ExecutionService(
        session=session,
        settings=Settings(trading_execution_enabled=False),  # type: ignore[call-arg]
        client=client,
        market=market,
    )
    kwargs = {
        "user": user, "notification": notification, "slot": signal, "plan": user.trading_plan,
        "has_trading_key": True, "key_can_trade_futures": True,
        "dual_side_position": True,
        "selected_exchange_mode": ExchangeKeyMode.LIVE, "now": NOW,
    }
    await service.evaluate(**kwargs)
    await service.evaluate(planned_price=D("100"), **kwargs)

    rows = list(
        await session.scalars(
            select(ExecutionOrder)
            .where(ExecutionOrder.signal_id == signal.id)
            .order_by(ExecutionOrder.id)
        )
    )
    assert [r.stage for r in rows] == ["card", "confirm"]


async def test_confirm_stage_uses_fail_fast_reads_card_stage_does_not(ctx) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: путь подтверждения («Да», planned_price задан) держит
    Redis-лок — get_ticker/get_balance/get_symbol_info обязаны звать биржу
    с max_retries=1, чтобы сбой биржи явился быстро (пакет C), а не после
    полного цикла повторов. Путь построения карточки (planned_price=None)
    не держит лок — там ничего не меняется, max_retries не передаётся
    (клиент использует свой обычный default).

    Отдельные MarketDataService с пустым кэшем на каждый вызов: иначе
    второй evaluate() забрал бы symbol_info из кэша первого (TTL_SYMBOLS
    большой) и get_symbols() на бирже вообще не позвал бы — тест ничего
    не проверил бы про max_retries на этом конкретном вызове."""
    session, user, client, _market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    kwargs = {
        "user": user, "notification": notification, "slot": signal, "plan": user.trading_plan,
        "has_trading_key": True, "key_can_trade_futures": True,
        "dual_side_position": True,
        "selected_exchange_mode": ExchangeKeyMode.LIVE, "now": NOW,
    }

    card_service = _service(session, settings, client, MarketDataService(client, TTLCache()))
    card_result = await card_service.evaluate(planned_price=None, **kwargs)
    assert isinstance(card_result, ExecutionQuote)
    assert client.ticker_retries_seen == [None]
    assert client.balance_retries_seen == [None]
    assert client.symbols_retries_seen == [None]

    confirm_service = _service(session, settings, client, MarketDataService(client, TTLCache()))
    confirm_result = await confirm_service.evaluate(planned_price=D("100"), **kwargs)
    assert isinstance(confirm_result, ExecutionQuote)
    assert client.ticker_retries_seen == [None, 1]
    assert client.balance_retries_seen == [None, 1]
    assert client.symbols_retries_seen == [None, 1]


# --- Шаг 15.5.2а: вход по снимку уведомления --------------------------------


def _live_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )


async def _evaluate(service, user, notification, slot, **kwargs):  # type: ignore[no-untyped-def]
    return await service.evaluate(
        user=user, notification=notification, slot=slot, plan=user.trading_plan,
        has_trading_key=True, key_can_trade_futures=True,
        dual_side_position=True,
        selected_exchange_mode=ExchangeKeyMode.LIVE, now=NOW, **kwargs,
    )


async def test_superseded_slot_refuses_before_any_exchange_call(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сканер записал в слот новый сетап после уведомления — SIGNAL_SUPERSEDED
    раньше тикера, баланса и списка инструментов (ни одного вызова биржи)."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)
    signal.fingerprint = "fp-new"
    signal.stop_loss = D("90")
    await session.flush()

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal, planned_price=D("100"))

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SIGNAL_SUPERSEDED
    assert client.ticker_retries_seen == []
    assert client.balance_retries_seen == []
    assert client.symbols_retries_seen == []

    [row] = list(
        await session.scalars(
            select(ExecutionOrder).where(ExecutionOrder.notification_id == notification.id)
        )
    )
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == Code.SIGNAL_SUPERSEDED.value
    assert row.stage == "confirm"
    assert row.signal_id == signal.id


async def test_expired_slot_refuses_signal_expired(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)
    signal.status = SignalRecordStatus.EXPIRED
    await session.flush()

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SIGNAL_EXPIRED
    assert client.ticker_retries_seen == []


async def test_expiry_is_read_from_snapshot_not_slot(ctx) -> None:  # type: ignore[no-untyped-def]
    """Слот продлён пересканом (expires_at в будущем), а снимок истёк —
    кнопка из этого сообщения мертва: SIGNAL_EXPIRED."""
    session, user, client, market = ctx
    signal = _signal(user.id, expires_at=NOW + timedelta(hours=4))
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal, expires_at=NOW - timedelta(seconds=1))

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SIGNAL_EXPIRED


async def test_burned_slot_accepts_new_notification(ctx) -> None:  # type: ignore[no-untyped-def]
    """signals.trade_opened_at на слоте (след входа до 15.5.2а) больше не
    сжигает слот: новое уведомление с другим сетапом проходит."""
    session, user, client, market = ctx
    signal = _signal(user.id, trade_opened_at=NOW - timedelta(days=7), fingerprint="fp-new")
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionQuote)


async def test_setup_already_traded_via_other_notification_refuses(ctx) -> None:  # type: ignore[no-untyped-def]
    """По тому же сетапу (слот + fingerprint) уже входили через другое
    уведомление — повторный вход запрещён."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    await _snapshot(session, signal, trade_opened_at=NOW - timedelta(hours=1))
    second = await _snapshot(session, signal)

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, second, signal)

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.SETUP_ALREADY_TRADED
    assert client.ticker_retries_seen == []


async def test_quote_uses_snapshot_levels_and_notification_id(ctx) -> None:  # type: ignore[no-untyped-def]
    """Стоп, тейк и направление — из снимка, client_order_id — от
    notification_id. Слот намеренно рассинхронизирован по уровням при том
    же fingerprint: так видно, откуда берутся числа."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)
    signal.stop_loss = D("50")
    signal.take_profit = D("500")
    await session.flush()

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionQuote)
    assert result.order.stop_loss == D("97")
    assert result.order.take_profit == D("110")
    assert result.order.notification_id == notification.id
    assert result.order.signal_id == signal.id
    assert result.order.entry_client_order_id == f"tj{notification.id}u{user.id}E"



# --- Шаг 15.5.3: уровни округляются до шага цены к цене входа ---------------


@pytest.mark.parametrize(
    "direction,stop,take,expected_stop,expected_take",
    [
        # precision символа — 1 (_symbol_info); уровни снимка — с 4 знаками,
        # как их пишет сканер (round_price).
        (SignalDirection.LONG, "97.1234", "110.0567", "97.2", "110.0"),
        (SignalDirection.SHORT, "103.0789", "90.0123", "103.0", "90.1"),
    ],
)
async def test_levels_rounded_toward_entry_and_sizing_uses_rounded_stop(  # type: ignore[no-untyped-def]
    ctx, direction, stop, take, expected_stop, expected_take
) -> None:
    """В запрос уходит стоп/тейк, округлённый по правилу стороны, и объём
    считается от него же: иначе биржа округлит по-своему, read-back не
    найдёт свой стоп по цене и спасение поставит второй."""
    from app.execution.sizing import calculate_size

    session, user, client, market = ctx
    signal = _signal(
        user.id, direction=direction, stop_loss=D(stop), take_profit=D(take),
        entry_low=D("100"), entry_high=D("100.5"),
    )
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionQuote), result
    assert result.order.stop_loss == D(expected_stop)
    assert result.order.take_profit == D(expected_take)
    side = TradeSide.LONG if direction is SignalDirection.LONG else TradeSide.SHORT
    expected_size = calculate_size(
        account_balance=D("1000"), risk_percent=user.trading_plan.risk_per_trade_percent,
        entry_price=D("100"), stop_loss=D(expected_stop), side=side,
        leverage=user.trading_plan.max_leverage, symbol_info=_symbol_info(),
    )
    assert result.order.quantity == expected_size.quantity



async def test_quote_carries_account_balance_for_journal(ctx) -> None:  # type: ignore[no-untyped-def]
    """Шаг 15.5.4: equity, от которого посчитан объём, едет в журнал
    (Trade.account_balance_at_entry)."""
    session, user, client, market = ctx
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)

    service = _service(session, _live_settings(), client, market)
    result = await _evaluate(service, user, notification, signal)

    assert isinstance(result, ExecutionQuote)
    assert result.account_balance == D("1000")


async def test_min_rr_and_card_rr_use_rounded_levels(ctx) -> None:  # type: ignore[no-untyped-def]
    """ЗАМОК (проверка к шагу 15.5.3, в 15.5.4): MIN_RR/INVALID_LEVELS и RR
    карточки считаются от ОКРУГЛЁННЫХ уровней — тех, что уходят на биржу.
    Сырой сетап 97.9 / 103.15 при цене 100 даёт RR ровно 1.5 (проходит
    exec_min_rr=1.5), а после округления тейка к входу (103.1) — 1.476:
    отказ. Проходит и на коде до 15.5.4 — фиксирует поведение."""
    session, user, client, market = ctx
    signal = _signal(user.id, stop_loss=D("97.9"), take_profit=D("103.15"))
    session.add(signal)
    await session.flush()
    notification = await _snapshot(session, signal)
    service = _service(session, _live_settings(), client, market)

    refused = await _evaluate(service, user, notification, signal)

    assert isinstance(refused, ExecutionRefusal)
    assert refused.code is Code.INVALID_LEVELS

    passing = _signal(
        user.id, timeframe="1h", stop_loss=D("97.9"), take_profit=D("103.25")
    )
    session.add(passing)
    await session.flush()
    passing_n = await _snapshot(session, passing)
    quote = await _evaluate(service, user, passing_n, passing)

    assert isinstance(quote, ExecutionQuote)
    assert quote.order.take_profit == D("103.2")
    # RR карточки — от округлённого тейка: (103.2-100)/(100-97.9) = 1.52…
    assert quote.order.risk_reward == calculate_risk_reward(
        entry_price=D("100"), stop_loss=D("97.9"), take_profit=D("103.2"),
        side=TradeSide.LONG,
    )



# --- Шаг 15.5.4: предварительная сделка видна гвардам ------------------------


async def _provisional_trade(session, user, signal, notification) -> None:  # type: ignore[no-untyped-def]
    """Сделка бота с неподтверждённым исполнением (UNKNOWN не найден) —
    тем же путём, что пишет хендлер."""
    from app.execution.journal_entry import record_entry_trade
    from app.execution.models import OrderRequest
    from app.execution.readback import ReadbackResult
    from app.execution.service import build_entry_order_pending
    from app.trading.enums import OrderSide

    order = OrderRequest(
        user_id=user.id, signal_id=signal.id, notification_id=notification.id,
        symbol=signal.symbol, side=OrderSide.BUY, position_side=TradeSide.LONG,
        quantity=D("0.05"), entry_price=D("100"), leverage=10, stop_loss=D("97"),
        take_profit=D("110"), notional=D("5"), margin=D("0.5"), risk_amount=D("15"),
        risk_percent=D("1.5"), risk_reward=D("3.33"),
    )
    entry = build_entry_order_pending(order)
    entry.status = OrderStatus.UNKNOWN
    session.add(entry)
    await session.flush()
    outcome = await record_entry_trade(
        session=session, entry_row=entry, order=order, notification=notification,
        timeframe=signal.timeframe, readback=ReadbackResult(entry_status=OrderStatus.UNKNOWN),
        account_balance=D("1000"), now=NOW,
    )
    assert outcome is not None and outcome.trade.fill_confirmed is False


async def test_provisional_trade_blocks_next_entry_on_symbol(ctx) -> None:  # type: ignore[no-untyped-def]
    """Неподтверждённая сделка бота блокирует следующий вход по символу —
    ложная блокировка до сверки безопаснее второй позиции."""
    session, user, client, market = ctx
    first = _signal(user.id)
    session.add(first)
    await session.flush()
    first_n = await _snapshot(session, first)
    await _provisional_trade(session, user, first, first_n)

    second = _signal(user.id, timeframe="1h")
    session.add(second)
    await session.flush()
    second_n = await _snapshot(session, second)
    service = _service(session, _live_settings(), client, market)

    result = await _evaluate(service, user, second_n, second)

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.POSITION_EXISTS


async def test_provisional_trade_counts_in_total_risk(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, client, _market = ctx
    first = _signal(user.id)
    session.add(first)
    await session.flush()
    first_n = await _snapshot(session, first)
    await _provisional_trade(session, user, first, first_n)  # риск 1.5%

    eth = _signal(user.id, symbol="ETH-USDT")
    session.add(eth)
    await session.flush()
    eth_n = await _snapshot(session, eth)
    client.symbol_info = SymbolInfo(
        symbol="ETH-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), min_notional=D("5"),
    )
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True, bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
        exec_max_total_risk_percent=user.trading_plan.risk_per_trade_percent + D("1"),
    )
    service = _service(session, settings, client, MarketDataService(client, TTLCache()))

    result = await _evaluate(service, user, eth_n, eth)

    assert isinstance(result, ExecutionRefusal)
    assert result.code is Code.MAX_TOTAL_RISK
