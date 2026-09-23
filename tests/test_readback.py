"""Тесты app/execution/readback.py (шаг 15.5.3) — против настоящей БД.

Биржа — фейк, но ответы разбираются настоящими BingXClient._parse_order_fill
и _parse_open_order: проверяется и строгость разбора, и форма.

СИНТЕТИКА ДО 15.5.5, заменить живым снимком: ответ get_order по
исполненному маркет-входу и условные ордера верхнего уровня в openOrders
(во что превращаются вложенные TP/SL после исполнения входа) живьём не
сняты. Форма элемента openOrders — от живого снимка 14.09
(tests/test_bingx_client.py::TestGetOpenOrders, architecture.md), только
тип/stopPrice/сторона — условного ордера.
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
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ExchangeClient,
    ExchangeResponseError,
    ExchangeUnavailableError,
    OpenOrder,
    OrderResult,
)
from app.exchanges.bingx import BingXClient
from app.execution.models import OrderRequest
from app.execution.readback import ConditionalOutcome, find_our_conditional, verify_entry
from app.execution.service import build_entry_order_pending
from app.services.user_service import UserService
from app.trading.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalDirection,
    SignalLevel,
    TradeSide,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
PRICE_PRECISION = 1


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _fill(**overrides: object) -> dict[str, object]:
    """Сырой ответ get_order по исполненному маркет-входу — СИНТЕТИКА ДО
    15.5.5. Разбирается фейком в момент вызова настоящим
    BingXClient._parse_order_fill (None в overrides — поля нет вовсе)."""
    raw: dict[str, object] = {
        "symbol": "BTC-USDT", "orderId": "9001", "clientOrderId": "",
        "side": "BUY", "positionSide": "LONG", "type": "MARKET",
        "status": "FILLED", "origQty": "0.010", "executedQty": "0.010",
        "avgPrice": "100.3", "commission": "-0.0005",
    }
    raw.update(overrides)
    return {k: v for k, v in raw.items() if v is not None}


def _conditional(
    *, order_type: str, stop_price: str, order_id: str, side: str = "SELL",
    position_side: str = "LONG", client_order_id: str = "", created: datetime | None = None,
    symbol: str = "BTC-USDT",
) -> OpenOrder:
    """Элемент openOrders в живой форме 14.09, тип и stopPrice — условного
    ордера (СИНТЕТИКА ДО 15.5.5)."""
    # Время — в момент вызова, не при импорте модуля: вход получает
    # created_at от сервера БД в момент теста, и условник «из прошлого»
    # отсекается проверкой «создан не раньше входа».
    created = created or datetime.now(UTC) + timedelta(seconds=1)
    return BingXClient._parse_open_order({
        "symbol": symbol, "orderId": order_id,
        "side": side, "positionSide": position_side, "type": order_type,
        "origQty": "0.0000", "price": "0.0", "executedQty": "0.0000",
        "avgPrice": "0.0", "status": "NEW", "stopPrice": stop_price,
        "workingType": "MARK_PRICE", "clientOrderId": client_order_id,
        "time": _ms(created), "updateTime": _ms(created),
        "leverage": "10X", "reduceOnly": False, "closePosition": "true",
        "takeProfit": {
            "type": "TAKE_PROFIT", "quantity": 0, "stopPrice": 0,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
        "stopLoss": {
            "type": "STOP", "quantity": 0, "stopPrice": 0,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
    })


class FakeReadbackClient(ExchangeClient):
    """Только то, чем пользуется verify_entry. Очереди ответов: элемент —
    значение или исключение; последний элемент повторяется."""

    name = "fake"

    def __init__(
        self,
        *,
        fills: list[object] | None = None,
        open_orders: list[object] | None = None,
        place_results: list[object] | None = None,
    ) -> None:
        self.fills = fills or []
        self.open_orders = open_orders or []
        self.place_results = place_results or []
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _next(queue: list[object]) -> object:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None):  # type: ignore[no-untyped-def]
        self.calls.append(("get_order_fill", {"cid": client_order_id, "max_retries": max_retries}))
        item = self._next(self.fills)
        return BingXClient._parse_order_fill(item) if isinstance(item, dict) else item

    async def get_open_orders(self, symbol=None, *, max_retries=None):  # type: ignore[no-untyped-def]
        self.calls.append(("get_open_orders", {"symbol": symbol, "max_retries": max_retries}))
        return self._next(self.open_orders)

    async def place_conditional_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("place_conditional_order", kwargs))
        return self._next(self.place_results)

    async def place_market_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("place_market_order", kwargs))
        raise AssertionError("read-back не имеет права отправлять вход повторно")

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    # --- не используется read-back --------------------------------------
    async def get_ticker(self, symbol, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_klines(self, symbol, interval, limit=500, end_time=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_symbols(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_funding_rate(self, symbol):  # type: ignore[no-untyped-def]
        return None

    async def get_balance(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return []

    async def get_api_restrictions(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_fills(self, start_time, end_time, symbol=None):  # type: ignore[no-untyped-def]
        return []

    async def get_leverage(self, symbol, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_position_mode(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def set_leverage(self, symbol, leverage, *, position_side=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_order(self, symbol, client_order_id, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _placed(order_id: str = "7001") -> OrderResult:
    return BingXClient._parse_order({"orderId": order_id, "status": "NEW"})


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user = await UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        ).get_or_create(telegram_id=unique_telegram_id())
        slot = SignalRecord(
            user_id=user.id, symbol="BTC-USDT", timeframe="1h", level=SignalLevel.READY,
            setup="Пробой с ретестом", direction=SignalDirection.LONG, fingerprint="fp",
            entry_low=D("100"), entry_high=D("101"), stop_loss=D("97"), take_profit=D("110"),
            detail="d", expires_at=NOW + timedelta(hours=4),
        )
        session.add(slot)
        await session.flush()
        notification = SignalNotification.snapshot_of(
            slot, notified_at=NOW, expires_at=NOW + timedelta(hours=4)
        )
        session.add(notification)
        await session.flush()
        yield session, user, slot, notification, settings
        await cleanup_user(session, user)
    await db.dispose()


def _order(
    user_id: int, slot_id: int, nid: int, *, side: TradeSide = TradeSide.LONG
) -> OrderRequest:
    long = side is TradeSide.LONG
    return OrderRequest(
        user_id=user_id, signal_id=slot_id, notification_id=nid, symbol="BTC-USDT",
        side=OrderSide.BUY if long else OrderSide.SELL, position_side=side,
        quantity=D("0.010"), entry_price=D("100.1"), leverage=10,
        stop_loss=D("97.0") if long else D("103.0"),
        take_profit=D("110.0") if long else D("90.0"),
        notional=D("1"), margin=D("0.1"), risk_amount=D("0.03"),
        risk_percent=D("1"), risk_reward=D("3"),
    )


async def _entry(  # type: ignore[no-untyped-def]
    session, order: OrderRequest, status: OrderStatus = OrderStatus.SUBMITTED
) -> ExecutionOrder:
    row = build_entry_order_pending(order)
    row.status = status
    row.raw_response = {"orderId": "9001"}
    session.add(row)
    await session.flush()
    await session.refresh(row)  # created_at с сервера
    return row


async def _rows(session, nid: int, role: OrderRole) -> list[ExecutionOrder]:  # type: ignore[no-untyped-def]
    stmt = select(ExecutionOrder).where(
        ExecutionOrder.notification_id == nid, ExecutionOrder.role == role
    )
    return list(await session.scalars(stmt))


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def _verify(session, client, settings, entry, order, sleep=None, side="LONG"):  # type: ignore[no-untyped-def]
    return await verify_entry(
        session=session, client=client, settings=settings, entry_row=entry, order=order,
        position_side=side, price_precision=PRICE_PRECISION, sleep=sleep or _Sleeps(),
    )


def _our_stop(order_id: str = "501", **kw: object) -> OpenOrder:
    return _conditional(order_type="STOP_MARKET", stop_price="97.0", order_id=order_id, **kw)  # type: ignore[arg-type]


def _our_take(order_id: str = "502", **kw: object) -> OpenOrder:
    return _conditional(  # type: ignore[arg-type]
        order_type="TAKE_PROFIT_MARKET", stop_price="110.0", order_id=order_id, **kw
    )


# --- исполнение входа -------------------------------------------------------


async def test_submitted_with_stop_and_take_in_place(ctx) -> None:  # type: ignore[no-untyped-def]
    """SUBMITTED + стоп и тейк на месте → строки S/T со своими orderId,
    вход FILLED, ни одной попытки что-либо выставить."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_fill()], open_orders=[[_our_stop(), _our_take()]])

    result = await _verify(session, client, settings, entry, order)

    assert entry.status is OrderStatus.FILLED
    assert result.fill is not None and result.fill.avg_price == D("100.3")
    assert result.stop.outcome is ConditionalOutcome.FOUND and result.stop.order_id == "501"
    assert result.take.outcome is ConditionalOutcome.FOUND and result.take.order_id == "502"
    assert result.alarm is None
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    [t] = await _rows(session, n.id, OrderRole.TAKE_PROFIT)
    assert (s.exchange_order_id, s.client_order_id, s.status) == (
        "501", f"tj{n.id}u{user.id}S", OrderStatus.SUBMITTED
    )
    assert (t.exchange_order_id, t.client_order_id) == ("502", f"tj{n.id}u{user.id}T")
    assert client.count("place_conditional_order") == 0
    assert client.count("get_open_orders") == 1
    assert all(c[1]["max_retries"] == 1 for c in client.calls if "max_retries" in c[1])


