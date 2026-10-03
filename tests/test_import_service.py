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
from app.database.models.trade import Trade, TradeFill
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
    ExchangeKeyMode,
    FillSide,
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
DEMO = ExchangeKeyMode.DEMO
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
    async def get_balance(self, *, max_retries: int | None = None) -> Balance: ...
    async def get_positions(self, *, max_retries=None) -> list[Position]: ...  # type: ignore[no-untyped-def]
    async def get_api_restrictions(self) -> ApiRestrictions: ...
    async def get_leverage(self, symbol, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def get_position_mode(self, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def set_leverage(self, symbol, leverage, *, position_side=None) -> int: ...  # type: ignore[no-untyped-def]
    async def place_market_order(self, **kwargs) -> OrderResult: ...  # type: ignore[no-untyped-def]
    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None): ...  # type: ignore[no-untyped-def]
    async def place_conditional_order(self, **kwargs): ...  # type: ignore[no-untyped-def]
    async def get_open_orders(self, symbol: str | None = None, *, max_retries=None) -> list: ...  # type: ignore[no-untyped-def]
    async def close(self) -> None: ...


def fill(
    fid: str, minutes: int, *, entry: bool, price: str, qty: str = "0.1",
    order_id: str | None = None,
    trigger_order_id: str | None = None,
) -> Fill:
    return Fill(
        external_id=fid,
        symbol="BTC-USDT",
        side=TradeSide.LONG,
        is_entry=entry,
        price=D(price),
        quantity=D(qty),
        fee=D("4"),
        executed_at=BASE + timedelta(minutes=minutes),
        order_id=order_id,
        trigger_order_id=trigger_order_id,
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

    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)
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
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)
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

    await HistoryImporter(exchange, repo, user.id, account_mode=DEMO).import_period(
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

    await HistoryImporter(exchange, repo, user.id, account_mode=DEMO).import_period(
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

    result = await HistoryImporter(exchange, repo, user.id, account_mode=DEMO).import_period(
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
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)

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
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)

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
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)

    with caplog.at_level("WARNING", logger="app.services.import_service"):
        result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.trades_created == 1
    assert "возможен дубль" in caplog.text


async def test_stop_exit_child_order_skipped_by_trigger_id(ctx) -> None:  # type: ignore[no-untyped-def]
    """Живая форма (SOL #4, 27.09): выход по стопу — дочерний ордер со своим
    orderId, связь со стопом бота только через triggerOrderId. Раньше фильтр
    смотрел один orderId, и выход бота проходил в импорт как чужой."""
    user, repo, session = ctx
    await _bot_order(session, user.id, "BOT-ENTRY")
    await _bot_order(session, user.id, "BOT-STOP")
    exchange = FakeExchange([
        fill("b1", 0, entry=True, price="60000", order_id="BOT-ENTRY"),
        fill("b2", 30, entry=False, price="59000", order_id="CHILD-OF-STOP",
             trigger_order_id="BOT-STOP"),
    ])
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)

    result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.fills_skipped_bot == 2
    assert result.trades_created == 0


async def test_exit_recorded_on_bot_trade_is_skipped(ctx) -> None:  # type: ignore[no-untyped-def]
    """Закрытие позиции бота, которое reconciler уже записал в сделку бота
    (external_fill_id = orderId биржи), — ручной маркет без triggerOrderId.
    Импорт не заводит его второй раз."""
    user, repo, session = ctx
    trade = Trade(
        user_id=user.id, symbol="BTC-USDT", side=TradeSide.LONG, quantity=D("0.1"),
        entry_price=D("60000"), opened_at=BASE, source=TradeSource.SIGNAL_EXECUTION,
    )
    trade.fills = [
        TradeFill(user_id=user.id, fill_side=FillSide.ENTRY, price=D("60000"),
                  quantity=D("0.1"), executed_at=BASE, external_fill_id="BOT-ENTRY"),
        TradeFill(user_id=user.id, fill_side=FillSide.EXIT, price=D("59500"),
                  quantity=D("0.1"), executed_at=BASE + timedelta(minutes=20),
                  external_fill_id="MANUAL-CLOSE-OF-BOT"),
    ]
    session.add(trade)
    await session.flush()
    exchange = FakeExchange([
        fill("x1", 20, entry=False, price="59500", order_id="MANUAL-CLOSE-OF-BOT"),
    ])
    importer = HistoryImporter(exchange, repo, user.id, account_mode=DEMO)

    result = await importer.import_period(BASE - timedelta(days=1), BASE + timedelta(days=1))

    assert result.fills_skipped_bot == 1


