"""Интеграционный тест импорта: поддельная биржа → настоящая база.

Главная проверка — идемпотентность. Окна запросов к бирже намеренно
перекрываются, чтобы не терять исполнения на границах, поэтому повторы
приходят всегда. Если дедупликация сломается, история задвоится, а
статистика покажет вдвое больший результат.
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
from app.database.models.trade import Trade
from app.database.repositories.strategy import (
    MistakeTypeRepository,
    StrategyRepository,
)
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ApiRestrictions,
    Balance,
    ExchangeClient,
    Fill,
    Kline,
    OrderResult,
    Position,
    SymbolInfo,
    Ticker,
)
from app.services.import_service import HistoryImporter
from app.services.user_service import UserService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    TradeSide,
    TradeSource,
    TradeStatus,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL"
)

D = Decimal
BASE = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


class FakeExchange(ExchangeClient):
    """Биржа, отдающая заданный набор исполнений.

    Считает обращения: так видно, что импорт режет период на окна, а не
    запрашивает год одним вызовом.
    """

    name = "bingx"

    def __init__(self, fills: list[Fill]) -> None:
        self._fills = fills
        self.calls = 0

    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]:
        self.calls += 1
        return [f for f in self._fills if start_time <= f.executed_at < end_time]

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker: ...
    async def get_klines(self, symbol, interval, limit=500, end_time=None) -> list[Kline]: ...  # type: ignore[no-untyped-def]
    async def get_symbols(self, *, max_retries: int | None = None) -> list[SymbolInfo]: ...
    async def get_funding_rate(self, symbol: str) -> Decimal | None: ...
    async def get_balance(self, *, max_retries: int | None = None) -> Balance: ...
    async def get_positions(self, *, max_retries=None) -> list[Position]: ...  # type: ignore[no-untyped-def]
    async def get_api_restrictions(self) -> ApiRestrictions: ...
    async def get_leverage(self, symbol, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def get_position_mode(self, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def set_leverage(self, symbol, leverage, *, position_side=None) -> int: ...  # type: ignore[no-untyped-def]
    async def place_market_order(self, **kwargs) -> OrderResult: ...  # type: ignore[no-untyped-def]
    async def get_order(self, symbol, client_order_id, *, max_retries=None) -> OrderResult: ...  # type: ignore[no-untyped-def]
    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def place_conditional_order(self, **kwargs): ...  # type: ignore[no-untyped-def]
    async def get_open_orders(self, symbol: str | None = None, *, max_retries=None) -> list: ...  # type: ignore[no-untyped-def]
    async def close(self) -> None: ...


def fill(
    fid: str, minutes: int, *, entry: bool, price: str, qty: str = "0.1",
    order_id: str | None = None,
) -> Fill:
    return Fill(
        external_id=fid,
        symbol="BTC-USDT",
        side=TradeSide.LONG,
        is_entry=entry,
        price=D(price),
        quantity=D(qty),
        fee=D("4"),
        realized_pnl=D(0),
        executed_at=BASE + timedelta(minutes=minutes),
        order_id=order_id,
    )


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        svc = UserService(
            UserRepository(session),
            StrategyRepository(session),
            MistakeTypeRepository(session),
            settings,
        )
        user = await svc.get_or_create(telegram_id=unique_telegram_id())
        yield user, TradeRepository(session), session
        await cleanup_user(session, user)
    await db.dispose()


async def test_import_creates_closed_trade_with_pnl(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, session = ctx
    exchange = FakeExchange([
        fill("f1", 0, entry=True, price="100000"),
        fill("f2", 60, entry=False, price="102000"),
    ])

    importer = HistoryImporter(exchange, repo, user.id)
    result = await importer.import_period(
        BASE - timedelta(hours=1),
        BASE + timedelta(hours=2),
        account_balance=D("10000"),
    )

    assert result.fills_received == 2
    assert result.trades_created == 1

    trades = await repo.list_recent(user.id)
    trade = trades[0]
    assert trade.status is TradeStatus.CLOSED
    assert trade.source is TradeSource.IMPORTED
    assert trade.is_annotated is False       # ждёт разметки пользователем
    assert trade.entry_price == D("100000")
    assert trade.exit_price == D("102000")
    # (102000 - 100000) * 0.1 = 200, минус 8 комиссий
    assert trade.pnl == D("192")
    assert trade.pnl_percent == D("1.9200")


async def test_repeated_import_does_not_duplicate(ctx) -> None:  # type: ignore[no-untyped-def]
    """Идемпотентность: повторный импорт того же периода ничего не добавит."""
    user, repo, session = ctx
    exchange = FakeExchange([
        fill("d1", 0, entry=True, price="100000"),
        fill("d2", 60, entry=False, price="102000"),
    ])
    importer = HistoryImporter(exchange, repo, user.id)
    window = (BASE - timedelta(hours=1), BASE + timedelta(hours=2))

    first = await importer.import_period(*window)
    await session.flush()
    second = await importer.import_period(*window)

    assert first.trades_created == 1
    assert second.fills_received == 2
    assert second.fills_new == 0
    assert second.trades_created == 0
    assert len(await repo.list_recent(user.id)) == 1


async def test_open_position_imported_as_open_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, session = ctx
    exchange = FakeExchange([fill("o1", 0, entry=True, price="100000")])

    await HistoryImporter(exchange, repo, user.id).import_period(
        BASE - timedelta(hours=1), BASE + timedelta(hours=2)
    )

    trade = (await repo.list_open(user.id))[0]
    assert trade.status is TradeStatus.OPEN
    assert trade.pnl is None
    assert trade.exit_price is None


async def test_long_period_is_split_into_windows(ctx) -> None:  # type: ignore[no-untyped-def]
    """Год истории не запрашивается одним вызовом."""
    user, repo, _ = ctx
    exchange = FakeExchange([])

    await HistoryImporter(exchange, repo, user.id).import_period(
        BASE - timedelta(days=365), BASE
    )

    # Окно 7 дней с перекрытием — порядка полусотни запросов.
    assert exchange.calls > 45


async def test_partial_failure_reports_but_keeps_data(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сбой одного окна не должен отменять успешно загруженные."""
    user, repo, _ = ctx

    class FlakyExchange(FakeExchange):
        """Второе окно всегда падает — имитация обрыва связи."""

        def __init__(self, fills: list[Fill]) -> None:
            super().__init__(fills)
            self.windows = 0

        async def get_fills(self, start_time, end_time, symbol=None):  # type: ignore[no-untyped-def]
            # Отдельный счётчик окон: self.calls увеличивается и в
            # родительском методе, поэтому опираться на него нельзя.
            self.windows += 1
            if self.windows == 2:
                raise TimeoutError("окно не загрузилось")
            return await super().get_fills(start_time, end_time, symbol)

    exchange = FlakyExchange([
        fill("p1", 0, entry=True, price="100000"),
        fill("p2", 60, entry=False, price="102000"),
    ])

    result = await HistoryImporter(exchange, repo, user.id).import_period(
        BASE - timedelta(hours=1), BASE + timedelta(days=10)
    )

    assert result.errors                      # о сбое сообщено
    assert result.trades_created == 1         # но данные не потеряны



