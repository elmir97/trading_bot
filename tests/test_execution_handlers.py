"""Тесты app/bot/handlers/execution.py (этап 15.4, разделы 1 и 5 ТЗ).

Через настоящий Dispatcher aiogram — как test_fsm_guard.py: важен порядок
и фактическая отработка хендлеров, не имитация. Bot.__call__ подменён,
поэтому в Telegram ничего не уходит; ExchangeFactory подменена на фейковую,
поэтому на BingX ничего не уходит (раздел 13 ТЗ — ни один тест не ходит
на биржу). Redis — свой фейк, живой сервер не нужен (см. tests/test_locks.py).

Против настоящей БД: сигналу и подтверждению нужны реальные User/SignalRecord/
ExecutionOrder — как test_execution_service.py.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.handlers import execution
from app.bot.keyboards.execution import ExecutionCB
from app.core.config import Settings
from app.core.locks import confirm_lock_key
from app.database.models.execution_order import ExecutionOrder
from app.database.models.signal import SignalRecord
from app.database.models.signal_notification import SignalNotification
from app.database.repositories.signal import SignalRepository
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.trade import TradeRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ApiRestrictions,
    Balance,
    ExchangeAuthError,
    ExchangeClient,
    ExchangeError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    LeverageInfo,
    OrderResult,
    Position,
    SymbolInfo,
    Ticker,
    UnsupportedPositionMode,
)
from app.exchanges.bingx import BingXClient
from app.execution import guards as guards_module
from app.execution import service as execution_service
from app.services.user_service import UserService
from app.trading.enums import (
    ExchangeKeyMode,
    ObservationStage,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalDirection,
    SignalLevel,
    TradeSide,
    TradeStatus,
)
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal
NOW = datetime.now(UTC)
CHAT_ID = 555
USER_TG_ID = 777
BOT_ID = 999


# ---------------------------------------------------------------------------
# Фейки: биржа, ключи, Redis
# ---------------------------------------------------------------------------


class FakeExchangeClient(ExchangeClient):
    name = "fake"

    def __init__(
        self,
        *,
        price: Decimal,
        balance: Decimal,
        symbol_info: SymbolInfo,
        restrictions: ApiRestrictions | None = None,
        restrictions_error: Exception | None = None,
        dual_side_position: bool = True,
        position_mode_error: Exception | None = None,
        current_leverage: object = None,
        leverage_error: Exception | None = None,
        set_leverage_error: Exception | None = None,
        place_order_result: object = None,
        place_order_error: Exception | None = None,
    ) -> None:
        # Шаг 15.5.3: read-back. По умолчанию фейк ведёт себя как биржа:
        # исполнение — по последнему place_market_order, в openOrders —
        # условники из его stopLoss/takeProfit (СИНТЕТИКА ДО 15.5.5).
        self.fill_overrides: dict[str, object] = {}
        self.fill_error: Exception | None = None
        self.attach_conditionals = True
        self.open_orders_error: Exception | None = None
        self.conditional_result: object = None
        self.conditional_error: Exception | None = None
        self.price = price
        self.balance = balance
        self.symbol_info = symbol_info
        self.restrictions = restrictions
        self.restrictions_error = restrictions_error
        # Раздел 16 ТЗ, шаг 15.5.1: дефолт True — как реальный демо-
        # аккаунт (хедж, снято живым запросом разведки раздела 16), не
        # произвольное значение.
        self.dual_side_position = dual_side_position
        self.position_mode_error = position_mode_error
        self.get_position_mode_calls = 0
        # Раздел 16 ТЗ, шаг 15.5.2: плечо и реальная отправка.
        self.current_leverage = current_leverage
        self.leverage_error = leverage_error
        self.set_leverage_error = set_leverage_error
        self.place_order_result = place_order_result
        self.place_order_error = place_order_error
        self.submit_calls: list[tuple[str, dict]] = []
        # Раздел 8 ТЗ: подтверждение (стадия CONFIRM) обязано звать эти
        # методы с max_retries=1, карточка (CARD) — без ограничения.
        self.ticker_retries_seen: list[int | None] = []
        self.balance_retries_seen: list[int | None] = []
        self.symbols_retries_seen: list[int | None] = []
        # Шаг 15.5.4а: живые позиции биржи. Карточка читает их с
        # max_retries=None, «Да» (_submit_real_order) — с max_retries=1.
        self.positions: list[Position] = []
        self.positions_error: Exception | None = None
        self.positions_retries_seen: list[int | None] = []

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        self.ticker_retries_seen.append(max_retries)
        return Ticker(
            symbol=symbol, last_price=self.price, volume_24h=D("0"),
            price_change_percent=D("0"), timestamp=NOW,
        )

    async def get_klines(self, symbol, interval, limit=500, end_time=None):
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

    async def get_positions(self, *, max_retries: int | None = None) -> list[Position]:
        self.positions_retries_seen.append(max_retries)
        if self.positions_error is not None:
            raise self.positions_error
        return self.positions

    async def get_api_restrictions(self) -> ApiRestrictions:
        if self.restrictions_error is not None:
            raise self.restrictions_error
        if self.restrictions is None:
            raise NotImplementedError
        return self.restrictions

    async def get_fills(self, start_time, end_time, symbol=None):
        return []

    async def get_leverage(self, symbol, *, max_retries=None):
        self.submit_calls.append(("get_leverage", {"symbol": symbol, "max_retries": max_retries}))
        if self.leverage_error is not None:
            raise self.leverage_error
        assert self.current_leverage is not None, "тест не задал current_leverage"
        return self.current_leverage

    async def get_position_mode(self, *, max_retries=None):
        self.get_position_mode_calls += 1
        if self.position_mode_error is not None:
            raise self.position_mode_error
        return self.dual_side_position

    async def set_leverage(self, symbol, leverage, *, position_side=None):
        self.submit_calls.append((
            "set_leverage",
            {"symbol": symbol, "leverage": leverage, "position_side": position_side},
        ))
        if self.set_leverage_error is not None:
            raise self.set_leverage_error
        return leverage

    async def place_market_order(self, **kwargs):
        self.submit_calls.append(("place_market_order", kwargs))
        if self.place_order_error is not None:
            raise self.place_order_error
        assert self.place_order_result is not None, "тест не задал place_order_result"
        return self.place_order_result

    async def get_order(self, symbol, client_order_id):
        raise NotImplementedError

    def _last_entry(self) -> dict:
        entries = [c[1] for c in self.submit_calls if c[0] == "place_market_order"]
        assert entries, "read-back до отправки входа"
        return entries[-1]

    async def get_order_fill(self, symbol, client_order_id, *, max_retries=None):  # type: ignore[no-untyped-def]
        self.submit_calls.append(
            ("get_order_fill", {"client_order_id": client_order_id, "max_retries": max_retries})
        )
        if self.fill_error is not None:
            raise self.fill_error
        entry = self._last_entry()
        placed_id = getattr(self.place_order_result, "order_id", "") or "9001"
        raw: dict[str, object] = {
            "orderId": placed_id, "clientOrderId": client_order_id, "status": "FILLED",
            "origQty": str(entry["quantity"]), "executedQty": str(entry["quantity"]),
            "avgPrice": str(self.price), "commission": "-0.05",
        }
        raw.update(self.fill_overrides)
        return BingXClient._parse_order_fill({k: v for k, v in raw.items() if v is not None})

    async def get_open_orders(self, symbol=None, *, max_retries=None):  # type: ignore[no-untyped-def]
        self.submit_calls.append(
            ("get_open_orders", {"symbol": symbol, "max_retries": max_retries})
        )
        if self.open_orders_error is not None:
            raise self.open_orders_error
        if not self.attach_conditionals:
            return []
        entry = self._last_entry()
        closing = "SELL" if entry["side"].value == "BUY" else "BUY"
        now_ms = int((datetime.now(UTC) + timedelta(seconds=1)).timestamp() * 1000)
        orders = []
        for key, order_type, order_id in (
            ("stop_loss", "STOP_MARKET", "8001"), ("take_profit", "TAKE_PROFIT_MARKET", "8002"),
        ):
            spec = entry.get(key)
            if spec is None:
                continue
            orders.append(BingXClient._parse_open_order({
                "symbol": entry["symbol"], "orderId": order_id, "side": closing,
                "positionSide": entry["position_side"], "type": order_type,
                "origQty": "0", "price": "0", "executedQty": "0", "avgPrice": "0",
                "status": "NEW", "stopPrice": str(spec.trigger_price), "clientOrderId": "",
                "time": now_ms, "updateTime": now_ms, "leverage": "10X",
                "reduceOnly": False, "closePosition": "true", "workingType": "MARK_PRICE",
            }))
        return orders

    async def place_conditional_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.submit_calls.append(("place_conditional_order", kwargs))
        if self.conditional_error is not None:
            raise self.conditional_error
        return self.conditional_result or BingXClient._parse_order(
            {"orderId": "8101", "status": "NEW"}
        )

    async def close(self) -> None:
        pass


@dataclass(slots=True)
class FakeCredentials:
    is_read_only: bool = False
    # NOW (не None) по умолчанию — большинство существующих тестов проверяют
    # execution-путь, а не раздел 8 (проверку прав), и не должны неожиданно
    # начать дёргать get_api_restrictions() из-за "протухшей" отметки.
    permissions_checked_at: datetime | None = NOW
    user_id: int = 1
    mode: ExchangeKeyMode = ExchangeKeyMode.LIVE


def _patch_exchange_factory(
    monkeypatch, client: FakeExchangeClient, credentials: FakeCredentials | None
) -> None:
    class FakeFactory:
        def __init__(self, settings, cipher) -> None:
            pass

        async def get_credentials(self, session, user_id, exchange="bingx", mode=None):
            return credentials

        async def for_user(self, session, user_id, exchange="bingx", mode=None):
            return client

        def public_client(self):
            return client

    monkeypatch.setattr(execution, "ExchangeFactory", FakeFactory)


class FakeRedis:
    """См. tests/test_locks.py — тот же минимальный дублёр."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        # Раздел 8 ТЗ: TTL берётся из Settings.confirm_lock_ttl_seconds, не
        # литерала — тест сверяет это по факту переданного `ex`.
        self.set_calls: list[tuple[str, int | None]] = []

    async def set(self, name, value, *, nx=False, ex=None):  # type: ignore[no-untyped-def]
        self.set_calls.append((name, ex))
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def eval(self, script, numkeys, *keys_and_args):  # type: ignore[no-untyped-def]
        key, token = keys_and_args[0], keys_and_args[1]
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def _symbol_info() -> SymbolInfo:
    return SymbolInfo(
        symbol="BTC-USDT", price_precision=1, quantity_precision=3,
        min_quantity=D("0.001"), min_notional=D("5"),
    )


