"""Тесты реальной отправки ордера (раздел 16 ТЗ, шаг 15.5.2).

Против настоящей БД, как test_execution_service.py — биржа замокана
полностью (FakeExchangeClient). Весь API этого файла (ExecutionService.
adjust_leverage/submit_entry_order, build_entry_order_pending,
bingx_position_side) новый — до этого шага не существовал вовсе, значит
каждый тест здесь по построению падал бы на любом более раннем коммите
(AttributeError/ImportError), а не только логически. Терминология: ни
один тест этого файла не "замок" — замок проходит на обеих ревизиях
(см. handoff-2026-09-22.md), а этот код на прошлой ревизии не существовал.
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
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ApiRestrictions,
    Balance,
    ExchangeClient,
    ExchangeResponseError,
    ExchangeUnavailableError,
    Fill,
    LeverageInfo,
    OrderResult,
    Position,
    SymbolInfo,
)
from app.exchanges.bingx import bingx_position_side
from app.execution.models import ExecutionRefusal, OrderRequest
from app.execution.models import ExecutionRefusalCode as Code
from app.execution.service import ExecutionService, build_entry_order_pending
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.user_service import UserService
from app.trading.enums import (
    OrderSide,
    OrderStatus,
    SignalDirection,
    SignalLevel,
    TradeSide,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)


class FakeSubmitClient(ExchangeClient):
    """Реализует только то, чем пользуются adjust_leverage/submit_entry_order."""

    name = "fake"

    def __init__(
        self,
        *,
        current_leverage: LeverageInfo | None = None,
        leverage_error: Exception | None = None,
        set_leverage_error: Exception | None = None,
        place_order_result: OrderResult | None = None,
        place_order_error: Exception | None = None,
    ) -> None:
        self.current_leverage = current_leverage
        self.leverage_error = leverage_error
        self.set_leverage_error = set_leverage_error
        self.place_order_result = place_order_result
        self.place_order_error = place_order_error
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def get_ticker(self, symbol, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_klines(self, symbol, interval, limit=500, end_time=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_symbols(self, *, max_retries=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_funding_rate(self, symbol):  # type: ignore[no-untyped-def]
        return None

    async def get_balance(self, *, max_retries=None) -> Balance:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        return []

    async def get_api_restrictions(self) -> ApiRestrictions:
        raise NotImplementedError

    async def get_fills(self, start_time, end_time, symbol=None) -> list[Fill]:
        return []

    async def get_leverage(self, symbol, *, max_retries=None) -> LeverageInfo:
        self.calls.append(("get_leverage", {"symbol": symbol, "max_retries": max_retries}))
        if self.leverage_error is not None:
            raise self.leverage_error
        return self.current_leverage

    async def get_position_mode(self, *, max_retries=None) -> bool:
        raise NotImplementedError

    async def set_leverage(self, symbol, leverage, *, position_side=None) -> int:
        self.calls.append((
            "set_leverage",
            {"symbol": symbol, "leverage": leverage, "position_side": position_side},
        ))
        if self.set_leverage_error is not None:
            raise self.set_leverage_error
        return leverage

    async def place_market_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("place_market_order", kwargs))
        if self.place_order_error is not None:
            raise self.place_order_error
        assert self.place_order_result is not None, "тест не задал place_order_result"
        return self.place_order_result

    async def get_order(self, symbol, client_order_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def get_open_orders(self, symbol=None) -> list:
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
        "fingerprint": "fp-submit",
        "detail": "detail",
        "entry_low": D("100"),
        "entry_high": D("101"),
        "stop_loss": D("97"),
        "take_profit": D("110"),
        "expires_at": NOW + timedelta(hours=4),
    }
    fields.update(overrides)
    return SignalRecord(**fields)  # type: ignore[arg-type]


def _order(user_id: int, signal_id: int, **overrides: object) -> OrderRequest:
    fields: dict[str, object] = {
        "user_id": user_id,
        "signal_id": signal_id,
        "symbol": "BTC-USDT",
        "side": OrderSide.BUY,
        "position_side": TradeSide.LONG,
        "quantity": D("0.01"),
        "entry_price": D("100"),
        "leverage": 10,
        "stop_loss": D("97"),
        "take_profit": D("110"),
        "notional": D("1000"),
        "margin": D("100"),
        "risk_amount": D("10"),
        "risk_percent": D("1"),
        "risk_reward": D("3"),
    }
    fields.update(overrides)
    return OrderRequest(**fields)  # type: ignore[arg-type]


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
        signal = _signal(user.id)
        session.add(signal)
        await session.flush()
        yield session, user, signal, settings
        await cleanup_user(session, user)
    await db.dispose()


def _service(session, settings: Settings, client) -> ExecutionService:
    market = MarketDataService(client, TTLCache())
    return ExecutionService(session=session, settings=settings, client=client, market=market)


class TestBingxPositionSide:
    def test_hedge_long(self) -> None:
        assert bingx_position_side(TradeSide.LONG, True) == "LONG"

    def test_hedge_short(self) -> None:
        assert bingx_position_side(TradeSide.SHORT, True) == "SHORT"

    def test_one_way_is_always_both(self) -> None:
        assert bingx_position_side(TradeSide.LONG, False) == "BOTH"
        assert bingx_position_side(TradeSide.SHORT, False) == "BOTH"


class TestAdjustLeverage:
    async def test_matching_leverage_does_not_call_set_leverage(self, ctx) -> None:  # type: ignore[no-untyped-def]
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id, leverage=10)
        client = FakeSubmitClient(
            current_leverage=LeverageInfo(
                symbol="BTC-USDT", long_leverage=10, short_leverage=5,
                max_long_leverage=125, max_short_leverage=125,
            )
        )
        service = _service(session, settings, client)

        result = await service.adjust_leverage(order=order, position_side="LONG")

        assert result is None
        names = [c[0] for c in client.calls]
        assert names == ["get_leverage"]

    async def test_mismatched_leverage_calls_set_leverage_with_hedge_side(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """Раздел 16 ТЗ, шаг 15.5.2, п.6 плана: set_leverage вызывается с
        position_side="LONG"/"SHORT" по стороне сделки, никогда с None и
        не с "BOTH" при hedge — position_side здесь приходит от
        bingx_position_side(), проверяем, что adjust_leverage прокидывает
        его как есть, не подставляя свой дефолт."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id, leverage=20, position_side=TradeSide.SHORT)
        client = FakeSubmitClient(
            current_leverage=LeverageInfo(
                symbol="BTC-USDT", long_leverage=10, short_leverage=5,
                max_long_leverage=125, max_short_leverage=125,
            )
        )
        service = _service(session, settings, client)
        position_side = bingx_position_side(TradeSide.SHORT, True)

        result = await service.adjust_leverage(order=order, position_side=position_side)

        assert result is None
        names = [c[0] for c in client.calls]
        assert names == ["get_leverage", "set_leverage"]
        set_call = client.calls[1][1]
        assert set_call["position_side"] == "SHORT"
        assert set_call["position_side"] is not None
        assert set_call["position_side"] != "BOTH"
        assert set_call["leverage"] == 20

    async def test_get_leverage_failure_refuses_without_set_leverage(self, ctx) -> None:  # type: ignore[no-untyped-def]
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        client = FakeSubmitClient(
            current_leverage=LeverageInfo(
                symbol="BTC-USDT", long_leverage=1, short_leverage=1,
                max_long_leverage=125, max_short_leverage=125,
            ),
            leverage_error=ExchangeUnavailableError("BingX не ответил"),
        )
        service = _service(session, settings, client)

        result = await service.adjust_leverage(order=order, position_side="LONG")

        assert isinstance(result, ExecutionRefusal)
        assert result.code is Code.LEVERAGE_FAILED
        names = [c[0] for c in client.calls]
        assert "set_leverage" not in names

        rows = list(
            await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id)
            )
        )
        assert len(rows) == 1
        assert rows[0].status is OrderStatus.REFUSED
        assert rows[0].error_code == Code.LEVERAGE_FAILED.value
        assert rows[0].client_order_id is None
        # Сырой текст ошибки биржи не должен попасть в текст пользователю.
        assert "BingX не ответил" not in rows[0].error_message

    async def test_set_leverage_failure_refuses(self, ctx) -> None:  # type: ignore[no-untyped-def]
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id, leverage=20)
        client = FakeSubmitClient(
            current_leverage=LeverageInfo(
                symbol="BTC-USDT", long_leverage=10, short_leverage=10,
                max_long_leverage=125, max_short_leverage=125,
            ),
            set_leverage_error=ExchangeUnavailableError("таймаут"),
        )
        service = _service(session, settings, client)

        result = await service.adjust_leverage(order=order, position_side="LONG")

        assert isinstance(result, ExecutionRefusal)
        assert result.code is Code.LEVERAGE_FAILED

        rows = list(
            await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id)
            )
        )
        assert len(rows) == 1
        assert rows[0].status is OrderStatus.REFUSED
        assert rows[0].error_code == Code.LEVERAGE_FAILED.value


