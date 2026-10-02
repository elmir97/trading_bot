"""Экран «💼 Позиции» (этап 3, app/bot/handlers/positions.py): позиции с
биржи со стопом/тейком из openOrders, сделки только из журнала с закрытием,
кнопка «📥 В журнал» — импорт по символу. Биржа, репозиторий и импорт
подменены — без сети и БД."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers import positions as screen
from app.bot.handlers.positions import PositionsCB
from app.bot.keyboards.main import MenuCallback
from app.bot.keyboards.trade import TradeCB
from app.database.models.trade import Trade
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeUnavailableError,
    OpenOrder,
    Position,
    SymbolInfo,
)
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource, TradeStatus

D = Decimal
NOW = datetime(2026, 10, 2, 6, 30, tzinfo=UTC)


def _position(symbol: str = "XRP-USDT") -> Position:
    return Position(
        symbol=symbol, side=TradeSide.LONG, quantity=D(30), entry_price=D("1.5253"),
        mark_price=D("1.5263"), leverage=20, unrealized_pnl=D("0.03"),
        liquidation_price=D("1.4553"), position_id="2105907655281221634",
    )


def _stop() -> OpenOrder:
    return OpenOrder(
        order_id="2105910661355233280", client_order_id="", symbol="XRP-USDT", side="SELL",
        position_side="LONG", order_type="STOP_MARKET", quantity=D(40), executed_qty=D(0),
        price=D(0), stop_price=D("1.5241"), status="NEW", leverage=20, reduce_only=True,
        close_position=True, working_type="MARK_PRICE", created_at=NOW, updated_at=NOW,
        take_profit=None, stop_loss=None,
    )


def _trade(trade_id: int, symbol: str) -> Trade:
    return Trade(
        id=trade_id, user_id=7, symbol=symbol, side=TradeSide.LONG, entry_price=D(100),
        quantity=D(1), status=TradeStatus.OPEN, source=TradeSource.MANUAL,
        opened_at=NOW - timedelta(days=1),
    )


class _Client:
    def __init__(self, positions: list[Position], orders: list[OpenOrder],
                 error: Exception | None = None) -> None:
        self._positions, self._orders, self._error = positions, orders, error
        self.closed = False
        self.open_orders_symbols: list[str | None] = []
        self.name = "bingx"

    async def get_positions(self, *, max_retries=None) -> list[Position]:  # type: ignore[no-untyped-def]
        if self._error:
            raise self._error
        return self._positions

    async def get_open_orders(self, symbol=None, *, max_retries=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.open_orders_symbols.append(symbol)
        return self._orders

    async def get_symbols(self, *, max_retries=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
        return [SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def env(monkeypatch):  # type: ignore[no-untyped-def]
    state = SimpleNamespace(client=None, auth_error=False, trades=[], imports=[])

    class Factory:
        def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
            pass

        async def for_user(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
            if state.auth_error:
                raise ExchangeAuthError("Ключи BingX не подключены.")
            return state.client

    class Importer:
        def __init__(self, client, trades, user_id) -> None:  # type: ignore[no-untyped-def]
            pass

        async def import_period(self, start, end, account_balance=None, *, symbol=None):  # type: ignore[no-untyped-def]
            state.imports.append((end - start, symbol))
            return SimpleNamespace(errors=[], trades_created=1, render=lambda: "")

    async def list_open(self, user_id, limit=50):  # type: ignore[no-untyped-def]
        return list(state.trades)

    monkeypatch.setattr(screen, "ExchangeFactory", Factory)
    monkeypatch.setattr(screen, "HistoryImporter", Importer)
    monkeypatch.setattr(TradeRepository, "list_open", list_open)
    screen._market_cache.invalidate("symbols")
    return state


def _message() -> MagicMock:
    message = MagicMock(spec=Message)
    message.photo = None
    for name in ("edit_text", "answer", "delete"):
        setattr(message, name, AsyncMock())
    return message


def _callback(data: str = MenuCallback.OPEN_POSITIONS) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = _message()
    callback.answer = AsyncMock()
    return callback


def _user() -> SimpleNamespace:
    settings = SimpleNamespace(active_exchange_mode=ExchangeKeyMode.DEMO)
    return SimpleNamespace(id=7, settings=settings)


async def _open(env, data: str = MenuCallback.OPEN_POSITIONS):  # type: ignore[no-untyped-def]
    callback = _callback(data)
    importing = data.startswith(PositionsCB.IMPORT)
    handler = screen.import_position if importing else screen.show_positions
    await handler(callback, MagicMock(), _user(), settings=None, cipher=None)  # type: ignore[arg-type]
    text = callback.message.edit_text.await_args.args[0]
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    return text, buttons


async def test_exchange_position_with_close_position_stop(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [_stop()])
    text, buttons = await _open(env)
    assert "<b>💼 Позиции · " in text
    assert "XRP-USDT</b> LONG · 30" in text
    assert "Стоп: 1.5241 (на всю позицию)" in text and "Тейк: нет" in text
    assert "⚠️ Не в журнале" in text
    assert buttons["📥 В журнал: XRP LONG"] == f"{PositionsCB.IMPORT}XRP-USDT"
    assert env.client.open_orders_symbols == [None]  # один запрос на все символы
    assert env.client.closed


async def test_linked_and_journal_only_trades(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    linked = _trade(12, "XRP-USDT")
    manual = _trade(13, "ETH-USDT")
    env.trades = [linked, manual]
    text, buttons = await _open(env)
    assert "📒 В журнале: сделка #12" in text
    assert "<b>Только в журнале</b>" in text and "📒 #13 ETH-USDT LONG" in text
    assert buttons["ETH-USDT LONG #13"] == f"{TradeCB.CLOSE}13"
    assert not any(t.startswith("📥") for t in buttons)


async def test_no_keys_shows_journal_only(env) -> None:  # type: ignore[no-untyped-def]
    env.auth_error = True
    env.trades = [_trade(13, "ETH-USDT")]
    text, buttons = await _open(env)
    assert "Ключи биржи не подключены" in text
    assert "📒 #13 ETH-USDT LONG" in text and f"{TradeCB.CLOSE}13" in buttons.values()


async def test_exchange_error_keeps_journal(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [], error=ExchangeUnavailableError("down"))
    env.trades = [_trade(13, "XRP-USDT")]
    text, _ = await _open(env)
    assert "Биржа не отвечает" in text and "📒 #13 XRP-USDT LONG" in text
    assert env.client.closed


async def test_empty(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [])
    text, buttons = await _open(env)
    assert "На бирже открытых позиций нет." in text
    assert set(buttons.values()) == {PositionsCB.REFRESH, MenuCallback.MAIN}


async def test_import_button_imports_that_symbol(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT")
    assert env.imports == [(timedelta(days=screen.IMPORT_DAYS), "XRP-USDT")]
    assert "📥 XRP-USDT: сделок создано — 1" in text


async def test_exchange_menu_button_opens_same_screen(env) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers.exchange import ExchangeCB

    env.client = _Client([], [])
    text, _ = await _open(env, ExchangeCB.POSITIONS)
    assert "<b>💼 Позиции · " in text
