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
    ExchangeClient,
    ExchangeUnavailableError,
    SymbolInfo,
    Ticker,
)
from app.services.user_service import UserService
from app.trading.enums import ExchangeKeyMode, OrderRole, OrderStatus, SignalDirection, SignalLevel
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

    async def get_ticker(self, symbol: str) -> Ticker:
        return Ticker(
            symbol=symbol, last_price=self.price, volume_24h=D("0"),
            price_change_percent=D("0"), timestamp=NOW,
        )

    async def get_klines(self, symbol, interval, limit=500, end_time=None):
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

    async def set(self, name, value, *, nx=False, ex=None):  # type: ignore[no-untyped-def]
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
    await session.refresh(signal)
    assert signal.trade_opened_at is None

    # Новая карточка отправлена (снова "Да, открыть" где-то в текстах) —
    # это следующее сообщение с кнопками, а не тихий вход.
    assert (user.id, signal.id) in execution._confirmations
    new_state = execution._confirmations[(user.id, signal.id)]
    assert new_state.planned_price == D("101")
