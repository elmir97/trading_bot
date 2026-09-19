"""app/bot/handlers/exchange.py — except Exception → except ExchangeError
в пяти местах (_with_exchange, show_prices, run_import, check_symbols,
fix_symbols).

Раньше эти пять try/except ловили ЛЮБОЕ исключение и превращали его в
дружелюбный, но неверный текст "не удалось получить данные с биржи" через
_describe() — так тихо прожил F821 (fmt_price не был импортирован, см.
_render_open_order). Теперь ловится только ExchangeError и наследники;
всё остальное пробрасывается наверх — в проде это поймает ErrorMiddleware
(app/bot/middlewares/errors.py) и покажет «Произошла внутренняя ошибка»,
а не соврёт про биржу.

Проверяем на трёх представительных местах (show_balance — общий код
_with_exchange, используемый ещё show_exchange_positions/show_exchange_orders;
check_symbols — тот же паттерн, что и в fix_symbols; run_import):
  1) не-ExchangeError (ValueError) не ловится хендлером, пробрасывается;
  2) ExchangeError по-прежнему даёт прежний дружелюбный текст через _describe.

show_prices и fix_symbols не дублирую отдельными тестами: show_prices
вообще не может получить исключение изнутри try (MarketDataService.
get_prices сама глотает всё на уровне символа, app/market/data.py:95-108),
а fix_symbols — тот же код, что и check_symbols, один в один.

Через настоящий Dispatcher aiogram, как test_execution_handlers.py:
важно, что реально ловит/не ловит хендлер, а не имитация. ExchangeFactory
подменена на фейковую — на BingX ничего не уходит.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.handlers import exchange
from app.bot.handlers.exchange import ExchangeCB
from app.core.config import Settings
from app.database.repositories.strategy import MistakeTypeRepository, StrategyRepository
from app.database.repositories.user import UserRepository
from app.database.session import Database
from app.exchanges.base import ExchangeUnavailableError
from app.services.import_service import HistoryImporter
from app.services.user_service import UserService
from tests.conftest import cleanup_user

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

CHAT_ID = 606
USER_TG_ID = 909
BOT_ID = 111


class FakeExchangeClient:
    """Ровно те методы, что реально вызывают проверяемые хендлеры."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.get_balance_raises: Exception | None = None
        self.get_symbols_raises: Exception | None = None

    async def get_balance(self):  # type: ignore[no-untyped-def]
        if self.get_balance_raises is not None:
            raise self.get_balance_raises
        return SimpleNamespace(
            asset="USDT", available=D("900"), equity=D("1000"),
            used_margin=D("0"), unrealized_pnl=D("0"),
        )

    async def get_symbols(self):  # type: ignore[no-untyped-def]
        if self.get_symbols_raises is not None:
            raise self.get_symbols_raises
        return []

    async def close(self) -> None:
        return None


def _patch_exchange_factory(monkeypatch, client: FakeExchangeClient) -> None:
    class FakeFactory:
        def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
            pass

        async def for_user(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
            return client

        def public_client(self):  # type: ignore[no-untyped-def]
            return client

    monkeypatch.setattr(exchange, "ExchangeFactory", FakeFactory)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[TelegramMethod] = []
        self._next_message_id = 20_000

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
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id or self._message_id(), date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"), text=method.text or "",
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    def last_edited_text(self) -> str | None:
        edits = [m.text for m in self.calls if isinstance(m, EditMessageText)]
        return edits[-1] if edits else None


@pytest_asyncio.fixture
async def bot(monkeypatch):  # type: ignore[no-untyped-def]
    recorder = Recorder()

    async def fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return await recorder(self, method, request_timeout)

    monkeypatch.setattr(Bot, "__call__", fake_call)
    instance = Bot(token=f"{BOT_ID}:AAFakeTokenForExchangeTests")
    instance.recorder = recorder  # type: ignore[attr-defined]
    yield instance
    await instance.session.close()


@pytest_asyncio.fixture
async def ctx(unique_telegram_id):  # type: ignore[no-untyped-def]
    settings = Settings(trading_execution_enabled=True)  # type: ignore[call-arg]
    db = Database(settings)
    async with db.session() as session:
        user_service = UserService(
            UserRepository(session), StrategyRepository(session),
            MistakeTypeRepository(session), settings,
        )
        user = await user_service.get_or_create(telegram_id=unique_telegram_id())
        yield session, user, settings
        await cleanup_user(session, user)
    await db.dispose()


def _make_callback(data: str) -> CallbackQuery:
    message = Message(
        message_id=1, date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="private"), text="card",
    )
    return CallbackQuery(
        id="1", from_user=TgUser(id=USER_TG_ID, is_bot=False, first_name="Tester"),
        chat_instance="ci", data=data, message=message,
    )