async def test_readback_retries_until_filled(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill(status="NEW", executedQty="0"), _fill()],
        open_orders=[[_our_stop(), _our_take()]],
    )
    sleeps = _Sleeps()

    await _verify(session, client, settings, entry, order, sleeps)

    assert client.count("get_order_fill") == 2
    assert sleeps.calls[0] == settings.exec_order_readback_delay_ms / 1000
    assert entry.status is OrderStatus.FILLED


async def test_missing_avg_price_is_incomplete_not_zero(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет avgPrice → ReadbackIncomplete, а не цена 0: вход остаётся
    SUBMITTED, пользователь видит, какого поля нет, стоп всё равно
    проверяется."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill(avgPrice=None)], open_orders=[[_our_stop(), _our_take()]]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.fill is None
    assert entry.status is OrderStatus.SUBMITTED
    assert any("avgPrice" in w for w in result.warnings)
    assert result.stop.outcome is ConditionalOutcome.FOUND


async def test_partial_fill_warns_and_still_checks_stop(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill(executedQty="0.004")], open_orders=[[_our_stop(), _our_take()]]
    )

    result = await _verify(session, client, settings, entry, order)

    assert any("0.004 из 0.01" in w for w in result.warnings)
    assert result.stop.outcome is ConditionalOutcome.FOUND


