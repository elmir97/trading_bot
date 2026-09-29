"""Тесты app/execution/readback.py (шаг 15.5.3) — против настоящей БД.

Биржа — фейк, но ответы разбираются настоящими BingXClient._parse_order_fill
и _parse_open_order: проверяется и строгость разбора, и форма.

Сценарий — живой LINK #3 (демо 27.09, tests/fixtures/bingx_demo_20260927.json):
вход 2037.8 по 14.400 (#37), стоп 13.526 (…705) и тейк 16.263 (…704) — отдельные
условники в openOrders, clientOrderId у них пустой. Ответы по умолчанию — живые
без изменений. Сценарии, которых живьём нет (частичное исполнение, ручной стоп в
openOrders, SHORT, отказ спасения), — синтетика ИЗ живого ответа заменой полей,
помечена в тесте: «СИНТЕТИКА ИЗ ЖИВОГО <что>, заменены: <поля>».
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
from tests.bingx_fixtures import live_items
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
PRICE_PRECISION = 3  # LINK-USDT: 14.400, 13.526


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# Живой LINK #3: вход ушёл в 08:15:32 (time входа — целые секунды), условники
# созданы через 908 мс. Строка входа в БД получает это время явно — иначе
# живые условники «созданы до входа» и отсекаются.
LIVE_ENTRY_PLACED = datetime(2026, 9, 27, 8, 15, 32, tzinfo=UTC)
LINK_ENTRY, LINK_STOP, LINK_TAKE = (
    "2104122757776154624", "2104122758140616705", "2104122758140616704",
)


def _fill(**overrides: object) -> dict[str, object]:
    """Живой ответ get_order по исполненному входу LINK (#37); overrides —
    синтетика из живого (None — поля нет вовсе). Разбирается фейком в момент
    вызова настоящим BingXClient._parse_order_fill."""
    [raw] = live_items("order #37 LINK-USDT ENTRY")
    raw.update(overrides)
    return {k: v for k, v in raw.items() if v is not None}


def _live_open_orders() -> list[OpenOrder]:
    """Живой openOrders LINK: [стоп …705, тейк …704]."""
    return [BingXClient._parse_open_order(item) for item in live_items("openOrders LINK")]


def _conditional(
    *, order_type: str, stop_price: str, order_id: str, side: str = "SELL",
    position_side: str = "LONG", client_order_id: str = "",
    created: datetime | None = None, symbol: str = "LINK-USDT",
) -> OpenOrder:
    """СИНТЕТИКА ИЗ ЖИВОГО стопа …705 (openOrders LINK), заменены: тип,
    stopPrice, orderId, стороны, clientOrderId, time, символ."""
    [raw, _take] = live_items("openOrders LINK")
    moment = int((created or LIVE_ENTRY_PLACED + timedelta(milliseconds=908)).timestamp() * 1000)
    raw.update({
        "type": order_type, "stopPrice": stop_price, "orderId": int(order_id), "side": side,
        "positionSide": position_side, "clientOrderId": client_order_id,
        "time": moment, "updateTime": moment, "symbol": symbol,
    })
    return BingXClient._parse_open_order(raw)


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

    async def get_balance(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_positions(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        # Проверка ликвидации: liquidationPrice живьём не снят (значение вне
        # allowlist разведки 27.09) — позиций нет, только предупреждение.
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

    async def close(self) -> None:
        pass


def _placed(order_id: str = "7001") -> OrderResult:
    """СИНТЕТИКА: ответ POST условного ордера живьём не снят."""
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
            user_id=user.id, symbol="LINK-USDT", timeframe="1h", level=SignalLevel.READY,
            setup="Пробой с ретестом", direction=SignalDirection.LONG, fingerprint="fp",
            entry_low=D("14.35"), entry_high=D("14.43"), stop_loss=D("13.526"),
            take_profit=D("16.263"),
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
        user_id=user_id, signal_id=slot_id, notification_id=nid, symbol="LINK-USDT",
        side=OrderSide.BUY if long else OrderSide.SELL, position_side=side,
        quantity=D("2037.8"), entry_price=D("14.398"), leverage=10,
        # SHORT — зеркало живого LONG (синтетика): стоп выше, тейк ниже.
        stop_loss=D("13.526") if long else D("15.270"),
        take_profit=D("16.263") if long else D("12.533"),
        notional=D("29340.24"), margin=D("2934.02"), risk_amount=D("1776.96"),
        risk_percent=D("1"), risk_reward=D("2.19"),
    )


async def _entry(  # type: ignore[no-untyped-def]
    session, order: OrderRequest, status: OrderStatus = OrderStatus.SUBMITTED
) -> ExecutionOrder:
    row = build_entry_order_pending(order)
    row.status = status
    row.raw_response = {"orderId": LINK_ENTRY}
    row.created_at = LIVE_ENTRY_PLACED
    session.add(row)
    await session.flush()
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


def _our_stop() -> OpenOrder:
    """Живой стоп …705 из openOrders LINK, без изменений."""
    return _live_open_orders()[0]


def _our_take() -> OpenOrder:
    """Живой тейк …704 из openOrders LINK, без изменений."""
    return _live_open_orders()[1]


# --- исполнение входа -------------------------------------------------------


async def test_submitted_with_stop_and_take_in_place(ctx) -> None:  # type: ignore[no-untyped-def]
    """SUBMITTED + стоп и тейк на месте → строки S/T со своими orderId,
    вход FILLED, ни одной попытки что-либо выставить. Всё живое: GET #37 и
    openOrders LINK без изменений."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_fill()], open_orders=[_live_open_orders()])

    result = await _verify(session, client, settings, entry, order)

    assert entry.status is OrderStatus.FILLED
    assert result.fill is not None
    assert (result.fill.avg_price, result.fill.executed_qty) == (D("14.400"), D("2037.8"))
    assert result.stop.outcome is ConditionalOutcome.FOUND and result.stop.order_id == LINK_STOP
    assert result.take.outcome is ConditionalOutcome.FOUND and result.take.order_id == LINK_TAKE
    assert result.alarm is None
    assert entry.exchange_order_id == LINK_ENTRY
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    [t] = await _rows(session, n.id, OrderRole.TAKE_PROFIT)
    assert (s.exchange_order_id, s.client_order_id, s.status) == (
        LINK_STOP, f"tj{n.id}u{user.id}S", OrderStatus.SUBMITTED
    )
    assert (t.exchange_order_id, t.client_order_id) == (LINK_TAKE, f"tj{n.id}u{user.id}T")
    assert client.count("place_conditional_order") == 0
    assert client.count("get_open_orders") == 1
    assert all(c[1]["max_retries"] == 1 for c in client.calls if "max_retries" in c[1])


def _live_new() -> dict[str, object]:
    """Живой ответ GET по ордеру в статусе NEW (#38, демо 27.09): commission
    и profit — пустые строки, avgPrice "0.000". Живого NEW маркет-входа нет —
    это ближайшая живая форма."""
    [raw] = live_items("order #38 LINK-USDT STOP_LOSS")
    return raw


async def test_live_new_form_is_retried_until_filled(ctx) -> None:  # type: ignore[no-untyped-def]
    """Р2: вход ещё NEW в живой форме (пустая commission) — read-back ждёт
    FILLED своим циклом повторов, а не обрывается на «нет поля commission»."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_live_new(), _fill()], open_orders=[[_our_stop(), _our_take()]]
    )
    sleeps = _Sleeps()

    result = await _verify(session, client, settings, entry, order, sleeps)

    assert client.count("get_order_fill") == 2
    assert sleeps.calls[0] == settings.exec_order_readback_delay_ms / 1000
    assert entry.status is OrderStatus.FILLED
    assert result.fill is not None and result.fill.avg_price == D("14.400")
    assert not any("commission" in w for w in result.warnings)


async def test_live_new_form_never_filled_is_unconfirmed(ctx) -> None:  # type: ignore[no-untyped-def]
    """Р2: вход так и не стал FILLED — все попытки, вход SUBMITTED, исполнения
    нет (не «исполнение 0»), предупреждение со статусом биржи."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_live_new()], open_orders=[[_our_stop(), _our_take()]])

    result = await _verify(session, client, settings, entry, order)

    assert client.count("get_order_fill") == settings.exec_order_readback_attempts
    assert entry.status is OrderStatus.SUBMITTED
    assert result.fill is None
    assert any("статус NEW" in w for w in result.warnings)


async def test_missing_avg_price_is_incomplete_not_zero(ctx) -> None:  # type: ignore[no-untyped-def]
    """Нет avgPrice → ReadbackIncomplete, а не цена 0: вход остаётся
    SUBMITTED, пользователь видит, какого поля нет, стоп всё равно
    проверяется. СИНТЕТИКА ИЗ ЖИВОГО #37, убран avgPrice."""
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
    """СИНТЕТИКА ИЗ ЖИВОГО #37, заменён executedQty. Статус частичного
    исполнения живьём не снят — status FILLED, как у живого."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill(executedQty="1000.0")], open_orders=[[_our_stop(), _our_take()]]
    )

    result = await _verify(session, client, settings, entry, order)

    assert any("1000 из 2037.8" in w for w in result.warnings)
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
    STOP_MARKET на закрывающей стороне, по уровню, ушедшему во вход.
    LONG: живой тейк …704 без стопа рядом (живой список минус стоп); SHORT —
    СИНТЕТИКА ИЗ ЖИВОГО тейка, заменены стороны и уровень."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id, side=side)
    entry = await _entry(session, order)
    take = _our_take() if side is TradeSide.LONG else _conditional(
        order_type="TAKE_PROFIT_MARKET", stop_price=str(order.take_profit), order_id=LINK_TAKE,
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
    assert result.alarm == "⚠️ ПОЗИЦИЯ БЕЗ СТОПА: LINK-USDT LONG 2037.8 — поставь стоп руками."
    assert "Позиция без стопа" in caplog.text
    [s] = await _rows(session, n.id, OrderRole.STOP_LOSS)
    assert s.status is expected_status

    # Повторный вызов (reconciler) — повторной попытки нет.
    await _verify(session, client, settings, entry, order)
    assert client.count("place_conditional_order") == 1


async def test_both_missing_live_empty_open_orders_rescues_both(ctx) -> None:  # type: ignore[no-untyped-def]
    """openOrders LINK — живой пустой ответ (демо 29.09): спасаются оба,
    стоп и тейк, по одной попытке."""
    from tests.bingx_fixtures import LINK_MANUAL_STOP

    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    empty = [
        BingXClient._parse_open_order(item)
        for item in live_items("openOrders LINK", LINK_MANUAL_STOP)
    ]
    assert empty == []
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[empty], place_results=[_placed("7001"), _placed("7002")]
    )

    result = await _verify(session, client, settings, entry, order)

    types = [c[1]["order_type"] for c in client.calls if c[0] == "place_conditional_order"]
    assert types == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert result.stop.outcome is ConditionalOutcome.RESCUED
    assert result.take.outcome is ConditionalOutcome.RESCUED
    assert result.alarm is None


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
    entry.exchange_order_id = None
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_stop(), _our_take()]]
    )
    sleeps = _Sleeps()

    result = await _verify(session, client, settings, entry, order, sleeps)

    assert sleeps.calls[0] == settings.exec_unknown_search_delay_ms / 1000
    assert entry.exchange_order_id == LINK_ENTRY
    assert entry.status is OrderStatus.FILLED
    assert result.stop.outcome is ConditionalOutcome.FOUND


async def test_unknown_not_found_is_not_resent(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order, OrderStatus.UNKNOWN)
    client = FakeReadbackClient(
        # Код и msg — живые (GET по несуществующему clientOrderID, демо 29.09).
        fills=[ExchangeResponseError("order not exist", code=109421, payload=None)]
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
        pytest.param({"created": LIVE_ENTRY_PLACED - timedelta(hours=1)}, id="создан до входа"),
        pytest.param({"stop_price": "13.4"}, id="другая цена"),
        pytest.param({"client_order_id": "tj999999u1S"}, id="чужой вход бота"),
        pytest.param({"position_side": "SHORT", "side": "BUY"}, id="другая сторона"),
        pytest.param({"symbol": "ETH-USDT"}, id="другой символ"),
    ],
)
async def test_manual_stop_is_not_taken_for_ours(ctx, manual) -> None:  # type: ignore[no-untyped-def]
    """Ручной стоп по тому же символу не принимается за наш — стоп
    спасается отдельно. Ручной условник в openOrders живьём не снят (29.09 он
    уже сработал) — СИНТЕТИКА ИЗ ЖИВОГО стопа …705."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    fields = {"order_type": "STOP_MARKET", "stop_price": "13.526", "order_id": "666", **manual}
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
        client_order_id=other_order.stop_loss_client_order_id, symbol="LINK-USDT",
        side=OrderSide.SELL, position_side=TradeSide.LONG, order_type=OrderType.STOP_MARKET,
        role=OrderRole.STOP_LOSS, status=OrderStatus.SUBMITTED, exchange_order_id=LINK_STOP,
    )
    session.add(other_stop)
    await session.flush()

    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[_live_open_orders()], place_results=[_placed()]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.stop.outcome is ConditionalOutcome.RESCUED