# --- Шаг 15.5.4: исполнения ордеров бота не импортируются --------------------


async def _bot_order(session, user_id: int, exchange_order_id: str) -> None:  # type: ignore[no-untyped-def]
    """Строка execution_orders с orderId биржи — след входа/стопа бота."""
    session.add(ExecutionOrder(
        user_id=user_id, client_order_id=f"tj-import-{user_id}-{exchange_order_id}",
        exchange_order_id=exchange_order_id, symbol="BTC-USDT", side=OrderSide.BUY,
        position_side=TradeSide.LONG, order_type=OrderType.MARKET, role=OrderRole.ENTRY,
        status=OrderStatus.FILLED,
    ))
    await session.flush()


async def test_bot_order_fills_are_skipped(ctx) -> None:  # type: ignore[no-untyped-def]
    """Сделка бота уже в журнале (с notification_id), у её TradeFill нет
    tradeId биржи — импорт не должен завести вторую сделку на ту же
    позицию. Ручная сделка по соседству импортируется как раньше."""
    user, repo, session = ctx
    await _bot_order(session, user.id, "BOT-ENTRY")
    await _bot_order(session, user.id, "BOT-STOP")
    exchange = FakeExchange([
        fill("b1", 0, entry=True, price="60000", order_id="BOT-ENTRY"),
        fill("b2", 30, entry=False, price="59000", order_id="BOT-STOP"),
        fill("m1", 60, entry=True, price="61000", order_id="MANUAL-1"),
        fill("m2", 90, entry=False, price="62000", order_id="MANUAL-2"),
    ])
    importer = HistoryImporter(exchange, repo, user.id)

    result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.fills_skipped_bot == 2
    assert result.trades_created == 1
    [trade] = list(await session.scalars(select(Trade).where(Trade.user_id == user.id)))
    assert trade.entry_price == D("61000")
    assert "Пропущено исполнений ордеров бота: 2" in result.render()


async def test_manual_exit_of_bot_position_makes_no_half_trade(ctx) -> None:  # type: ignore[no-untyped-def]
    """Вход бота пропущен, а позицию закрыли руками на бирже (выход — не
    наш ордер): выход без входа отбрасывается, половинчатой сделки нет."""
    user, repo, session = ctx
    await _bot_order(session, user.id, "BOT-ENTRY")
    exchange = FakeExchange([
        fill("b1", 0, entry=True, price="60000", order_id="BOT-ENTRY"),
        fill("x1", 30, entry=False, price="59000", order_id="MANUAL-EXIT"),
    ])
    importer = HistoryImporter(exchange, repo, user.id)

    result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.trades_created == 0
    assert list(await session.scalars(select(Trade).where(Trade.user_id == user.id))) == []


async def test_fill_without_order_id_is_imported_with_warning(ctx, caplog) -> None:  # type: ignore[no-untyped-def]
    """Без orderId исполнение нельзя отличить от ордеров бота — импорт как
    раньше, но не тихо: warning о возможном дубле."""
    user, repo, session = ctx
    await _bot_order(session, user.id, "BOT-ENTRY")
    exchange = FakeExchange([
        fill("n1", 0, entry=True, price="60000", order_id=None),
    ])
    importer = HistoryImporter(exchange, repo, user.id)

    with caplog.at_level("WARNING", logger="app.services.import_service"):
        result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.trades_created == 1
    assert "возможен дубль" in caplog.text