# --- спасение стопа и тейка --------------------------------------------------


@pytest.mark.parametrize(
    "side,closing,position_side",
    [(TradeSide.LONG, "SELL", "LONG"), (TradeSide.SHORT, "BUY", "SHORT")],
)
async def test_missing_stop_is_rescued_with_close_position(  # type: ignore[no-untyped-def]
    ctx, side, closing, position_side
) -> None:
    """Стопа нет и после повторного чтения → ровно один отдельный
    STOP_MARKET на закрывающей стороне, по уровню, ушедшему во вход."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id, side=side)
    entry = await _entry(session, order)
    take = _conditional(
        order_type="TAKE_PROFIT_MARKET", stop_price=str(order.take_profit), order_id="502",
        side=closing, position_side=position_side,
    )
    client = FakeReadbackClient(
        fills=[_fill(side=order.side.value, positionSide=position_side)],
        open_orders=[[take], [take]],
        place_results=[_placed("7001")],
    )
    sleeps = _Sleeps()

    result = await _verify(session, client, settings, entry, order, sleeps, side=position_side)

    assert client.count("get_open_orders") == 2  # «нет» — только после перечтения
    assert settings.exec_open_orders_recheck_delay_ms / 1000 in sleeps.calls
    [call] = [c[1] for c in client.calls if c[0] == "place_conditional_order"]
    assert call["order_type"] == "STOP_MARKET"
    assert call["side"].value == closing
    assert call["position_side"] == position_side
    assert call["stop_price"] == order.stop_loss
    assert call["client_order_id"] == f"tj{n.id}u{user.id}S"
    assert result.stop.outcome is ConditionalOutcome.RESCUED
    assert result.alarm is None
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    assert (s.status, s.exchange_order_id, s.order_type) == (
        OrderStatus.SUBMITTED, "7001", OrderType.STOP_MARKET
    )


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (ExchangeResponseError("BingX: отказ (код 80012)", code=80012, payload={"code": 80012}),
         OrderStatus.REJECTED),
        (ExchangeUnavailableError("timeout"), OrderStatus.UNKNOWN),
    ],
)
async def test_failed_stop_rescue_raises_alarm_once(  # type: ignore[no-untyped-def]
    ctx, caplog, error, expected_status
) -> None:
    """Спасение стопа не удалось → «ПОЗИЦИЯ БЕЗ СТОПА», logger.error,
    строка S с исходом попытки, ровно одна попытка (UNKNOWN не
    переотправляется)."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_take()]], place_results=[error]
    )

    with caplog.at_level("ERROR", logger="app.execution.readback"):
        result = await _verify(session, client, settings, entry, order)

    assert client.count("place_conditional_order") == 1
    assert result.stop.outcome is ConditionalOutcome.RESCUE_FAILED
    assert result.alarm == "⚠️ ПОЗИЦИЯ БЕЗ СТОПА: BTC-USDT LONG 0.01 — поставь стоп руками."
    assert "Позиция без стопа" in caplog.text
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    assert s.status is expected_status

    # Повторный вызов (reconciler) — повторной попытки нет.
    await _verify(session, client, settings, entry, order)
    assert client.count("place_conditional_order") == 1