async def test_two_matching_stops_are_ambiguous_no_rescue(ctx) -> None:  # type: ignore[no-untyped-def]
    """Живой стоп …705 и его копия с другим orderId (синтетика из живого)."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[
            _our_stop(),
            _conditional(order_type="STOP_MARKET", stop_price="13.526", order_id="503"),
            _our_take(),
        ]]
    )

    result = await _verify(session, client, settings, entry, order)

    assert result.stop.outcome is ConditionalOutcome.AMBIGUOUS
    assert client.count("place_conditional_order") == 0
    assert await _rows(session, n.id, OrderRole.STOP_LOSS) == []
    assert any("Не могу отличить свой стоп" in w for w in result.warnings)


def test_find_our_conditional_matches_rounded_level() -> None:
    """Уровень сравнивается до шага цены: 13.5264 ≈ 13.526 при precision 3
    (СИНТЕТИКА ИЗ ЖИВОГО стопа, заменён stopPrice)."""
    stop = _conditional(order_type="STOP_MARKET", stop_price="13.5264", order_id="1")
    match = find_our_conditional(
        [stop], symbol="LINK-USDT", order_type=OrderType.STOP_MARKET, position_side="LONG",
        closing_side=OrderSide.SELL, level=D("13.526"), price_precision=PRICE_PRECISION,
        placed_after=LIVE_ENTRY_PLACED, claimed_order_ids=set(), own_client_order_id="tj5u1S",
    )
    assert match.order is stop


def test_find_our_conditional_live_lowercase_own_client_order_id() -> None:
    """Р1: условник с нашим cid в живом регистре биржи (строчными: tj…s при
    tj…S в БД) — наш."""
    stop = _conditional(
        order_type="STOP_MARKET", stop_price="13.526", order_id="1", client_order_id="tj5u1s"
    )
    match = find_our_conditional(
        [stop], symbol="LINK-USDT", order_type=OrderType.STOP_MARKET, position_side="LONG",
        closing_side=OrderSide.SELL, level=D("13.526"), price_precision=PRICE_PRECISION,
        placed_after=LIVE_ENTRY_PLACED, claimed_order_ids=set(), own_client_order_id="tj5u1S",
    )
    assert match.order is stop


def test_find_our_conditional_foreign_bot_cid_any_case_is_not_ours() -> None:
    """Р1, СИНТЕТИКА ИЗ ЖИВОГО (заменён регистр): cid другого входа бота
    заглавными — всё равно чужой, сравнение без учёта регистра."""
    foreign = _conditional(
        order_type="STOP_MARKET", stop_price="13.526", order_id="2",
        client_order_id="TJ999999U1S",
    )
    match = find_our_conditional(
        [foreign], symbol="LINK-USDT", order_type=OrderType.STOP_MARKET, position_side="LONG",
        closing_side=OrderSide.SELL, level=D("13.526"), price_precision=PRICE_PRECISION,
        placed_after=LIVE_ENTRY_PLACED, claimed_order_ids=set(), own_client_order_id="tj5u1S",
    )
    assert match.order is None


# --- ошибки и идемпотентность ------------------------------------------------


async def test_open_orders_failure_is_unverified_alarm_without_rescue(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_fill()], open_orders=[ExchangeUnavailableError("down")])

    result = await _verify(session, client, settings, entry, order)

    assert client.count("place_conditional_order") == 0
    assert result.stop.outcome is ConditionalOutcome.UNVERIFIED
    assert result.alarm == (
        "⚠️ СТОП НЕ ПОДТВЕРЖДЁН: LINK-USDT LONG 2037.8 — проверь позицию в BingX."
    )
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
            symbol="LINK-USDT", side=OrderSide.SELL, position_side=TradeSide.LONG,
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


# --- 28.09: тревога read-back несёт вид события reconciliation_events --------------
# Вид берётся по имени внутри теста: на коде до 28.09 его нет, падают только эти.


async def test_failed_rescue_alarm_has_event_kind(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[[_our_take()]],
        place_results=[ExchangeUnavailableError("timeout")],
    )

    result = await _verify(session, client, settings, entry, order)

    [alarm] = result.alarms
    assert alarm.kind.value == "STOP_RESCUE_FAILED"
    assert alarm.text == "⚠️ ПОЗИЦИЯ БЕЗ СТОПА: LINK-USDT LONG 2037.8 — поставь стоп руками."


async def test_unverified_stop_alarm_has_event_kind(ctx) -> None:  # type: ignore[no-untyped-def]
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    client = FakeReadbackClient(fills=[_fill()], open_orders=[ExchangeUnavailableError("down")])

    result = await _verify(session, client, settings, entry, order)

    [alarm] = result.alarms
    assert alarm.kind.value == "STOP_UNVERIFIED"
    assert alarm.text.startswith("⚠️ СТОП НЕ ПОДТВЕРЖДЁН: LINK-USDT LONG 2037.8")


def _as_client_returns(raws: list[dict[str, object]]) -> list[OpenOrder] | ExchangeResponseError:
    """openOrders так, как его отдал бы настоящий клиент: разбор в момент
    чтения, ошибка разбора — исключение из get_open_orders."""
    try:
        return [BingXClient._parse_open_order(item) for item in raws]
    except ExchangeResponseError as exc:
        return exc


@pytest.mark.parametrize("field", ["time", "type", "symbol"])
async def test_open_order_without_matching_field_is_unverified_not_rescued(  # type: ignore[no-untyped-def]
    ctx, field: str
) -> None:
    """Разведка 29.09: у стопа в openOrders нет поля, по которому read-back
    ищет свой условник. Раньше time → 1970 («поставлен до входа»), type и
    symbol → "" — свой стоп не находился, и read-back выставлял второй. Теперь
    чтение openOrders — ошибка разбора: стоп не подтверждён, ничего не
    выставлено. СИНТЕТИКА ИЗ ЖИВОГО openOrders LINK, у стопа …705 убрано поле."""
    session, user, slot, n, settings = ctx
    order = _order(user.id, slot.id, n.id)
    entry = await _entry(session, order)
    raws = live_items("openOrders LINK")
    del raws[0][field]
    client = FakeReadbackClient(
        fills=[_fill()], open_orders=[_as_client_returns(raws)], place_results=[_placed()]
    )

    result = await _verify(session, client, settings, entry, order)

    assert client.count("place_conditional_order") == 0
    assert result.stop.outcome is ConditionalOutcome.UNVERIFIED