def _restrictions(**overrides: object) -> ApiRestrictions:
    fields: dict[str, object] = {
        "ip_restrict": True, "create_time": NOW, "permits_universal_transfer": False,
        "enable_reading": True, "enable_futures": True, "enable_spot_and_margin_trading": False,
    }
    fields.update(overrides)
    return ApiRestrictions(**fields)  # type: ignore[arg-type]


def _leverage_info(**overrides: object) -> LeverageInfo:
    fields: dict[str, object] = {
        "symbol": "BTC-USDT", "long_leverage": 10, "short_leverage": 10,
        "max_long_leverage": 125, "max_short_leverage": 125,
    }
    fields.update(overrides)
    return LeverageInfo(**fields)  # type: ignore[arg-type]


def _order_result(**overrides: object) -> OrderResult:
    fields: dict[str, object] = {
        "order_id": "9001", "client_order_id": "tj1u1E", "symbol": "BTC-USDT",
        "side": "BUY", "position_side": "LONG", "order_type": "MARKET",
        "status": "FILLED", "price": D("0"), "avg_price": D("100.1"),
        "quantity": D("0.01"), "executed_qty": D("0.01"), "fee": D("0.05"),
        "raw": {"orderId": "9001", "status": "FILLED"},
    }
    fields.update(overrides)
    return OrderResult(**fields)  # type: ignore[arg-type]


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


async def _notify(session, signal: SignalRecord, **overrides: object) -> SignalNotification:  # type: ignore[no-untyped-def]
    """Шаг 15.5.2а: снимок уведомления по текущему состоянию слота — то,
    что пишет сканер при отправке; кнопки адресуют его id."""
    notification = SignalNotification.snapshot_of(
        signal, notified_at=NOW, expires_at=NOW + timedelta(hours=4)
    )
    for key, value in overrides.items():
        setattr(notification, key, value)
    session.add(notification)
    await session.flush()
    return notification


# ---------------------------------------------------------------------------
# Плумбинг aiogram: настоящий Dispatcher, Bot.__call__ подменён
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[TelegramMethod] = []
        self._next_message_id = 10_000

    def _message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    async def __call__(self, bot: Bot, method: TelegramMethod, request_timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=self._message_id(), date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"), text=method.text or "",
            )
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return Message(
                message_id=method.message_id or self._message_id(), date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", "") or "",
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, SendMessage)]

    def alerts(self) -> list[str]:
        return [
            m.text for m in self.calls
            if isinstance(m, AnswerCallbackQuery) and m.show_alert and m.text
        ]


@pytest_asyncio.fixture
async def bot(monkeypatch):  # type: ignore[no-untyped-def]
    recorder = Recorder()

    async def fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return await recorder(self, method, request_timeout)

    monkeypatch.setattr(Bot, "__call__", fake_call)
    instance = Bot(token=f"{BOT_ID}:AAFakeTokenForExecutionTests")
    instance.recorder = recorder  # type: ignore[attr-defined]
    yield instance
    await instance.session.close()


def _fresh_execution_router() -> Router:
    """Как в test_fsm_guard.py: модульный Router нельзя подключить к двум
    Dispatcher-ам подряд — регистрируем те же функции-хендлеры заново."""
    r = Router(name="execution-test")
    r.callback_query.register(execution.open_confirmation, F.data.startswith(ExecutionCB.OPEN))
    r.callback_query.register(execution.confirm_no, F.data.startswith(ExecutionCB.NO))
    r.callback_query.register(execution.expired_noop, F.data == ExecutionCB.EXPIRED)
    r.callback_query.register(execution.confirm_yes, F.data.startswith(ExecutionCB.YES))
    r.callback_query.register(
        execution.legacy_signal_button,
        F.data.startswith(ExecutionCB.LEGACY_OPEN)
        | F.data.startswith(ExecutionCB.LEGACY_YES)
        | F.data.startswith(ExecutionCB.LEGACY_NO),
    )
    return r


def make_callback(data: str, message_id: int) -> CallbackQuery:
    message = Message(
        message_id=message_id, date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="private"), text="card",
    )
    return CallbackQuery(
        id=str(message_id), from_user=TgUser(id=USER_TG_ID, is_bot=False, first_name="Tester"),
        chat_instance="ci", data=data, message=message,
    )


def make_bound_callback(bot: Bot, data: str, message_id: int) -> CallbackQuery:
    """Как make_callback, но с привязкой к bot — нужно тестам, которые
    зовут execution._process_confirm() напрямую, в обход dp.feed_update()
    (тот привязывает bot ко всем вложенным объектам через context при
    парсинге Update, чего прямой конструктор CallbackQuery(...) не делает —
    без этого callback.answer()/callback.message.edit_text() падают с
    "not mounted to any bot instance")."""
    callback = make_callback(data, message_id)
    callback.as_(bot)
    if isinstance(callback.message, Message):
        callback.message.as_(bot)
    return callback


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    # bingx_trading_mode="live" — совпадает с дефолтом
    # UserSettings.active_exchange_mode=LIVE (этап 15.4в), иначе guard
    # MODE_NOT_ALLOWED отказывал бы во всех «счастливых» тестах ниже.
    # exec_allow_live_mode_orders=True — раздел 16 ТЗ, шаг 15.5.1: без
    # этого LIVE_ORDERS_NOT_ALLOWED отказывал бы во всех тех же тестах,
    # раньше даже MODE_NOT_ALLOWED.
    settings = Settings(  # type: ignore[call-arg]
        trading_execution_enabled=True,
        bingx_trading_mode="live",
        exec_allow_live_mode_orders=True,
    )
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        client = FakeExchangeClient(price=D("100"), balance=D("1000"), symbol_info=_symbol_info())
        redis = FakeRedis()

        dp = Dispatcher(storage=MemoryStorage())
        dp["session"] = session
        dp["user"] = user
        dp["settings"] = settings
        dp["cipher"] = None
        dp["redis"] = redis
        dp["db"] = db
        dp.include_router(_fresh_execution_router())

        yield dp, session, user, client, redis, settings
        execution._confirmations.clear()
        await cleanup_user(session, user)
    await db.dispose()


async def _feed(dp: Dispatcher, bot: Bot, update_id: int, callback: CallbackQuery) -> None:
    await dp.feed_update(bot=bot, update=Update(update_id=update_id, callback_query=callback))


async def _orders_for_signal(session, signal_id: int) -> list[ExecutionOrder]:
    stmt = select(ExecutionOrder).where(ExecutionOrder.signal_id == signal_id)
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------------------
# Открыть сделку
# ---------------------------------------------------------------------------


async def test_open_button_sends_confirmation_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    text = texts[0]
    assert "BTC-USDT" in text
    assert "Стоп: 97" in text
    assert "Тейк: 110" in text
    assert (user.id, notification.id) in execution._confirmations