async def test_missing_take_one_attempt_no_alarm(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_stop()]],
        place_results=[ExchangeResponseError("отказ", code=80012, payload=None)],
    )

    result = await _verify(session, client, settings, entry, order)

    [call] = [c[1] for c in client.calls if c[0] == "place_conditional_order"]
    assert call["order_type"] == "TAKE_PROFIT_MARKET"
    assert result.take.outcome is ConditionalOutcome.RESCUE_FAILED
    assert result.alarm is None
    assert "Тейк не выставился — поставь руками, если нужен." in result.warnings


# --- UNKNOWN ------------------------------------------------------------------


async def test_unknown_found_continues_as_submitted(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.UNKNOWN)
    entry.raw_response = None
    client = FakeReadbackClient(
        fills=[_fill(orderId="9555")], open_orders=[[_our_stop(), _our_take()]]
    )
    sleeps = _Sleeps()

    result = await _verify(session, client, settings, entry, order, sleeps)

    assert sleeps.calls[0] == settings.exec_unknown_search_delay_ms / 1000
    assert entry.exchange_order_id == "9555"
    assert entry.status is OrderStatus.FILLED
    assert result.stop.outcome is ConditionalOutcome.FOUND


async def test_unknown_not_found_is_not_resent(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.UNKNOWN)
    client = FakeReadbackClient(
        fills=[ExchangeResponseError("order not exist", code=109414, payload=None)]
    )

    result = await _verify(session, client, settings, entry, order)

    assert entry.status is OrderStatus.UNKNOWN
    assert client.count("get_order_fill") == 1
    assert client.count("place_market_order") == 0
    assert client.count("get_open_orders") == 0
    assert result.stop is None
    assert any("Повторно не отправляю" in w for w in result.warnings)


# --- чужие условники ------------------------------------------------------


