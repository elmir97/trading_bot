"""Экран «💼 Позиции» (этап 3, app/bot/handlers/positions.py): позиции с
биржи со стопом/тейком из openOrders, сделки только из журнала с закрытием,
кнопка «⚙️ XRP LONG» — экран действий позиции, «📥 В журнал» — только текущая
позиция. Биржа, репозиторий и импорт подменены — без сети и БД."""

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


def _position(symbol: str = "XRP-USDT", side: TradeSide = TradeSide.LONG) -> Position:
    return Position(
        symbol=symbol, side=side, quantity=D(30), entry_price=D("1.5253"),
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
    state = SimpleNamespace(
        client=None, auth_error=False, trades=[], imports=[],
        import_outcome=SimpleNamespace(trade=SimpleNamespace(id=21), refusal=None),
    )

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

        async def import_open_position(self, start, end, *, symbol, side, quantity):  # type: ignore[no-untyped-def]
            state.imports.append((end - start, symbol, side, quantity))
            return state.import_outcome

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
    if data.startswith(PositionsCB.IMPORT):
        handler = screen.import_position
    elif data.startswith(PositionsCB.ACTIONS):
        handler = screen.show_actions
    else:
        handler = screen.show_positions
    await handler(callback, MagicMock(), _user(), settings=None, cipher=None)  # type: ignore[arg-type]
    text = callback.message.edit_text.await_args.args[0]
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    env.rows = [[b.text for b in row] for row in keyboard.inline_keyboard]
    return text, buttons


async def test_exchange_position_with_close_position_stop(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [_stop()])
    text, buttons = await _open(env)
    assert "<b>💼 Позиции · " in text
    assert "XRP-USDT</b> LONG · 30" in text
    assert "Стоп: 1.5241 (на всю позицию)" in text and "Тейк: нет" in text
    assert "⚠️ Не в журнале" in text
    assert buttons["📥 В журнал: XRP LONG"] == f"{PositionsCB.IMPORT}XRP-USDT:L"
    assert buttons["⚙️ XRP LONG"] == f"{PositionsCB.ACTIONS}XRP-USDT:L"
    assert env.rows[0] == ["⚙️ XRP LONG", "📥 В журнал: XRP LONG"]
    # Действия — на экране позиции, не в списке.
    assert not any(data.startswith("pa:") for data in buttons.values())
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


async def test_several_positions_one_row_each(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position(), _position("BTC-USDT"), _position(side=TradeSide.SHORT)], [])
    env.trades = [_trade(12, "BTC-USDT")]
    _, buttons = await _open(env)
    assert sorted(env.rows[:3]) == sorted([
        ["⚙️ XRP LONG", "📥 В журнал: XRP LONG"],
        ["⚙️ BTC LONG"],                      # в журнале — без «В журнал»
        ["⚙️ XRP SHORT", "📥 В журнал: XRP SHORT"],
    ])
    assert buttons["⚙️ XRP SHORT"] == f"{PositionsCB.ACTIONS}XRP-USDT:S"


async def test_actions_screen_full_labels_two_per_row(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position(), _position(side=TradeSide.SHORT)], [_stop()])
    text, buttons = await _open(env, f"{PositionsCB.ACTIONS}XRP-USDT:L")
    assert "<b>⚙️ Действия · XRP-USDT LONG</b>" in text
    assert "Стоп: 1.5241 (на всю позицию)" in text
    assert env.rows == [
        ["🛡 Стоп в безубыток", "✏️ Изменить стоп"],
        ["🎯 Тейк", "✂️ Закрыть 25%"],
        ["✂️ Закрыть 50%", "❌ Закрыть всё"],
        ["◀️ К позициям"],
    ]
    assert buttons["🛡 Стоп в безубыток"] == "pa:be:XRP-USDT:L"
    assert buttons["❌ Закрыть всё"] == "pa:cf:XRP-USDT:L"
    assert buttons["◀️ К позициям"] == PositionsCB.REFRESH


async def test_actions_screen_position_gone_back_to_list(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [])
    text, _ = await _open(env, f"{PositionsCB.ACTIONS}XRP-USDT:L")
    assert "Позиции XRP-USDT LONG на бирже уже нет." in text
    assert "<b>💼 Позиции · " in text


async def test_import_button_imports_current_position_only(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert env.imports == [
        (timedelta(days=screen.IMPORT_DAYS), "XRP-USDT", TradeSide.LONG, D(30))
    ]
    assert "📥 XRP-USDT LONG: в журнале — сделка #21." in text


async def test_import_refusal_points_to_full_import(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.import_outcome = SimpleNamespace(trade=None, refusal="Объём не сходится.")
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert "📥 XRP-USDT LONG: Объём не сходится. Историю целиком — /import." in text


async def test_import_position_gone_imports_nothing(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [])
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert env.imports == []
    assert "позиции на бирже уже нет" in text


async def test_exchange_menu_button_opens_same_screen(env) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers.exchange import ExchangeCB

    env.client = _Client([], [])
    text, _ = await _open(env, ExchangeCB.POSITIONS)
    assert "<b>💼 Позиции · " in text