async def test_open_button_shows_refusal_when_execution_disabled(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    settings.trading_execution_enabled = False  # выключаем после сборки dp — тот же объект
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" in texts[0]
    assert (user.id, notification.id) not in execution._confirmations

    # Раздел 12а ТЗ: отказ гварда пишется ExecutionService.evaluate() сам,
    # без похода в биржу — карточка при этом не показывается.
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "EXECUTION_DISABLED"
    assert orders[0].stage == ObservationStage.CARD
    assert orders[0].client_order_id is None


async def test_open_button_refuses_permissions_unknown_on_stale_check_failure(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Раздел 8 ТЗ: отметки нет вовсе, запрос к бирже за правами
    провалился — отказ PERMISSIONS_UNKNOWN, не "открыл бы" вслепую."""
    dp, session, user, client, _redis, _settings = ctx
    client.restrictions_error = ExchangeUnavailableError("BingX не ответил")
    _patch_exchange_factory(
        monkeypatch, client, FakeCredentials(is_read_only=True, permissions_checked_at=None)
    )

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не удалось проверить права ключа" in texts[0]
    assert (user.id, notification.id) not in execution._confirmations

    # Пакет A (инцидент GRAMTON): PERMISSIONS_UNKNOWN теперь строится внутри
    # ExecutionService.evaluate(), как и остальные отказы гвардов — строка
    # в execution_orders пишется, ещё до похода за тикером (price is None).
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "PERMISSIONS_UNKNOWN"
    assert orders[0].client_order_id is None
    assert orders[0].price is None


async def test_open_button_refreshes_stale_permissions_and_proceeds(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Отметка протухла, но запрос прошёл успешно и права дают торговать —
    карточка строится по СВЕЖИМ правам, а не по устаревшему is_read_only
    в БД (raздел 8 ТЗ: NO_TRADING_KEY не должен сработать зря)."""
    dp, session, user, client, _redis, _settings = ctx
    client.restrictions = _restrictions(enable_futures=True)
    creds = FakeCredentials(
        is_read_only=True,  # устаревшее значение — по нему NO_TRADING_KEY отказал бы
        permissions_checked_at=NOW - timedelta(hours=7),
    )
    _patch_exchange_factory(monkeypatch, client, creds)

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" not in texts[0]
    assert "BTC-USDT" in texts[0]
    assert (user.id, notification.id) in execution._confirmations

    assert creds.is_read_only is False
    assert creds.permissions_checked_at is not None


async def test_open_button_shows_refusal_when_mode_not_allowed(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Этап 15.4в: в настройках выбран демо-счёт, а конфиг (в фикстуре ctx —
    bingx_trading_mode="live") разрешает исполнение только на реальном —
    карточка не показывается, приходит понятный отказ."""
    dp, session, user, client, _redis, _settings = ctx
    user.settings.active_exchange_mode = ExchangeKeyMode.DEMO
    await session.flush()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "реальном счёте" in texts[0]
    assert (user.id, notification.id) not in execution._confirmations

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "MODE_NOT_ALLOWED"


async def test_open_button_shows_refusal_when_signal_stale(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Пакет B: цена ушла далеко от сигнала в сторону тейка ещё до первого
    показа карточки — то, что раньше не ловил ни один гвард (PRICE_DRIFT на
    первом вызове структурно бессилен, см. ExecutionService.evaluate)."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    # reference = (100+101)/2 = 100.5, стоп 97 → дистанция 3.5, допустимо
    # (дефолт ratio=1.0) 3.5. Тейк далеко (200), чтобы RR остался в норме
    # и карточка не отказала бы под видом INVALID_LEVELS.
    signal = _signal(user.id, take_profit=D("200"))
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    client.price = D("106")

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" in texts[0]
    assert "устарел" in texts[0]
    assert (user.id, notification.id) not in execution._confirmations

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "SIGNAL_STALE"


# ---------------------------------------------------------------------------
# Да
# ---------------------------------------------------------------------------


async def test_confirm_yes_creates_dry_run_orders(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 3
    assert {o.role for o in orders} == {OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}
    assert all(o.status is OrderStatus.DRY_RUN for o in orders)

    await session.refresh(notification)
    assert notification.trade_opened_at is not None

    dry_run_texts = [t for t in bot.recorder.sent_texts() if "Сухой прогон" in t]
    assert len(dry_run_texts) == 1
    assert (user.id, notification.id) not in execution._confirmations


async def _open_and_confirm(dp, bot, notification, user) -> None:  # type: ignore[no-untyped-def]
    """Общий пролог для тестов реальной отправки ниже: открыть карточку,
    нажать «Да»."""
    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )


async def test_confirm_yes_submits_real_order(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 16 ТЗ, шаг 15.5.2: EXEC_DRY_RUN=false — счастливый путь.
    Одна ENTRY-строка SUBMITTED (не три, как у DRY_RUN — SL/TP-строки
    появятся на read-back, 15.5.3), сообщение "Ордер отправлен, id …"
    (раздел 16 ТЗ, шаг 15.5.2, п.7 плана)."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)  # совпадает с plan.max_leverage=10
    client.place_order_result = _order_result(order_id="555555")
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    orders = await _orders_for_signal(session, signal.id)
    entries = [o for o in orders if o.role is OrderRole.ENTRY]
    assert len(entries) == 1
    # Шаг 15.5.3: read-back подтвердил исполнение — FILLED, а не SUBMITTED;
    # стоп и тейк найдены среди openOrders — строки S/T с их orderId.
    assert entries[0].status is OrderStatus.FILLED
    assert entries[0].exchange_order_id == "555555"
    assert entries[0].client_order_id is not None
    assert {o.role: o.exchange_order_id for o in orders if o.role is not OrderRole.ENTRY} == {
        OrderRole.STOP_LOSS: "8001", OrderRole.TAKE_PROFIT: "8002",
    }

    await session.refresh(notification)
    assert notification.trade_opened_at is not None

    submit_calls = [c[0] for c in client.submit_calls]
    # плечо совпало — set_leverage не вызывается; стоп и тейк на месте —
    # спасения нет, openOrders прочитан один раз.
    assert submit_calls == [
        "get_leverage", "place_market_order", "get_order_fill", "get_open_orders",
    ]
    # Шаг 15.5.4а: позиции биржи — на карточке (без ограничения повторов) и
    # на «Да» клиентом отправки (max_retries=1).
    assert client.positions_retries_seen == [None, 1]

    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert "⏳ Ордер отправлен, проверяю исполнение…" in edits
    assert any("✅ Вход исполнен: BTC-USDT" in t and "id 8001" in t for t in edits)


def _exchange_position(side: TradeSide = TradeSide.LONG) -> Position:
    return Position(
        symbol="BTC-USDT", side=side, quantity=D("0.8397"), entry_price=D("100"),
        mark_price=D("100"), leverage=10, unrealized_pnl=D("0"), margin=D("10"),
    )


async def test_card_refuses_on_live_exchange_position(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Шаг 15.5.4а: журнал пуст, на бирже позиция по символу — карточка не
    открывается."""
    dp, session, user, client, _redis, _settings = ctx
    client.positions = [_exchange_position(TradeSide.SHORT)]
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "На бирже уже есть открытая позиция по этому символу." in texts[0]
    assert (user.id, notification.id) not in execution._confirmations
    [row] = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == "EXCHANGE_POSITION_EXISTS"
    assert row.stage == "card"


async def test_confirm_yes_refuses_on_position_opened_after_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Позиция появилась на бирже между карточкой и «Да» — отказ клиентом
    отправки, до плеча: ни get_leverage, ни ордера, отметки нет."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    client.positions = [_exchange_position()]
    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    assert client.positions_retries_seen == [None, 1]
    assert client.submit_calls == []
    [row] = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == "EXCHANGE_POSITION_EXISTS"
    assert row.stage == "confirm"
    await session.refresh(notification)
    assert notification.trade_opened_at is None
    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert any("На бирже уже есть открытая позиция по этому символу." in t for t in edits)


async def test_confirm_yes_one_way_position_names_reason(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """positionSide=BOTH на «Да» — ERROR-строка и текст с причиной, не
    «биржа не отвечает»; до плеча и ордера не доходит."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    client.positions_error = UnsupportedPositionMode("BTC-USDT")
    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    assert client.submit_calls == []
    [row] = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.error_code == "UnsupportedPositionMode"
    assert row.stage == "confirm"
    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert (
        "⛔ BTC-USDT: позиция в режиме one-way (BOTH) — форма ответа не проверена, "
        "вход заблокирован"
    ) in edits


async def test_confirm_yes_dry_run_reads_exchange_positions_only_on_card(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Сухой прогон до _submit_real_order не доходит — на «Да» биржу не
    проверяет (ордера нет); evaluate() на «Да» позиции не читает."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    assert client.positions_retries_seen == [None]
    orders = await _orders_for_signal(session, signal.id)
    assert all(o.status is OrderStatus.DRY_RUN for o in orders)


async def test_confirm_yes_real_order_leverage_mismatch_calls_set_leverage(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=5)  # расходится с plan.max_leverage=10
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    submit_calls = {c[0]: c[1] for c in client.submit_calls}
    assert "set_leverage" in submit_calls
    assert submit_calls["set_leverage"]["position_side"] == "LONG"
    assert submit_calls["set_leverage"]["position_side"] is not None
    assert submit_calls["set_leverage"]["position_side"] != "BOTH"


async def test_confirm_yes_real_order_rejected_shows_known_code_not_raw_msg(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Раздел 16 ТЗ, шаг 15.5.2, п.1 и п.7 плана: биржа явно отказала
    (code задан и не 0) → REJECTED, текст пользователю — по известным
    кодам, иначе общий фолбэк; сырой msg биржи в чат не попадает."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_error = ExchangeResponseError(
        "BingX: insufficient margin, no leaking (код 80001)",
        code=80001,
        payload={"code": 80001, "msg": "insufficient margin, no leaking"},
    )
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REJECTED
    assert orders[0].error_code == "80001"

    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert any(
        "BingX отклонил ордер, код 80001\n"
        "Повторить по этому сигналу нельзя — дождись следующего уведомления." in t
        for t in edits
    )
    assert not any("insufficient margin" in t for t in edits)


async def test_confirm_yes_real_order_timeout_shows_unknown_text(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Раздел 16 ТЗ, шаг 15.5.2, п.1 и п.7 плана: ответа не было —
    UNKNOWN, не REJECTED, текст без ссылок на шаги разработки."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_error = ExchangeUnavailableError("BingX не ответил вовремя")
    # Шаг 15.5.3: поиск по clientOrderID тоже не находит ордер.
    client.fill_error = ExchangeResponseError("order not exist", code=109414, payload=None)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.UNKNOWN

    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    # Шаг 15.5.3: UNKNOWN — один поиск по clientOrderID; не нашёлся —
    # повторной отправки нет.
    assert any(
        "Биржа не подтвердила ордер — проверь позиции в BingX. "
        "Повторно не отправляю." in t
        for t in edits
    )
    assert [c[0] for c in client.submit_calls].count("place_market_order") == 1
    assert not any("15.5" in t for t in edits)


async def test_confirm_yes_real_order_without_order_id_says_so(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """code 0 без orderId: пустая строка в exchange_order_id не пишется.
    Шаг 15.5.3: id приходит из read-back (get_order по clientOrderID) —
    именно та сверка, которую раньше обещал текст «сверю по
    clientOrderID»; пользователь видит итог read-back, а не «id »."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result(order_id="", raw={})
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    [entry] = [
        o for o in await _orders_for_signal(session, signal.id) if o.role is OrderRole.ENTRY
    ]
    assert entry.status is OrderStatus.FILLED
    assert entry.exchange_order_id == "9001"  # из read-back, не "" из ответа на POST

    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert any(t.startswith("✅ Вход исполнен") for t in edits)
    assert not any("id " + "\n" in t or t.endswith("id ") for t in edits)


async def test_confirm_yes_live_orders_not_allowed_blocks_before_http(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Раздел 16 ТЗ, шаг 15.5.2, п.3 плана: bingx_trading_mode="live" и
    exec_allow_live_mode_orders=False — place_market_order не вызывается.
    Тест всего пути: флаг выключается между карточкой и «Да», и отказывает
    ранняя проверка в evaluate() на «Да» (слой A), до _submit_real_order.
    Падает только при выключенных всех трёх слоях (A — evaluate(), B —
    run_guards(), C — _submit_real_order). Слой C отдельно —
    test_live_orders_send_phase_layer_refuses_alone."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    # Обходим evaluate()/run_guards() тем же приёмом, что и остальные
    # тесты файла (мутация settings после сборки dp): гвард уже пропустил
    # запрос на "Да" (значения были верными при открытии карточки), эта
    # защита — вторая, в самой фазе отправки, а не подмена первой.
    settings.exec_allow_live_mode_orders = False

    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    assert client.submit_calls == []
    orders = await _orders_for_signal(session, signal.id)
    assert any(
        o.status is OrderStatus.REFUSED and o.error_code == "LIVE_ORDERS_NOT_ALLOWED"
        for o in orders
    )


async def test_live_without_allow_refuses_card_before_any_send(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Шаг 15.5.5: live + EXEC_DRY_RUN=false + EXEC_ALLOW_LIVE_MODE_ORDERS=false
    с самого начала. Карточка отказывает LIVE_ORDERS_NOT_ALLOWED раньше
    тикера, баланса, инструментов, позиций и любой отправки.

    Не раньше ЛЮБОГО обращения к бирже: _build_quote до evaluate() читает
    права ключа (apiRestrictions) и режим позиций (positionSide/dual) —
    это чтение, не отправка; порядок сознательно не меняли.
    Падает при выключенных слоях A (evaluate) и B (run_guards)."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False
    settings.exec_allow_live_mode_orders = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    assert (user.id, notification.id) not in execution._confirmations
    [row] = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == "LIVE_ORDERS_NOT_ALLOWED"
    assert row.stage == "card"
    # Чтения до evaluate() — были (см. docstring).
    assert client.get_position_mode_calls == 1
    # Ничего из evaluate() после гварда и ничего из отправки.
    assert client.ticker_retries_seen == []
    assert client.balance_retries_seen == []
    assert client.symbols_retries_seen == []
    assert client.positions_retries_seen == []
    assert client.submit_calls == []


async def test_live_orders_send_phase_layer_refuses_alone(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Слой C — повтор LIVE_ORDERS_NOT_ALLOWED в _submit_real_order — сам по
    себе: слои A (evaluate) и B (run_guards) выключены подменой, карточка
    строится, «Да» доходит до фазы отправки. Отказ раньше get_positions,
    плеча и ордера, отметки нет. Падает при выключенном слое C."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False
    settings.exec_allow_live_mode_orders = False
    monkeypatch.setattr(execution_service, "check_live_orders_allowed", lambda **kw: None)
    monkeypatch.setattr(guards_module, "check_live_orders_allowed", lambda **kw: None)

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    orders = await _orders_for_signal(session, signal.id)
    [row] = [o for o in orders if o.error_code == "LIVE_ORDERS_NOT_ALLOWED"]
    assert row.status is OrderStatus.REFUSED
    assert row.stage == "confirm"
    # Позиции читала только карточка; на «Да» — ни чтения, ни отправки.
    assert client.positions_retries_seen == [None]
    assert client.submit_calls == []
    await session.refresh(notification)
    assert notification.trade_opened_at is None


async def test_confirm_yes_real_order_commit_survives_later_exception(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Раздел 16 ТЗ, шаг 15.5.2, п.4 плана: коммит после ответа биржи —
    не flush(). Падение ПОСЛЕ submit_entry_order() (здесь — в logger.info
    сразу за вторым commit()) не должно откатить уже известный биржевой
    статус обратно в PENDING. Проверка — вторым соединением к той же
    тестовой БД, не той же (ещё не закоммиченной снаружи) сессией."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result(order_id="424242")
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] == "Реальная отправка ордера":
            raise RuntimeError("boom-after-commit")

    monkeypatch.setattr(execution.logger, "info", _boom)

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    signal_id = signal.id

    with pytest.raises(RuntimeError, match="boom-after-commit"):
        await _open_and_confirm(dp, bot, notification, user)

    db2 = Database(settings)
    async with db2.session() as session2:
        row = await session2.scalar(
            select(ExecutionOrder).where(ExecutionOrder.signal_id == signal_id)
        )
        assert row is not None
        assert row.status is OrderStatus.SUBMITTED
        assert row.exchange_order_id == "424242"
    await db2.dispose()


async def test_position_mode_read_on_card_not_reread_on_confirm(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 16 ТЗ, шаг 15.5.1: чтение — только на построении карточки
    (как refresh_permissions). По нажатию «Да» не перезапрашивается —
    значение несётся в ExecutionQuote.dual_side_position."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    assert client.get_position_mode_calls == 1
    state = execution._confirmations[(user.id, notification.id)]
    assert state.quote.dual_side_position is True  # дефолт фейка

    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    assert client.get_position_mode_calls == 1  # не переспросили на "Да"
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 3


async def test_open_button_refuses_position_mode_unknown_on_fetch_failure(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """По образцу test_open_button_refuses_permissions_unknown_on_stale_
    check_failure выше: сбой чтения режима позиций — отказ этим кодом,
    не "открыл бы" вслепую."""
    dp, session, user, client, _redis, _settings = ctx
    client.position_mode_error = ExchangeUnavailableError("BingX не ответил")
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не удалось проверить режим позиций" in texts[0]
    assert (user.id, notification.id) not in execution._confirmations

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "POSITION_MODE_UNKNOWN"
    assert orders[0].client_order_id is None
    assert orders[0].price is None


async def test_confirm_yes_lock_ttl_comes_from_settings(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: TTL лока — settings.confirm_lock_ttl_seconds, не
    литерал 15."""
    dp, session, user, client, redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    lock_calls = [
        ex for name, ex in redis.set_calls if name == confirm_lock_key(user.id, notification.id)
    ]
    assert lock_calls == [settings.confirm_lock_ttl_seconds]
    assert settings.confirm_lock_ttl_seconds != 15


async def test_confirm_no_cancels_without_orders(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    await _feed(dp, bot, 2, make_callback(f"exn:no:{notification.id}", message_id=state.message_id))

    # Раздел 12а ТЗ: карточка была показана и отклонена пользователем — не
    # DRY_RUN (нет тройки вход/стоп/тейк), а одна строка-наблюдение DECLINED
    # с цифрами карточки, без client_order_id (нечего отправлять на биржу).
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.DECLINED
    assert orders[0].role is OrderRole.ENTRY
    assert orders[0].client_order_id is None
    assert orders[0].quantity is not None
    await session.refresh(notification)
    assert notification.trade_opened_at is None
    assert (user.id, notification.id) not in execution._confirmations


async def test_confirm_yes_busy_lock_answers_and_creates_nothing(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: занятый ключ — мгновенный отказ, без ожидания."""
    dp, session, user, client, redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    redis.store[confirm_lock_key(user.id, notification.id)] = "someone-elses-token"

    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    assert any("уже обрабатывается" in a.lower() for a in bot.recorder.alerts())
    orders = await _orders_for_signal(session, signal.id)
    assert orders == []


async def test_two_parallel_confirms_one_signal_race_is_caught(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: разведка перед 15.5 показала, что при реалистичных
    таймаутах биржи путь подтверждения способен занять дольше TTL лока —
    ключ в Redis гаснет по таймеру, пока обработка ещё легитимно идёт.
    Второе нажатие в этом окне успешно берёт лок и запускает второй,
    полностью параллельный _process_confirm. Воспроизводим это напрямую,
    в обход RedisLock (как если бы TTL уже истёк), двумя независимыми
    сессиями на одно уведомление — как в проде два разных апдейта получают
    каждый свою сессию.

    Шаг 15.5.2а: в проде такую пару уже сериализует FOR NO KEY UPDATE на
    строке слота (SignalRepository.get_for_update) — второй дождётся
    коммита первого и получит SIGNAL_ALREADY_USED. Этот тест проверяет
    последний рубеж на случай, если сериализации нет: get_for_update
    подменён чтением БЕЗ блокировки, а барьер держит оба процесса на
    входе в гварды идентичности (после перечитывания снимка), пока до
    него не дойдут ОБА — оба видят trade_opened_at=None, проходят
    SIGNAL_ALREADY_USED и расходятся заведомо позже, на UNIQUE
    client_order_id при записи execution_orders. Без барьера планировщик
    asyncio иногда прогонял один процесс целиком раньше, чем второй
    доходил до чтения, и тест проходил зелёным, ни разу не исполнив ветку
    IntegrityError (флейк ~1 на 5 прогонов)."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await session.commit()  # видно другим сессиям/соединениям

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    # Подмены ставим ПОСЛЕ open_confirmation: у него свой, одиночный вызов
    # evaluate() (до гонки) — если бы барьер ждал двоих уже тогда, тест
    # завис бы там навсегда.
    barrier = asyncio.Barrier(2)
    original_get = SignalRepository.get

    async def unlocked_get_for_update(self, sid: int, uid: int):  # type: ignore[no-untyped-def]
        # Без FOR NO KEY UPDATE: иначе второй процесс ждал бы коммита
        # первого, а первый — второго на барьере ниже (взаимоблокировка).
        return await original_get(self, sid, uid)

    # Барьер — внутри evaluate(), прямо перед гвардами идентичности: оба
    # процесса к этому моменту уже перечитали снимок (trade_opened_at=None).
    original_exists_traded = execution_service.SignalNotificationRepository.exists_traded

    async def synced_exists_traded(self, signal_id: int, fingerprint: str) -> bool:  # type: ignore[no-untyped-def]
        result = await original_exists_traded(self, signal_id, fingerprint)
        await barrier.wait()
        return result

    monkeypatch.setattr(SignalRepository, "get_for_update", unlocked_get_for_update)
    monkeypatch.setattr(
        execution_service.SignalNotificationRepository, "exists_traded", synced_exists_traded
    )

    db = Database(settings)
    try:
        async def run_confirm() -> None:
            async with db.session() as own_session:
                callback = make_bound_callback(
                    bot, f"exn:yes:{notification.id}", message_id=state.message_id
                )
                await execution._process_confirm(
                    callback, own_session, user, notification.id, settings, None, db
                )

        # asyncio.gather без RedisLock: оба процесса реально параллельны,
        # как если бы каждый уже прошёл (или в обход) проверку лока.
        await asyncio.gather(run_confirm(), run_confirm())
    finally:
        await db.dispose()

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 3, "дубля тройки ордеров или REFUSED-строки быть не должно"
    assert {o.role for o in orders} == {
        OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT,
    }
    assert all(o.status is OrderStatus.DRY_RUN for o in orders)

    edit_texts = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    success_texts = [t for t in edit_texts if t and "Подтверждено" in t]
    race_texts = [t for t in edit_texts if t and "уже обрабатывается" in t]
    assert len(success_texts) == 1
    assert len(race_texts) == 1

    edit_texts = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    success_texts = [t for t in edit_texts if t and "Подтверждено" in t]
    race_texts = [
        t for t in edit_texts
        if t and ("уже обрабатывается" in t or "уже открывали сделку" in t)
    ]
    assert len(success_texts) == 1
    assert len(race_texts) == 1

    await session.refresh(notification)
    assert notification.trade_opened_at is not None


class _FakeAsyncpgCauseError(Exception):
    """Подобие настоящего asyncpg.exceptions.*Error — только он несёт
    .constraint_name (проверено живым запросом к тестовой БД, см. докстринг
    execution._is_client_order_id_collision)."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__("fake asyncpg error")
        self.constraint_name = constraint_name


class _FakeOrigError(Exception):
    """Подобие sqlalchemy.dialects.postgresql.asyncpg.AsyncAdapt_asyncpg_dbapi
    .IntegrityError — обёртки SQLAlchemy без .constraint_name на себе,
    настоящая ошибка asyncpg лежит в .__cause__."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__("fake db error")
        self.__cause__ = _FakeAsyncpgCauseError(constraint_name)


def _fake_integrity_error(constraint_name: str):  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import IntegrityError

    return IntegrityError("INSERT", {}, _FakeOrigError(constraint_name))


class TestIsClientOrderIdCollision:
    """Юнит-тест предиката напрямую, без похода в БД/диспетчер: отличает
    гонку по client_order_id от IntegrityError другого происхождения
    (NOT NULL, FK и т.п.) — второй не должен выглядеть как «сигнал уже
    обрабатывается» (баг кода, не гонка)."""

    def test_true_for_client_order_id_unique_violation(self) -> None:
        exc = _fake_integrity_error("uq_execution_orders_client_order_id")
        assert execution._is_client_order_id_collision(exc) is True

    def test_false_for_other_constraint(self) -> None:
        exc = _fake_integrity_error("fk_execution_orders_user_id_users")
        assert execution._is_client_order_id_collision(exc) is False

    def test_false_when_cause_missing(self) -> None:
        """orig без __cause__ (не asyncpg-ошибка вовсе) — не считаем
        совпадением по умолчанию, не притворяемся, что знаем происхождение."""
        from sqlalchemy.exc import IntegrityError

        exc = IntegrityError("INSERT", {}, Exception("нет __cause__"))
        assert execution._is_client_order_id_collision(exc) is False


async def test_integrity_error_other_than_client_order_id_propagates(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Сквозной прогон через _process_confirm: IntegrityError другого
    происхождения не превращается в «уже обрабатывается» и не глотается —
    выходит наружу как есть."""
    from sqlalchemy.exc import IntegrityError

    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    async def fake_flush(self) -> None:  # type: ignore[no-untyped-def]
        raise _fake_integrity_error("fk_execution_orders_user_id_users")

    from app.database.repositories.execution_order import ExecutionOrderRepository

    monkeypatch.setattr(ExecutionOrderRepository, "flush", fake_flush)

    callback = make_bound_callback(bot, f"exn:yes:{notification.id}", message_id=state.message_id)
    with pytest.raises(IntegrityError):
        await execution._process_confirm(
            callback, session, user, notification.id, settings, None, dp["db"]
        )

    edit_texts = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert not any(t and "уже обрабатывается" in t for t in edit_texts)


async def test_confirm_yes_after_ttl_shows_expired(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    state.created_at = datetime.now(UTC) - timedelta(seconds=settings.exec_confirm_ttl_seconds + 1)

    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    # Раздел 12а ТЗ: "Да" пришло позже TTL — считаем карточку истёкшей и
    # логируем EXPIRED, а не молча проглатываем попытку.
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.EXPIRED
    assert (user.id, notification.id) not in execution._confirmations
    edits = [m for m in bot.recorder.calls if isinstance(m, EditMessageReplyMarkup)]
    assert len(edits) == 1


async def test_expire_card_background_task_writes_expired(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 12а ТЗ: настоящая фоновая просрочка (никто не нажал ни одну
    кнопку) тоже обязана записать EXPIRED, не только путь через "Да".

    _expire_card пишет из отдельной сессии (задача не участвует в
    транзакции хендлера, который её запустил) — сигнал обязан быть
    закоммичен по-настоящему, иначе отдельное соединение его не увидит
    (в проде транзакция апдейта давно закрыта к моменту срабатывания TTL;
    здесь это эмулирует явный commit вместо обычного flush).
    """
    dp, session, user, client, _redis, settings = ctx
    settings.exec_confirm_ttl_seconds = 0
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await session.commit()

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    assert (user.id, notification.id) in execution._confirmations

    tasks = list(execution._background_tasks)
    assert len(tasks) == 1
    await tasks[0]

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.EXPIRED
    assert (user.id, notification.id) not in execution._confirmations
    edits = [m for m in bot.recorder.calls if isinstance(m, EditMessageReplyMarkup)]
    assert len(edits) == 1


async def test_confirm_yes_price_drift_sends_recalculated_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    # Дрейф 1 > 0.3 * 3 (дистанция до стопа 97) — за границей допустимого,
    # но RR по новой цене (101→110 против стопа 97) всё ещё ≥ EXEC_MIN_RR,
    # так что пересчитанная карточка обязана уйти успешно, а не отказом.
    client.price = D("101")

    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    # Раздел 12а ТЗ: сам PRICE_DRIFT-отказ на "Да" — это тоже отказ гварда,
    # ExecutionService.evaluate() пишет его сам (REFUSED). Тройки DRY_RUN
    # при этом всё ещё нет — вход не состоялся, только пересчёт карточки.
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "PRICE_DRIFT"
    assert orders[0].stage == ObservationStage.CONFIRM
    await session.refresh(notification)
    assert notification.trade_opened_at is None

    # Новая карточка отправлена (снова "Да, открыть" где-то в текстах) —
    # это следующее сообщение с кнопками, а не тихий вход.
    assert (user.id, notification.id) in execution._confirmations
    new_state = execution._confirmations[(user.id, notification.id)]
    assert new_state.planned_price == D("101")


# ---------------------------------------------------------------------------
# Пакет C: стадия отказа и сбои биржи (раздел 12а)
# ---------------------------------------------------------------------------


def _patch_factory_auth_error(monkeypatch, client: FakeExchangeClient, error: Exception) -> None:
    """Ключ есть, но for_user падает (ExchangeAuthError и т.п.) — сбой до
    гвардов, ещё до похода за ценой."""

    class FailingFactory:
        def __init__(self, settings, cipher) -> None:
            pass

        async def get_credentials(self, session, user_id, exchange="bingx", mode=None):
            return FakeCredentials(is_read_only=False)

        async def for_user(self, session, user_id, exchange="bingx", mode=None):
            raise error

        def public_client(self):
            return client

    monkeypatch.setattr(execution, "ExchangeFactory", FailingFactory)


def _make_ticker_fail(monkeypatch, client: FakeExchangeClient, error: Exception) -> None:
    async def failing(symbol: str, *, max_retries: int | None = None):
        raise error

    monkeypatch.setattr(client, "get_ticker", failing)


async def test_card_stage_refusal_is_marked_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    settings.trading_execution_enabled = False
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.REFUSED
    assert row.stage == ObservationStage.CARD


async def test_confirm_stage_refusal_is_marked_confirm(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Карточка показана, на «Да» отказал гвард (тут — SIGNAL_ALREADY_USED
    невозможен, поэтому выключаем исполнение между показом и «Да»)."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    settings.trading_execution_enabled = False

    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == "EXECUTION_DISABLED"
    assert row.stage == ObservationStage.CONFIRM


async def test_price_drift_refusal_on_yes_is_confirm_and_recalculated_card_is_card(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    client.price = D("101")
    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.error_code == "PRICE_DRIFT"
    assert row.stage == ObservationStage.CONFIRM


async def test_auth_error_on_card_is_recorded_as_error_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_factory_auth_error(monkeypatch, client, ExchangeAuthError("ключ отозван"))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1 and "ключ отозван" in texts[0]
    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.stage == ObservationStage.CARD
    assert row.error_code == "ExchangeAuthError"
    assert row.role is OrderRole.ENTRY
    assert row.client_order_id is None


async def test_exchange_error_in_evaluate_on_card_is_recorded(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    _make_ticker_fail(monkeypatch, client, ExchangeUnavailableError("timeout"))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    assert len(bot.recorder.sent_texts()) == 1
    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.stage == ObservationStage.CARD
    assert row.error_code == "ExchangeUnavailableError"


async def test_exchange_error_on_yes_is_recorded_as_error_confirm(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    _make_ticker_fail(monkeypatch, client, ExchangeUnavailableError("timeout"))

    await _feed(
        dp, bot, 2, make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    )

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.stage == ObservationStage.CONFIRM
    assert (user.id, notification.id) not in execution._confirmations
    await session.refresh(notification)
    assert notification.trade_opened_at is None


async def test_error_row_never_stores_exception_text(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """В str(exc) может быть тело ответа биржи (одна из ручек эхом отдаёт
    apiKey): в БД — только класс исключения."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    _make_ticker_fail(monkeypatch, client, ExchangeError("body: apiKey=SECRET-KEY-123"))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.error_message is None
    assert row.raw_response is None
    assert "SECRET-KEY-123" not in f"{row.error_code}{row.error_message}{row.raw_response}"


async def test_failed_observation_write_does_not_swallow_user_message(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch, caplog
) -> None:
    dp, session, user, client, _redis, _settings = ctx
    _patch_factory_auth_error(monkeypatch, client, ExchangeAuthError("ключ отозван"))

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(execution, "build_exchange_error_order", boom)
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1 and "ключ отозван" in texts[0]
    assert await _orders_for_signal(session, signal.id) == []


# ---------------------------------------------------------------------------
# Шаг 15.5.2а: уведомление сигнала как неизменяемая сущность
# ---------------------------------------------------------------------------


def _exchange_calls(client: FakeExchangeClient) -> int:
    """Все обращения к бирже, которые фейк умеет считать."""
    return (
        len(client.ticker_retries_seen)
        + len(client.balance_retries_seen)
        + len(client.symbols_retries_seen)
        + len(client.submit_calls)
        + client.get_position_mode_calls
    )


async def _orders_for_notification(session, notification_id: int) -> list[ExecutionOrder]:  # type: ignore[no-untyped-def]
    stmt = select(ExecutionOrder).where(ExecutionOrder.notification_id == notification_id)
    return list((await session.scalars(stmt)).all())


async def test_card_window_superseded_refuses_on_yes_without_exchange_calls(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Карточка показана → сканер перезаписал слот новым сетапом (новый
    fingerprint, другие уровни) → «Да» даёт SIGNAL_SUPERSEDED, и после
    показа карточки на биржу не уходит ни одного запроса. До 15.5.2а «Да»
    молча исполнило бы новые уровни слота, которых пользователь не видел."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]
    calls_after_card = _exchange_calls(client)

    signal.fingerprint = "fp-new"
    signal.stop_loss = D("90")
    signal.take_profit = D("130")
    await session.flush()

    await _feed(
        dp, bot, 2,
        make_callback(f"exn:yes:{notification.id}", message_id=state.message_id),
    )

    assert _exchange_calls(client) == calls_after_card
    rows = await _orders_for_notification(session, notification.id)
    refused = [r for r in rows if r.status is OrderStatus.REFUSED]
    assert len(refused) == 1
    assert refused[0].error_code == "SIGNAL_SUPERSEDED"
    assert refused[0].stage == ObservationStage.CONFIRM
    assert not [r for r in rows if r.status is OrderStatus.DRY_RUN]
    await session.refresh(notification)
    assert notification.trade_opened_at is None

    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert any(
        "Сетап обновился после уведомления — новые уровни придут новым сигналом" in (t or "")
        for t in edits
    )


@pytest.mark.parametrize("prefix", ["exec:open:", "exec:yes:", "exec:no:"])
async def test_legacy_signal_button_answers_stale_and_does_nothing(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch, prefix
) -> None:
    """Кнопки старого образца exec:*:{signal_id} из уже отправленных
    сообщений: «Уведомление устарело», ни записей, ни карточки, ни биржи."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    await _notify(session, signal)

    await _feed(dp, bot, 1, make_callback(f"{prefix}{signal.id}", message_id=1))

    assert bot.recorder.alerts() == ["⏳ Уведомление устарело — дождись нового сигнала."]
    assert bot.recorder.sent_texts() == []
    assert await _orders_for_signal(session, signal.id) == []
    assert execution._confirmations == {}
    assert _exchange_calls(client) == 0


async def test_open_unknown_notification_answers_stale(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Кнопка указывает на id, которого в БД нет (коммит сканера после
    отправки не случился) — «устарело», а не исключение."""
    dp, _session, _user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    await _feed(dp, bot, 1, make_callback("exn:open:2147483000", message_id=1))

    assert bot.recorder.sent_texts() == ["⏳ Уведомление устарело — дождись нового сигнала."]
    assert _exchange_calls(client) == 0


@pytest.mark.parametrize("dry_run", [True, False])
async def test_two_notifications_same_slot_distinct_client_order_ids(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch, dry_run
) -> None:
    """Два уведомления одного слота с разными fingerprint: вход по каждому
    получает свой client_order_id (tj{nid}u…E), второй вход не упирается в
    UNIQUE. До 15.5.2а оба давали tj{signal_id}u…E, и второй вход ловил
    ложную «гонку»."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    if not dry_run:
        settings.exec_dry_run = False  # обходим валидатор конструктора, как и другие тесты
        client.current_leverage = _leverage_info(long_leverage=10)
        client.place_order_result = _order_result()

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    await _open_and_confirm(dp, bot, first, user)
    if not dry_run:
        # Шаг 15.5.4: вход пишет сделку в журнал, и открытая позиция по
        # символу блокирует второй вход (POSITION_EXISTS). Этот тест — про
        # идентичность входа, поэтому первая позиция к моменту второго
        # сигнала закрыта.
        for trade in await TradeRepository(session).list_open(user.id):
            trade.status = TradeStatus.CLOSED
            trade.closed_at = datetime.now(UTC)
        await session.flush()

    signal.fingerprint = "fp-second"
    signal.stop_loss = D("96")
    await session.flush()
    second = await _notify(session, signal)
    await _open_and_confirm(dp, bot, second, user)

    entries = [
        o for o in await _orders_for_signal(session, signal.id) if o.role is OrderRole.ENTRY
    ]
    # Шаг 15.5.3: на реальном пути read-back подтверждает исполнение — FILLED.
    expected_status = OrderStatus.DRY_RUN if dry_run else OrderStatus.FILLED
    assert [o.status for o in entries] == [expected_status, expected_status]
    assert {o.client_order_id for o in entries} == {
        f"tj{first.id}u{user.id}E", f"tj{second.id}u{user.id}E",
    }
    assert {o.notification_id for o in entries} == {first.id, second.id}

    edits = [m.text or "" for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert not any("уже обрабатывается" in t for t in edits)
    if not dry_run:
        placed = [
            c[1]["client_order_id"] for c in client.submit_calls if c[0] == "place_market_order"
        ]
        assert placed == [f"tj{first.id}u{user.id}E", f"tj{second.id}u{user.id}E"]


async def test_setup_already_traded_refuses_other_notification_of_same_setup(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """По сетапу вошли через одно уведомление; сканер прислал новое
    уведомление того же сетапа (тот же fingerprint) — повторный вход
    запрещён: SETUP_ALREADY_TRADED уже на карточке, без запросов к бирже
    за ценой."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    await _open_and_confirm(dp, bot, first, user)
    await session.refresh(first)
    assert first.trade_opened_at is not None

    second = await _notify(session, signal)  # тот же fingerprint
    tickers_before = len(client.ticker_retries_seen)
    await _feed(dp, bot, 3, make_callback(f"exn:open:{second.id}", message_id=3))

    assert (user.id, second.id) not in execution._confirmations
    assert len(client.ticker_retries_seen) == tickers_before
    [row] = await _orders_for_notification(session, second.id)
    assert row.status is OrderStatus.REFUSED
    assert row.error_code == "SETUP_ALREADY_TRADED"
    assert any("По этому сетапу уже открывали сделку" in t for t in bot.recorder.sent_texts())


def _reject_entry(client) -> None:  # type: ignore[no-untyped-def]
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_error = ExchangeResponseError(
        "BingX: отказ (код 80001)", code=80001, payload={"code": 80001}
    )


async def test_rejected_entry_does_not_burn_setup_for_next_notification(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Решение А (раздел 8): биржа явно отклонила вход по №1 — позиции нет,
    уведомление №2 того же слота и fingerprint открывает карточку без
    SETUP_ALREADY_TRADED. Отметка на №1 остаётся."""
    dp, session, user, client, _redis, settings = ctx
    _reject_entry(client)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    await _open_and_confirm(dp, bot, first, user)
    await session.refresh(first)
    assert first.trade_opened_at is not None
    [entry] = await _orders_for_notification(session, first.id)
    assert entry.status is OrderStatus.REJECTED

    second = await _notify(session, signal)  # тот же fingerprint
    await _feed(dp, bot, 3, make_callback(f"exn:open:{second.id}", message_id=3))

    assert (user.id, second.id) in execution._confirmations
    assert await _orders_for_notification(session, second.id) == []
    assert not any("уже открывали сделку" in t for t in bot.recorder.sent_texts())


async def test_unknown_entry_still_burns_setup_for_next_notification(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """UNKNOWN по №1 — ордер мог пройти: №2 того же сетапа получает
    SETUP_ALREADY_TRADED, как до решения А."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_error = ExchangeUnavailableError("BingX не ответил вовремя")
    client.fill_error = ExchangeResponseError("order not exist", code=109414, payload=None)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    await _open_and_confirm(dp, bot, first, user)
    entries = [
        o for o in await _orders_for_notification(session, first.id)
        if o.role is OrderRole.ENTRY
    ]
    assert [o.status for o in entries] == [OrderStatus.UNKNOWN]

    second = await _notify(session, signal)
    await _feed(dp, bot, 3, make_callback(f"exn:open:{second.id}", message_id=3))

    assert (user.id, second.id) not in execution._confirmations
    [row] = await _orders_for_notification(session, second.id)
    assert row.error_code == "SETUP_ALREADY_TRADED"


async def test_pending_entry_still_burns_setup_for_next_notification(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """PENDING по №1 (процесс упал между коммитом PENDING и ответом биржи,
    раздел 8) — исход неизвестен: №2 того же сетапа получает
    SETUP_ALREADY_TRADED. Состояние собрано вручную: хендлер PENDING не
    оставляет."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal, trade_opened_at=NOW)
    session.add(ExecutionOrder(
        user_id=user.id, signal_id=signal.id, notification_id=first.id,
        client_order_id=f"tj{first.id}u{user.id}E", symbol="BTC-USDT",
        side=OrderSide.BUY, position_side=TradeSide.LONG, order_type=OrderType.MARKET,
        role=OrderRole.ENTRY, status=OrderStatus.PENDING,
    ))
    await session.flush()

    second = await _notify(session, signal)
    await _feed(dp, bot, 3, make_callback(f"exn:open:{second.id}", message_id=3))

    assert (user.id, second.id) not in execution._confirmations
    [row] = await _orders_for_notification(session, second.id)
    assert row.error_code == "SETUP_ALREADY_TRADED"


async def test_repeat_on_rejected_notification_says_exchange_rejected(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Повторная попытка по №1 после REJECTED: SIGNAL_ALREADY_USED с текстом
    про отказ биржи, не «уже открывали сделку»; второго ордера нет — только
    строка-наблюдение REFUSED, как у любого отказа гварда (раздел 12а)."""
    dp, session, user, client, _redis, settings = ctx
    _reject_entry(client)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    await _open_and_confirm(dp, bot, first, user)

    await _feed(dp, bot, 3, make_callback(f"exn:open:{first.id}", message_id=3))

    assert (user.id, first.id) not in execution._confirmations
    assert any(
        "Биржа уже отклонила вход по этому сигналу. "
        "Повторить нельзя — дождись следующего уведомления." in t
        for t in bot.recorder.sent_texts()
    )
    assert not any("уже открывали сделку" in t for t in bot.recorder.sent_texts())
    rows = await _orders_for_notification(session, first.id)
    orders = [r for r in rows if r.client_order_id is not None]
    assert [(o.role, o.status) for o in orders] == [(OrderRole.ENTRY, OrderStatus.REJECTED)]
    refused = [r for r in rows if r.status is OrderStatus.REFUSED]
    assert [r.error_code for r in refused] == ["SIGNAL_ALREADY_USED"]
    assert len(rows) == 2
    assert [c[0] for c in client.submit_calls].count("place_market_order") == 1


async def test_parallel_yes_on_two_notifications_of_same_setup_one_wins(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Р2: два уведомления одного сетапа, обе карточки открыты, два «Да»
    параллельно в разных сессиях. FOR NO KEY UPDATE на слоте сериализует
    их: второй дожидается коммита первого и видит его trade_opened_at —
    ровно один вход, второй — SETUP_ALREADY_TRADED."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    first = await _notify(session, signal)
    second = await _notify(session, signal)  # тот же fingerprint
    await session.commit()

    await _feed(dp, bot, 1, make_callback(f"exn:open:{first.id}", message_id=1))
    await _feed(dp, bot, 2, make_callback(f"exn:open:{second.id}", message_id=2))
    state_first = execution._confirmations[(user.id, first.id)]
    state_second = execution._confirmations[(user.id, second.id)]
    await session.commit()  # отпустить транзакцию общей сессии теста

    # Оба процесса прочитали свои снимки до того, как кто-то взял слот.
    barrier = asyncio.Barrier(2)
    original_get = execution.SignalNotificationRepository.get

    async def synced_get(self, nid: int, uid: int):  # type: ignore[no-untyped-def]
        result = await original_get(self, nid, uid)
        await barrier.wait()
        return result

    monkeypatch.setattr(execution.SignalNotificationRepository, "get", synced_get)

    db = Database(settings)
    try:
        async def run_confirm(nid: int, message_id: int) -> None:
            async with db.session() as own_session:
                callback = make_bound_callback(bot, f"exn:yes:{nid}", message_id=message_id)
                await execution._process_confirm(
                    callback, own_session, user, nid, settings, None, db
                )

        await asyncio.wait_for(
            asyncio.gather(
                run_confirm(first.id, state_first.message_id),
                run_confirm(second.id, state_second.message_id),
            ),
            timeout=30,
        )
    finally:
        await db.dispose()

    rows = await _orders_for_signal(session, signal.id)
    dry_run = [r for r in rows if r.status is OrderStatus.DRY_RUN]
    refused = [r for r in rows if r.status is OrderStatus.REFUSED]
    assert len(dry_run) == 3  # одна тройка вход/стоп/тейк
    assert len({r.notification_id for r in dry_run}) == 1
    assert [r.error_code for r in refused] == ["SETUP_ALREADY_TRADED"]
    assert refused[0].notification_id != dry_run[0].notification_id


# ---------------------------------------------------------------------------
# Шаг 15.5.3: read-back входа после отправки
# ---------------------------------------------------------------------------

_READBACK_CALLS = ("get_order_fill", "get_open_orders", "place_conditional_order")


def _readback_calls(client: FakeExchangeClient) -> list[str]:
    return [c[0] for c in client.submit_calls if c[0] in _READBACK_CALLS]


async def test_dry_run_makes_no_readback_calls(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """exec_dry_run=True — ни одного вызова read-back: сухой прогон ничего
    не отправляет, читать нечего."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    assert settings.exec_dry_run is True

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await _open_and_confirm(dp, bot, notification, user)

    assert _readback_calls(client) == []
    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert "⏳ Ордер отправлен, проверяю исполнение…" not in edits


async def test_callback_answered_before_first_exchange_call_on_yes(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Telegram ждёт ответ на callback ~15 с, а путь «Да» с read-back — до
    TTL лока (173 с): callback.answer() обязан уйти до первого HTTP к бирже."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await _feed(dp, bot, 1, make_callback(f"exn:open:{notification.id}", message_id=1))
    state = execution._confirmations[(user.id, notification.id)]

    yes_callback = make_callback(f"exn:yes:{notification.id}", message_id=state.message_id)
    answered_before_ticker: list[bool] = []
    original_get_ticker = client.get_ticker

    async def spying_get_ticker(symbol, *, max_retries=None):  # type: ignore[no-untyped-def]
        answered_before_ticker.append(any(
            isinstance(m, AnswerCallbackQuery) and m.callback_query_id == yes_callback.id
            for m in bot.recorder.calls
        ))
        return await original_get_ticker(symbol, max_retries=max_retries)

    monkeypatch.setattr(client, "get_ticker", spying_get_ticker)
    await _feed(dp, bot, 2, yes_callback)

    assert answered_before_ticker == [True]


async def test_real_order_intermediate_then_readback_result_same_message(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Сразу после отправки — «⏳ Ордер отправлен, проверяю исполнение…»,
    итог read-back редактирует то же сообщение: цена против карточки
    (проскальзывание словами), объём, комиссия, id стопа и тейка."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result(order_id="555555")
    client.fill_overrides = {"avgPrice": "100.3"}  # карточка на 100
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await _open_and_confirm(dp, bot, notification, user)

    card_message_id = next(
        m.message_id for m in bot.recorder.calls
        if isinstance(m, EditMessageText) and m.text == "⏳ Ордер отправлен, проверяю исполнение…"
    )
    same_message = [
        m.text for m in bot.recorder.calls
        if isinstance(m, EditMessageText) and m.message_id == card_message_id
    ]
    assert same_message[0] == "⏳ Ордер отправлен, проверяю исполнение…"
    final = same_message[-1]
    assert final.startswith("✅ Вход исполнен: BTC-USDT")
    assert "Цена: 100.3 (карточка 100, проскальзывание в худшую сторону 0.3%" in final
    assert "комиссия 0.05 USDT" in final
    assert "Стоп: 97 (id 8001)" in final
    assert "Тейк: 110 (id 8002)" in final


async def test_failed_stop_rescue_alarm_sent_as_separate_message(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    """Стоп не прикрепился, спасение не удалось — тревога и в итоге, и
    отдельным сообщением: правка сообщения в Telegram не даёт уведомления."""
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result()
    client.attach_conditionals = False
    client.conditional_error = ExchangeResponseError("отказ", code=80012, payload=None)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await _open_and_confirm(dp, bot, notification, user)

    alarms = [t for t in bot.recorder.sent_texts() if t.startswith("⚠️ ПОЗИЦИЯ БЕЗ СТОПА")]
    assert len(alarms) == 1
    assert "BTC-USDT LONG" in alarms[0]
    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert any("⚠️ ПОЗИЦИЯ БЕЗ СТОПА" in t and "Стоп: 97 (НЕ ВЫСТАВЛЕН)" in t for t in edits)
    # Спасение — по одной попытке на стоп и тейк, повторов нет.
    assert _readback_calls(client).count("place_conditional_order") == 2


@pytest.mark.parametrize(
    "side,avg,expected",
    [
        (TradeSide.LONG, "100.3", "проскальзывание в худшую сторону 0.3% (0.23 USDT)"),
        (TradeSide.LONG, "99.7", "проскальзывание в лучшую сторону 0.3% (0.23 USDT)"),
        (TradeSide.SHORT, "99.7", "проскальзывание в худшую сторону 0.3% (0.23 USDT)"),
        (TradeSide.SHORT, "100.3", "проскальзывание в лучшую сторону 0.3% (0.23 USDT)"),
        (TradeSide.LONG, "100", "без проскальзывания"),
        (TradeSide.SHORT, "100", "без проскальзывания"),
    ],
)
def test_slippage_described_in_words_by_trade_side(side, avg, expected) -> None:  # type: ignore[no-untyped-def]
    """Сторона — по направлению сделки: LONG хуже, если купил дороже
    карточки; SHORT хуже, если продал дешевле. Словами, не знаком."""
    text = execution.describe_slippage(
        side=side, planned_price=D("100"), avg_price=D(avg), quantity=D("0.75")
    )
    assert text == expected


# ---------------------------------------------------------------------------
# Шаг 15.5.4: сделка в журнале после read-back
# ---------------------------------------------------------------------------


async def _user_trades(session, user_id: int) -> list:  # type: ignore[no-untyped-def]
    return await TradeRepository(session).list_open(user_id)


async def _real_confirm(ctx, bot, monkeypatch, **client_attrs):  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    client.current_leverage = _leverage_info(long_leverage=10)
    client.place_order_result = _order_result(order_id="555555")
    for name, value in client_attrs.items():
        setattr(client, name, value)
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    settings.exec_dry_run = False
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)
    await _open_and_confirm(dp, bot, notification, user)
    edits = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    return session, user, notification, edits


async def test_filled_entry_writes_trade_and_reports_it(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, notification, edits = await _real_confirm(ctx, bot, monkeypatch)

    [trade] = await _user_trades(session, user.id)
    assert trade.notification_id == notification.id
    assert trade.fill_confirmed is True
    assert any(t.endswith(f"📒 Сделка #{trade.id} в журнале") for t in edits)


async def test_unknown_not_found_writes_provisional_trade_with_cancel_hint(  # type: ignore[no-untyped-def]
    ctx, bot, monkeypatch
) -> None:
    session, user, _n, edits = await _real_confirm(
        ctx, bot, monkeypatch,
        place_order_error=ExchangeUnavailableError("timeout"),
        fill_error=ExchangeResponseError("order not exist", code=109414, payload=None),
    )

    [trade] = await _user_trades(session, user.id)
    assert trade.fill_confirmed is False
    assert any(
        f"📒 Сделка #{trade.id} в журнале — предварительно. Если позиции на BingX нет — "
        f"отмени сделку #{trade.id} в журнале, иначе символ останется заблокирован для "
        "новых входов" in t
        for t in edits
    )


async def test_unconfirmed_fill_writes_provisional_trade(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, _n, edits = await _real_confirm(
        ctx, bot, monkeypatch, fill_overrides={"avgPrice": None}
    )

    [trade] = await _user_trades(session, user.id)
    assert trade.fill_confirmed is False
    assert any(
        t.endswith(f"📒 Сделка #{trade.id} в журнале — предварительно, исполнение не подтверждено")
        for t in edits
    )


async def test_dry_run_writes_no_trade(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    assert settings.exec_dry_run is True
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    notification = await _notify(session, signal)

    await _open_and_confirm(dp, bot, notification, user)

    assert await _user_trades(session, user.id) == []


async def test_entry_past_stop_alarm_is_separate_message(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, _n, _edits = await _real_confirm(
        ctx, bot, monkeypatch, fill_overrides={"avgPrice": "96.5"}
    )

    alarms = [
        t for t in bot.recorder.sent_texts() if t.startswith("⚠️ Вход исполнен за уровнем стопа")
    ]
    assert alarms == [
        "⚠️ Вход исполнен за уровнем стопа: BTC-USDT LONG, цена 96.5, стоп 97. "
        "Убыток больше заявленного — проверь позицию в BingX"
    ]
    [trade] = await _user_trades(session, user.id)
    assert trade.stop_loss is None