def _router_with(*handlers_and_filters) -> Router:  # type: ignore[no-untyped-def]
    """Свежий Router на тест: тот же handler-объект нельзя навесить на два
    Dispatcher-а подряд (см. test_execution_handlers.py::_fresh_execution_router)."""
    r = Router(name="exchange-test")
    for handler, data_filter in handlers_and_filters:
        r.callback_query.register(handler, data_filter)
    return r


async def _feed(dp: Dispatcher, bot: Bot, callback: CallbackQuery) -> None:
    await dp.feed_update(bot=bot, update=Update(update_id=1, callback_query=callback))


def _dispatcher(router: Router, session, user, settings) -> Dispatcher:  # type: ignore[no-untyped-def]
    dp = Dispatcher(storage=MemoryStorage())
    dp["session"] = session
    dp["user"] = user
    dp["settings"] = settings
    dp["cipher"] = None
    dp.include_router(router)
    return dp


# ---------------------------------------------------------------------------
# _with_exchange (show_balance/show_exchange_positions/show_exchange_orders)
# ---------------------------------------------------------------------------


async def test_show_balance_value_error_propagates(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-balance-valueerror")
    client.get_balance_raises = ValueError("баг рендера, не биржи")
    _patch_exchange_factory(monkeypatch, client)

    router = _router_with((exchange.show_balance, F.data == ExchangeCB.BALANCE))
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(ExchangeCB.BALANCE)

    with pytest.raises(ValueError, match="баг рендера"):
        await _feed(dp, bot, callback)


async def test_show_balance_exchange_error_shows_friendly_text(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-balance-exchangeerror")
    client.get_balance_raises = ExchangeUnavailableError("BingX не ответил вовремя")
    _patch_exchange_factory(monkeypatch, client)

    router = _router_with((exchange.show_balance, F.data == ExchangeCB.BALANCE))
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(ExchangeCB.BALANCE)

    await _feed(dp, bot, callback)

    text = bot.recorder.last_edited_text()
    assert text is not None and "биржа не отвечает" in text.lower()


# ---------------------------------------------------------------------------
# check_symbols (тот же паттерн, что и fix_symbols)
# ---------------------------------------------------------------------------


async def test_check_symbols_value_error_propagates(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-symbols-valueerror")
    client.get_symbols_raises = ValueError("баг парсинга, не биржи")
    _patch_exchange_factory(monkeypatch, client)

    router = _router_with((exchange.check_symbols, F.data == ExchangeCB.CHECK_SYMBOLS))
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(ExchangeCB.CHECK_SYMBOLS)

    with pytest.raises(ValueError, match="баг парсинга"):
        await _feed(dp, bot, callback)


async def test_check_symbols_exchange_error_shows_friendly_text(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-symbols-exchangeerror")
    client.get_symbols_raises = ExchangeUnavailableError("BingX не ответил вовремя")
    _patch_exchange_factory(monkeypatch, client)

    router = _router_with((exchange.check_symbols, F.data == ExchangeCB.CHECK_SYMBOLS))
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(ExchangeCB.CHECK_SYMBOLS)

    await _feed(dp, bot, callback)

    text = bot.recorder.last_edited_text()
    assert text is not None and "биржа не отвечает" in text.lower()


# ---------------------------------------------------------------------------
# run_import (HistoryImporter.import_period)
# ---------------------------------------------------------------------------


async def test_run_import_value_error_propagates(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-import-valueerror")
    _patch_exchange_factory(monkeypatch, client)

    async def broken_import_period(self, start, end, account_balance=None):  # type: ignore[no-untyped-def]
        raise ValueError("баг группировки сделок, не биржи")

    monkeypatch.setattr(HistoryImporter, "import_period", broken_import_period)

    router = _router_with(
        (exchange.run_import, F.data.startswith(ExchangeCB.IMPORT_CONFIRM))
    )
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(f"{ExchangeCB.IMPORT_CONFIRM}7")

    with pytest.raises(ValueError, match="баг группировки"):
        await _feed(dp, bot, callback)


async def test_run_import_exchange_error_shows_friendly_text(ctx, bot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session, user, settings = ctx
    client = FakeExchangeClient(name="fake-import-exchangeerror")
    _patch_exchange_factory(monkeypatch, client)

    async def broken_import_period(self, start, end, account_balance=None):  # type: ignore[no-untyped-def]
        raise ExchangeUnavailableError("BingX не ответил вовремя")

    monkeypatch.setattr(HistoryImporter, "import_period", broken_import_period)

    router = _router_with(
        (exchange.run_import, F.data.startswith(ExchangeCB.IMPORT_CONFIRM))
    )
    dp = _dispatcher(router, session, user, settings)
    callback = _make_callback(f"{ExchangeCB.IMPORT_CONFIRM}7")

    await _feed(dp, bot, callback)

    text = bot.recorder.last_edited_text()
    assert text is not None and "биржа не отвечает" in text.lower()