@pytest.mark.parametrize("field", ["price", "volume", "commission"])
async def test_live_fill_with_empty_number_is_window_error_not_trade(ctx, field: str) -> None:  # type: ignore[no-untyped-def]
    """Настоящий BingXClient, живой allFillOrders SOL (вход и стоп-выход 27.09),
    у выхода поле пустое. Раньше "" → 0: импорт создавал закрытую сделку с
    ценой выхода, объёмом или комиссией 0. Теперь окно не загрузилось —
    ошибка в итоге импорта, сделки нет."""
    import httpx

    from app.exchanges.bingx import BingXClient
    from tests.bingx_fixtures import live_items

    user, repo, _ = ctx
    entry, exit_fill = live_items("allFillOrders SOL (get_fills)")
    exit_fill[field] = ""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 0, "msg": "", "data": {"fill_orders": [entry, exit_fill]}}
        )

    client = BingXClient(
        api_key="k", api_secret="s",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://t"),
    )
    try:
        result = await HistoryImporter(client, repo, user.id, account_mode=DEMO).import_period(
            datetime(2026, 9, 27, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
        )
    finally:
        await client.close()

    assert result.trades_created == 0
    assert len(result.errors) == 1 and field in result.errors[0]


def xrp(fid: str, minutes: int, *, entry: bool, qty: str, price: str = "1.52") -> Fill:
    from dataclasses import replace

    return replace(fill(fid, minutes, entry=entry, price=price, qty=qty), symbol="XRP-USDT")


# Кнопка «📥 В журнал» на экране «Позиции»: только текущая позиция. 02.10 на
# демо она занесла и две закрытые сделки разведки этапа 0 по тому же символу.
XRP_HISTORY = [
    xrp("r1", 0, entry=True, qty="40"),            # разведка: закрыта
    xrp("r2", 9, entry=False, qty="10"),
    xrp("r3", 10, entry=False, qty="30"),
    xrp("r4", 40, entry=True, qty="40"),           # разведка: закрыта
    xrp("r5", 41, entry=False, qty="10"),
    xrp("r6", 55, entry=False, qty="30"),
    fill("b1", 60, entry=True, price="100000"),    # другой символ
    xrp("c1", 90, entry=True, qty="30", price="1.5394"),   # текущая позиция
    xrp("c2", 91, entry=True, qty="10", price="1.5400"),
]
WINDOW = (BASE - timedelta(hours=1), BASE + timedelta(hours=3))


async def test_open_position_button_imports_only_current_position(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, session = ctx
    importer = HistoryImporter(FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO)
    outcome = await importer.import_open_position(
        *WINDOW, symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(40),
        position_id="2106063262781022210",
    )
    # positionId с биржи — в истории исполнений BingX его нет (02.10, #12).
    assert outcome.trade.external_position_id == "2106063262781022210"

    assert outcome.refusal is None
    [trade] = await repo.list_recent(user.id)
    assert trade.id == outcome.trade.id
    assert trade.status is TradeStatus.OPEN and trade.symbol == "XRP-USDT"
    assert trade.quantity == D(40)
    assert sorted(f.external_fill_id for f in outcome.trade.fills) == ["c1", "c2"]


async def test_open_position_quantity_mismatch_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    """Вход старше периода: открытый объём по исполнениям ≠ позиции —
    отказ, без сделки с неверной ценой входа."""
    user, repo, _ = ctx
    importer = HistoryImporter(FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO)
    outcome = await importer.import_open_position(
        *WINDOW, symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(55)
    )

    assert outcome.trade is None and "не сходится" in str(outcome.refusal)
    assert await repo.list_recent(user.id) == []


async def test_open_position_without_entries_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, _ = ctx
    outcome = await HistoryImporter(
        FakeExchange(XRP_HISTORY[:6]), repo, user.id, account_mode=DEMO
    ).import_open_position(*WINDOW, symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(40))

    assert outcome.trade is None and "не найдено" in str(outcome.refusal)
    assert await repo.list_recent(user.id) == []


async def test_open_position_already_in_journal_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, session = ctx
    importer = HistoryImporter(FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO)
    args = {"symbol": "XRP-USDT", "side": TradeSide.LONG, "quantity": D(40)}
    first = await importer.import_open_position(*WINDOW, **args)
    await session.flush()
    second = await importer.import_open_position(*WINDOW, **args)

    assert first.trade is not None
    assert second.trade is None and "уже есть в журнале" in str(second.refusal)
    assert len(await repo.list_recent(user.id)) == 1


async def test_history_import_links_open_trade_to_live_position(ctx) -> None:  # type: ignore[no-untyped-def]
    """/import (03.10): незакрытой сделке — positionId живой позиции того же
    символа и стороны с тем же объёмом; закрытым — нет. Объём не сходится —
    не записываем."""
    user, repo, session = ctx
    live = Position(
        symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(40), entry_price=D("1.54"),
        mark_price=D("1.54"), leverage=20, unrealized_pnl=D(0), liquidation_price=None,
        position_id="2106063262781022210",
    )
    exchange = FakeExchange(XRP_HISTORY)
    exchange.get_positions = lambda **_: _async([live])  # type: ignore[method-assign]
    await HistoryImporter(exchange, repo, user.id, account_mode=DEMO).import_period(*WINDOW)

    trades = {t.status: t for t in await repo.list_recent(user.id) if t.symbol == "XRP-USDT"}
    assert trades[TradeStatus.OPEN].external_position_id == "2106063262781022210"
    closed = [t for t in await repo.list_recent(user.id)
              if t.symbol == "XRP-USDT" and t.status is TradeStatus.CLOSED]
    assert closed and all(t.external_position_id is None for t in closed)


async def test_history_import_quantity_mismatch_no_link(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, session = ctx
    live = Position(
        symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(55), entry_price=D("1.54"),
        mark_price=D("1.54"), leverage=20, unrealized_pnl=D(0), liquidation_price=None,
        position_id="p-other",
    )
    exchange = FakeExchange(XRP_HISTORY)
    exchange.get_positions = lambda **_: _async([live])  # type: ignore[method-assign]
    await HistoryImporter(exchange, repo, user.id, account_mode=DEMO).import_period(*WINDOW)
    [open_trade] = [t for t in await repo.list_open(user.id) if t.symbol == "XRP-USDT"]
    assert open_trade.external_position_id is None


async def _async(value):  # type: ignore[no-untyped-def]
    return value


@pytest.mark.parametrize("mode", [ExchangeKeyMode.DEMO, ExchangeKeyMode.LIVE])
async def test_imported_trades_carry_account_mode(ctx, mode) -> None:  # type: ignore[no-untyped-def]
    """03.10.2026: счёт клиента импорта пишется в сделку — лимиты убытка
    считаются по сделкам своего счёта."""
    user, repo, _session = ctx
    exchange = FakeExchange([
        fill("f1", 0, entry=True, price="100000"),
        fill("f2", 60, entry=False, price="102000"),
    ])
    await HistoryImporter(exchange, repo, user.id, account_mode=mode).import_period(
        BASE - timedelta(hours=1), BASE + timedelta(hours=2)
    )
    open_pos = await HistoryImporter(
        FakeExchange(XRP_HISTORY), repo, user.id, account_mode=mode
    ).import_open_position(*WINDOW, symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(40))
    assert open_pos.trade is not None
    assert {t.account_mode for t in await repo.list_recent(user.id)} == {mode}


# --- M4: отсечка журнала (03.10.2026) -------------------------------------------
# Журнал очищен 03.10 20:38 UTC; без отсечки /import за 30 дней вернул бы
# удалённые сделки — дедуп держится на исполнениях в журнале.


async def test_cutoff_drops_fills_before_it_and_reports(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, _session = ctx
    cutoff = BASE + timedelta(minutes=50)                 # после разведки r1–r6
    importer = HistoryImporter(
        FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO,
        journal_cutoff=cutoff, tz_offset=5,
    )
    result = await importer.import_period(*WINDOW)
    # r6 (55 мин) — выход без входа после отсечки, b1 и c1/c2 — после неё.
    assert {t.symbol for t in await repo.list_recent(user.id)} == {"BTC-USDT", "XRP-USDT"}
    xrp_trades = [t for t in await repo.list_recent(user.id) if t.symbol == "XRP-USDT"]
    assert [t.quantity for t in xrp_trades] == [D(40)]      # только текущая позиция
    text = result.render(5)
    assert text.endswith(
        "Журнал ведётся с 02.03.2026 15:50 (UTC+5) — исполнения раньше не импортируются."
    )


async def test_period_entirely_before_cutoff_skips_exchange(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, _session = ctx
    exchange = FakeExchange(XRP_HISTORY)
    result = await HistoryImporter(
        exchange, repo, user.id, account_mode=DEMO, journal_cutoff=WINDOW[1],
    ).import_period(*WINDOW)
    assert exchange.calls == 0
    assert result.all_before_cutoff
    assert result.render(0) == (
        "Весь период раньше начала журнала (02.03.2026 13:00 (UTC)) — импортировать нечего."
    )
    assert await repo.list_recent(user.id) == []


async def test_open_position_from_before_cutoff_refused(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, _session = ctx
    outcome = await HistoryImporter(
        FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO,
        journal_cutoff=BASE + timedelta(minutes=90, seconds=30), tz_offset=5,
    ).import_open_position(*WINDOW, symbol="XRP-USDT", side=TradeSide.LONG, quantity=D(40))
    assert outcome.trade is None
    assert outcome.refusal == (
        "Позиция открыта до начала журнала (02.03.2026 16:30 (UTC+5)) — в журнал не заносится."
    )
    assert await repo.list_recent(user.id) == []


async def test_no_cutoff_imports_everything(ctx) -> None:  # type: ignore[no-untyped-def]
    user, repo, _session = ctx
    result = await HistoryImporter(
        FakeExchange(XRP_HISTORY), repo, user.id, account_mode=DEMO,
    ).import_period(*WINDOW)
    assert result.fills_before_cutoff == 0 and result.cutoff is None
    assert "Журнал ведётся" not in result.render(5)


def test_cutoff_filter_drops_boundary_fills() -> None:
    """Окна идут с перекрытием — исполнение раньше отсечки, попавшее в первое
    окно, отбрасывается поштучно."""
    from app.services.import_service import ImportResult

    importer = HistoryImporter(
        FakeExchange([]), None, 1, account_mode=DEMO,  # type: ignore[arg-type]
        journal_cutoff=BASE + timedelta(minutes=10),
    )
    result = ImportResult()
    kept = importer._drop_before_cutoff(XRP_HISTORY[:4], result)
    assert [f.external_id for f in kept] == ["r3", "r4"]
    assert result.fills_before_cutoff == 2
