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
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import (
    ApiRestrictions,
    Balance,
    ExchangeAuthError,
    ExchangeClient,
    ExchangeError,
    ExchangeUnavailableError,
    SymbolInfo,
    Ticker,
)
from app.services.user_service import UserService
from app.trading.enums import (
    ExchangeKeyMode,
    ObservationStage,
    OrderRole,
    OrderStatus,
    SignalDirection,
    SignalLevel,
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
    ) -> None:
        self.price = price
        self.balance = balance
        self.symbol_info = symbol_info
        self.restrictions = restrictions
        self.restrictions_error = restrictions_error
        # Раздел 8 ТЗ: подтверждение (стадия CONFIRM) обязано звать эти
        # методы с max_retries=1, карточка (CARD) — без ограничения.
        self.ticker_retries_seen: list[int | None] = []
        self.balance_retries_seen: list[int | None] = []
        self.symbols_retries_seen: list[int | None] = []

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

    async def get_positions(self):
        return []

    async def get_api_restrictions(self) -> ApiRestrictions:
        if self.restrictions_error is not None:
            raise self.restrictions_error
        if self.restrictions is None:
            raise NotImplementedError
        return self.restrictions

    async def get_fills(self, start_time, end_time, symbol=None):
        return []

    async def set_leverage(self, symbol, leverage, *, position_side=None):
        raise NotImplementedError

    async def place_market_order(self, **kwargs):
        raise NotImplementedError

    async def get_order(self, symbol, client_order_id):
        raise NotImplementedError

    async def get_open_orders(self, symbol=None):
        raise NotImplementedError

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
        min_quantity=D("0.001"), max_leverage=50, min_notional=D("5"),
    )


def _restrictions(**overrides: object) -> ApiRestrictions:
    fields: dict[str, object] = {
        "ip_restrict": True, "create_time": NOW, "permits_universal_transfer": False,
        "enable_reading": True, "enable_futures": True, "enable_spot_and_margin_trading": False,
    }
    fields.update(overrides)
    return ApiRestrictions(**fields)  # type: ignore[arg-type]


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
    settings = Settings(trading_execution_enabled=True, bingx_trading_mode="live")  # type: ignore[call-arg]
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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    text = texts[0]
    assert "BTC-USDT" in text
    assert "Стоп: 97" in text
    assert "Тейк: 110" in text
    assert (user.id, signal.id) in execution._confirmations