class TestBuildEntryOrderPending:
    async def test_status_pending_no_sl_tp_rows(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """Раздел 8 ТЗ / раздел 16 ТЗ, шаг 15.5.2: одна ENTRY-строка,
        PENDING — SL/TP execution_orders-строк до read-back (15.5.3) нет."""
        session, user, signal, _settings = ctx
        order = _order(user.id, signal.id)

        row = build_entry_order_pending(order)
        session.add(row)
        await session.flush()

        assert row.status is OrderStatus.PENDING
        assert row.client_order_id == order.entry_client_order_id
        assert row.role.value == "ENTRY"

        rows = list(
            await session.scalars(
                select(ExecutionOrder).where(ExecutionOrder.signal_id == signal.id)
            )
        )
        assert len(rows) == 1


def _order_result(**overrides: object) -> OrderResult:
    fields: dict[str, object] = {
        "order_id": "9001",
        "client_order_id": "tj1u1E",
        "symbol": "BTC-USDT",
        "side": "BUY",
        "position_side": "LONG",
        "order_type": "MARKET",
        "status": "FILLED",
        "price": D("0"),
        "avg_price": D("100.1"),
        "quantity": D("0.01"),
        "executed_qty": D("0.01"),
        "fee": D("0.05"),
        "raw": {"orderId": "9001", "status": "FILLED"},
    }
    fields.update(overrides)
    return OrderResult(**fields)  # type: ignore[arg-type]


class TestSubmitEntryOrder:
    """Раздел 16 ТЗ, шаг 15.5.2, п.1 плана: REJECTED только если
    exc.code is not None и exc.code != 0 — биржа явно отказала. Всё
    остальное (ExchangeUnavailableError, ExchangeResponseError с
    code=None) — UNKNOWN: при code=0 и неразобранном ответе ордер мог
    пройти, REJECTED сказал бы пользователю обратное."""

    async def test_success_marks_submitted(self, ctx) -> None:  # type: ignore[no-untyped-def]
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(
            place_order_result=_order_result(order_id="777", raw={"orderId": "777"})
        )
        service = _service(session, settings, client)

        updated = await service.submit_entry_order(
            order=order, position_side="LONG", entry_row=entry_row
        )

        assert updated.status is OrderStatus.SUBMITTED
        assert updated.exchange_order_id == "777"
        assert updated.raw_response == {"orderId": "777"}

    async def test_explicit_rejection_code_marks_rejected(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """Биржа ответила (HTTP 200, JSON с кодом) и явно отказала — ответ
        определённо получен, ордер определённо не прошёл."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        payload = {"code": 80001, "msg": "insufficient margin"}
        client = FakeSubmitClient(
            place_order_error=ExchangeResponseError(
                "BingX: insufficient margin (код 80001)", code=80001, payload=payload
            )
        )
        service = _service(session, settings, client)

        updated = await service.submit_entry_order(
            order=order, position_side="LONG", entry_row=entry_row
        )

        assert updated.status is OrderStatus.REJECTED
        assert updated.error_code == "80001"
        assert updated.raw_response == payload
        # Сырой msg биржи не должен попасть в error_message.
        assert updated.error_message is None or "insufficient margin" not in (
            updated.error_message or ""
        )

    async def test_timeout_marks_unknown_not_rejected(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """Ответа не было вовсе — ордер мог пройти, REJECTED здесь был бы
        ложью пользователю."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(
            place_order_error=ExchangeUnavailableError("BingX не ответил вовремя")
        )
        service = _service(session, settings, client)

        updated = await service.submit_entry_order(
            order=order, position_side="LONG", entry_row=entry_row
        )

        assert updated.status is OrderStatus.UNKNOWN
        assert updated.error_code == "ExchangeUnavailableError"

    async def test_code_zero_with_unparsed_field_marks_unknown_not_rejected(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """Раздел 16 ТЗ, шаг 15.5.2, п.1 плана — сценарий, явно названный
        в правках: code=0 (успех по коду), но что-то дальше не
        разобралось (типично — BingXClient._parse_order()/_to_decimal
        на кривом поле). exc.code=None в этом случае, не 0 и не то же
        самое, что явный отказ — обязана быть UNKNOWN, не REJECTED."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(
            place_order_error=ExchangeResponseError(
                "Не удалось разобрать поле avgPrice: 'oops'", code=None, payload=None
            )
        )
        service = _service(session, settings, client)

        updated = await service.submit_entry_order(
            order=order, position_side="LONG", entry_row=entry_row
        )

        assert updated.status is OrderStatus.UNKNOWN
        assert updated.error_code == "ExchangeResponseError"

    async def test_unexpected_non_exchange_error_marks_unknown_with_traceback(  # type: ignore[no-untyped-def]
        self, ctx, caplog
    ) -> None:
        """Запрос ушёл, дальше — непредвиденный сбой не из ExchangeError
        (баг разбора ответа, неожиданная форма data). Исход неизвестен:
        UNKNOWN, а не пролетевшее исключение со строкой, оставшейся PENDING.
        Баг не прячется — полный трейс в логе."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(place_order_error=KeyError("orderId"))
        service = _service(session, settings, client)

        with caplog.at_level("ERROR", logger="app.execution.service"):
            updated = await service.submit_entry_order(
                order=order, position_side="LONG", entry_row=entry_row
            )

        assert updated.status is OrderStatus.UNKNOWN
        assert updated.error_code == "KeyError"
        assert updated.raw_response is None
        records = [r for r in caplog.records if r.name == "app.execution.service"]
        assert any(
            r.exc_info is not None and r.exc_info[0] is KeyError for r in records
        )

    async def test_code_zero_without_order_id_stores_none_not_empty_string(  # type: ignore[no-untyped-def]
        self, ctx, caplog
    ) -> None:
        """code 0 без orderId: биржа приняла — SUBMITTED верен, но пустая
        строка вместо id — молчаливый фолбэк. exchange_order_id=None и
        предупреждение в лог."""
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(place_order_result=_order_result(order_id="", raw={}))
        service = _service(session, settings, client)

        with caplog.at_level("WARNING", logger="app.execution.service"):
            updated = await service.submit_entry_order(
                order=order, position_side="LONG", entry_row=entry_row
            )

        assert updated.status is OrderStatus.SUBMITTED
        assert updated.exchange_order_id is None
        assert "BingX не вернул orderId при code 0" in caplog.text

    async def test_only_one_place_market_order_call(self, ctx) -> None:  # type: ignore[no-untyped-def]
        session, user, signal, settings = ctx
        order = _order(user.id, signal.id)
        entry_row = build_entry_order_pending(order)
        session.add(entry_row)
        await session.flush()

        client = FakeSubmitClient(place_order_result=_order_result())
        service = _service(session, settings, client)

        await service.submit_entry_order(order=order, position_side="LONG", entry_row=entry_row)

        calls = [c for c in client.calls if c[0] == "place_market_order"]
        assert len(calls) == 1
        kwargs = calls[0][1]
        assert kwargs["stop_loss"].trigger_price == order.stop_loss
        assert kwargs["take_profit"].trigger_price == order.take_profit
        assert kwargs["client_order_id"] == order.entry_client_order_id