@pytest.mark.parametrize(
    "manual",
    [
        pytest.param({"created": NOW - timedelta(hours=1)}, id="создан до входа"),
        pytest.param({"stop_price": "96.5"}, id="другая цена"),
        pytest.param({"client_order_id": "tj999999u1S"}, id="чужой вход бота"),
        pytest.param({"position_side": "SHORT", "side": "BUY"}, id="другая сторона"),
        pytest.param({"symbol": "ETH-USDT"}, id="другой символ"),
    ],
)
async def test_manual_stop_is_not_taken_for_ours(ctx, manual) -> None:  # type: ignore[no-untyped-def]
    """Ручной стоп по тому же символу не принимается за наш — стоп
    спасается отдельно."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    fields = {"order_type": "STOP_MARKET", "stop_price": "97.0", "order_id": "666", **manual}
    foreign = _conditional(**fields)  # type: ignore[arg-type]
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[foreign, _our_take()]], place_results=[_placed()]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.stop.outcome is ConditionalOutcome.RESCUED
    assert client.count("place_conditional_order") == 1


async def test_stop_claimed_by_other_entry_is_not_ours(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    other = SignalNotification.snapshot_of(
        slot, notified_at=NOW, expires_at=NOW + timedelta(hours=4)
    )
    session.add(other)
    await session.flush()
    other_order = _order(user.id, slot.id, other.id)
    other_stop = ExecutionOrder(
        user_id=user.id, signal_id=slot.id, notification_id=other.id,
        client_order_id=other_order.stop_loss_client_order_id, symbol="BTC-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED, exchange_order_id="501",
    )
    session.add(other_stop)
    await session.flush()

    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_stop("501"), _our_take()]], place_results=[_placed()]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.stop.outcome is ConditionalOutcome.RESCUED


async def test_two_matching_stops_are_ambiguous_no_rescue(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_stop("501"), _our_stop("503"), _our_take()]]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.stop.outcome is ConditionalOutcome.AMBIGUOUS
    assert client.count("place_conditional_order") == 0
    assert await _rows(session, n.id, OrderRole.STOP_LOSS) == []
    assert any("Не могу отличить свой стоп" in w for w in result.warnings)


def test_find_our_conditional_matches_rounded_level() -> None:
    """Уровень сравнивается до шага цены: 97.04 ≈ 97.0 при precision 1."""
    stop = _conditional(order_type="STOP_MARKET", stop_price="97.04", order_id="1")
    match = find_our_conditional(
        [stop], symbol="BTC-USDT", order_type=OrderType.STOP_MARKET, position_side="LONG",
        closing_side=OrderSide.SELL, level=D("97.0"), price_precision=1,
        placed_after=NOW, claimed_order_ids=set(), own_client_order_id="tj5u1S",
    )
    assert match.order is stop


# --- ошибки и идемпотентность ------------------------------------------------


async def test_open_orders_failure_is_unverified_alarm_without_rescue(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_fill()], open_orders=[ExchangeUnavailableError("down")])

    result = await _verify(session, client, settings, entry, order)

    assert client.count("place_conditional_order") == 0
    assert result.stop.outcome is ConditionalOutcome.UNVERIFIED
    assert result.alarm == "⚠️ СТОП НЕ ПОДТВЕРЖДЁН: BTC-USDT LONG 0.01 — проверь позицию в BingX."
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    assert (s.status, s.error_code, s.client_order_id) == (
        OrderStatus.ERROR, "STOP_UNVERIFIED", None
    )


async def test_repeat_call_is_idempotent(ctx) -> None:  # type: ignore[no-untyped-def]
    """Повторный verify_entry (reconciler 15.6): исполнение не читается
    заново, строки не дублируются, ничего не выставляется."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_take()], [_our_take()]], place_results=[_placed()]
    )
    await _verify(session, client, settings, entry, order)
    calls_before = len(client.calls)

    result = await _verify(session, client, settings, entry, order)

    assert len(client.calls) == calls_before
    assert result.stop.outcome is ConditionalOutcome.RESCUED
    assert len(await _rows(session, n.id, OrderRole.STOP_LOSS)) == 1
    assert len(await _rows(session, n.id, OrderRole.TAKE_PROFIT)) == 1


async def test_pending_rescue_row_is_not_resent(ctx) -> None:  # type: ignore[no-untyped-def]
    """Строка спасения в PENDING (процесс упал между commit и ответом) — не
    отправляется повторно."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.FILLED)
    for role, cid in (
        (OrderRole.STOP_LOSS, order.stop_loss_client_order_id),
        (OrderRole.TAKE_PROFIT, order.take_profit_client_order_id),
    ):
        session.add(ExecutionOrder(
            user_id=user.id, signal_id=slot.id, notification_id=n.id, client_order_id=cid,
            symbol="BTC-USDT", side=OrderSide.SELL, position_side=TradeSide.LONG,
            order_type=OrderType.STOP_MARKET, role=role, status=OrderStatus.PENDING,
        ))
    await session.flush()
    client = FakeReadbackClient()

    result = await _verify(session, client, settings, entry, order)

    assert client.calls == []
    assert result.stop.outcome is ConditionalOutcome.PENDING


async def test_rejected_entry_is_not_read_back(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.REJECTED)
    client = FakeReadbackClient()

    result = await _verify(session, client, settings, entry, order)

    assert client.calls == []
    assert result.stop is None