async def test_open_button_shows_refusal_when_execution_disabled(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    settings.trading_execution_enabled = False  # выключаем после сборки dp — тот же объект
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" in texts[0]
    assert (user.id, signal.id) not in execution._confirmations

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не удалось проверить права ключа" in texts[0]
    assert (user.id, signal.id) not in execution._confirmations

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" not in texts[0]
    assert "BTC-USDT" in texts[0]
    assert (user.id, signal.id) in execution._confirmations

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "реальном счёте" in texts[0]
    assert (user.id, signal.id) not in execution._confirmations

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
    client.price = D("106")

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1
    assert "Не открыл" in texts[0]
    assert "устарел" in texts[0]
    assert (user.id, signal.id) not in execution._confirmations

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    await _feed(
        dp, bot, 2,
        make_callback(f"exec:yes:{signal.id}", message_id=state.message_id),
    )

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 3
    assert {o.role for o in orders} == {OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}
    assert all(o.status is OrderStatus.DRY_RUN for o in orders)

    await session.refresh(signal)
    assert signal.trade_opened_at is not None

    dry_run_texts = [t for t in bot.recorder.sent_texts() if "Сухой прогон" in t]
    assert len(dry_run_texts) == 1
    assert (user.id, signal.id) not in execution._confirmations


async def test_confirm_yes_lock_ttl_comes_from_settings(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: TTL лока — settings.confirm_lock_ttl_seconds, не
    литерал 15."""
    dp, session, user, client, redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    await _feed(
        dp, bot, 2,
        make_callback(f"exec:yes:{signal.id}", message_id=state.message_id),
    )

    lock_calls = [
        ex for name, ex in redis.set_calls if name == confirm_lock_key(user.id, signal.id)
    ]
    assert lock_calls == [settings.confirm_lock_ttl_seconds]
    assert settings.confirm_lock_ttl_seconds != 15


async def test_confirm_no_cancels_without_orders(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    await _feed(dp, bot, 2, make_callback(f"exec:no:{signal.id}", message_id=state.message_id))

    # Раздел 12а ТЗ: карточка была показана и отклонена пользователем — не
    # DRY_RUN (нет тройки вход/стоп/тейк), а одна строка-наблюдение DECLINED
    # с цифрами карточки, без client_order_id (нечего отправлять на биржу).
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.DECLINED
    assert orders[0].role is OrderRole.ENTRY
    assert orders[0].client_order_id is None
    assert orders[0].quantity is not None
    await session.refresh(signal)
    assert signal.trade_opened_at is None
    assert (user.id, signal.id) not in execution._confirmations


async def test_confirm_yes_busy_lock_answers_and_creates_nothing(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Раздел 8 ТЗ: занятый ключ — мгновенный отказ, без ожидания."""
    dp, session, user, client, redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    redis.store[confirm_lock_key(user.id, signal.id)] = "someone-elses-token"

    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

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
    сессиями на один сигнал — как в проде два разных апдейта получают
    каждый свою сессию.

    Какая из двух защит поймает конкретно эту гонку — не детерминировано
    (реальные асинхронные round-trip'ы к Postgres, порядок переключения
    корутин не фиксирован): либо UNIQUE на client_order_id (проигравший
    падает на flush строк заказа, откатывается по SAVEPOINT, ничего не
    оставляет), либо гвард SIGNAL_ALREADY_USED, если signal.trade_opened_at
    победителя успел закоммититься раньше, чем проигравший его прочитал
    (тогда проигравший получает REFUSED-строку с этим кодом). Важен не
    конкретный путь, а инвариант: ровно одна тройка DRY_RUN, второй попытке
    пройти нельзя, необработанных исключений нет."""
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()
    await session.commit()  # видно другим сессиям/соединениям

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    db = Database(settings)
    try:
        async def run_confirm() -> None:
            async with db.session() as own_session:
                callback = make_bound_callback(
                    bot, f"exec:yes:{signal.id}", message_id=state.message_id
                )
                await execution._process_confirm(
                    callback, own_session, user, signal.id, settings, None, db
                )

        # asyncio.gather без RedisLock: оба процесса реально параллельны,
        # как если бы каждый уже прошёл (или в обход) проверку лока.
        await asyncio.gather(run_confirm(), run_confirm())
    finally:
        await db.dispose()

    orders = await _orders_for_signal(session, signal.id)
    dry_run = [o for o in orders if o.status is OrderStatus.DRY_RUN]
    other = [o for o in orders if o.status is not OrderStatus.DRY_RUN]

    assert len(dry_run) == 3, "должна остаться ровно одна тройка DRY_RUN"
    assert {o.role for o in dry_run} == {
        OrderRole.ENTRY, OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT,
    }

    # Проигравший либо вообще не оставляет строк (поймало UNIQUE на
    # client_order_id — SAVEPOINT откатил вставку), либо оставляет ровно
    # одну REFUSED-строку с кодом SIGNAL_ALREADY_USED (поймал гвард) —
    # см. докстринг выше. Второй тройки DRY_RUN и любого другого исхода
    # быть не должно.
    assert len(other) <= 1
    if other:
        assert other[0].status is OrderStatus.REFUSED
        assert other[0].error_code == "SIGNAL_ALREADY_USED"

    edit_texts = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    success_texts = [t for t in edit_texts if t and "Подтверждено" in t]
    race_texts = [
        t for t in edit_texts
        if t and ("уже обрабатывается" in t or "уже открывали сделку" in t)
    ]
    assert len(success_texts) == 1
    assert len(race_texts) == 1

    await session.refresh(signal)
    assert signal.trade_opened_at is not None


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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    async def fake_flush(self) -> None:  # type: ignore[no-untyped-def]
        raise _fake_integrity_error("fk_execution_orders_user_id_users")

    from app.database.repositories.execution_order import ExecutionOrderRepository

    monkeypatch.setattr(ExecutionOrderRepository, "flush", fake_flush)

    callback = make_bound_callback(bot, f"exec:yes:{signal.id}", message_id=state.message_id)
    with pytest.raises(IntegrityError):
        await execution._process_confirm(
            callback, session, user, signal.id, settings, None, dp["db"]
        )

    edit_texts = [m.text for m in bot.recorder.calls if isinstance(m, EditMessageText)]
    assert not any(t and "уже обрабатывается" in t for t in edit_texts)


async def test_confirm_yes_after_ttl_shows_expired(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]
    state.created_at = datetime.now(UTC) - timedelta(seconds=settings.exec_confirm_ttl_seconds + 1)

    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

    # Раздел 12а ТЗ: "Да" пришло позже TTL — считаем карточку истёкшей и
    # логируем EXPIRED, а не молча проглатываем попытку.
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.EXPIRED
    assert (user.id, signal.id) not in execution._confirmations
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
    await session.commit()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    assert (user.id, signal.id) in execution._confirmations

    tasks = list(execution._background_tasks)
    assert len(tasks) == 1
    await tasks[0]

    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.EXPIRED
    assert (user.id, signal.id) not in execution._confirmations
    edits = [m for m in bot.recorder.calls if isinstance(m, EditMessageReplyMarkup)]
    assert len(edits) == 1


async def test_confirm_yes_price_drift_sends_recalculated_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))

    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]

    # Дрейф 1 > 0.3 * 3 (дистанция до стопа 97) — за границей допустимого,
    # но RR по новой цене (101→110 против стопа 97) всё ещё ≥ EXEC_MIN_RR,
    # так что пересчитанная карточка обязана уйти успешно, а не отказом.
    client.price = D("101")

    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

    # Раздел 12а ТЗ: сам PRICE_DRIFT-отказ на "Да" — это тоже отказ гварда,
    # ExecutionService.evaluate() пишет его сам (REFUSED). Тройки DRY_RUN
    # при этом всё ещё нет — вход не состоялся, только пересчёт карточки.
    orders = await _orders_for_signal(session, signal.id)
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REFUSED
    assert orders[0].error_code == "PRICE_DRIFT"
    assert orders[0].stage == ObservationStage.CONFIRM
    await session.refresh(signal)
    assert signal.trade_opened_at is None

    # Новая карточка отправлена (снова "Да, открыть" где-то в текстах) —
    # это следующее сообщение с кнопками, а не тихий вход.
    assert (user.id, signal.id) in execution._confirmations
    new_state = execution._confirmations[(user.id, signal.id)]
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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]
    settings.trading_execution_enabled = False

    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]
    client.price = D("101")
    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.error_code == "PRICE_DRIFT"
    assert row.stage == ObservationStage.CONFIRM


async def test_auth_error_on_card_is_recorded_as_error_card(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dp, session, user, client, _redis, _settings = ctx
    _patch_factory_auth_error(monkeypatch, client, ExchangeAuthError("ключ отозван"))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))
    state = execution._confirmations[(user.id, signal.id)]
    _make_ticker_fail(monkeypatch, client, ExchangeUnavailableError("timeout"))

    await _feed(dp, bot, 2, make_callback(f"exec:yes:{signal.id}", message_id=state.message_id))

    (row,) = await _orders_for_signal(session, signal.id)
    assert row.status is OrderStatus.ERROR
    assert row.stage == ObservationStage.CONFIRM
    assert (user.id, signal.id) not in execution._confirmations
    await session.refresh(signal)
    assert signal.trade_opened_at is None


async def test_error_row_never_stores_exception_text(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """В str(exc) может быть тело ответа биржи (одна из ручек эхом отдаёт
    apiKey): в БД — только класс исключения."""
    dp, session, user, client, _redis, _settings = ctx
    _patch_exchange_factory(monkeypatch, client, FakeCredentials(is_read_only=False))
    _make_ticker_fail(monkeypatch, client, ExchangeError("body: apiKey=SECRET-KEY-123"))
    signal = _signal(user.id)
    session.add(signal)
    await session.flush()

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

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

    await _feed(dp, bot, 1, make_callback(f"exec:open:{signal.id}", message_id=1))

    texts = bot.recorder.sent_texts()
    assert len(texts) == 1 and "ключ отозван" in texts[0]
    assert await _orders_for_signal(session, signal.id) == []
