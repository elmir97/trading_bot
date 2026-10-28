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
from app.bot.keyboards.positions import IMPORT_ORPHAN, orphan_import_data
from app.bot.keyboards.trade import TradeCB
from app.database.models.trade import Trade
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import (
    Balance,
    ExchangeAuthError,
    ExchangeUnavailableError,
    OpenOrder,
    Position,
    SymbolInfo,
)
from app.services.import_service import apply_import_levels
from app.trading.enums import ExchangeKeyMode, TradeSide, TradeSource, TradeStatus
from tests.test_locks import FakeRedis

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


def _trade(
    trade_id: int, symbol: str, account_mode: ExchangeKeyMode | None = ExchangeKeyMode.DEMO,
) -> Trade:
    return Trade(
        id=trade_id, user_id=7, symbol=symbol, side=TradeSide.LONG, entry_price=D(100),
        quantity=D(1), status=TradeStatus.OPEN,
        source=TradeSource.MANUAL if account_mode is None else TradeSource.IMPORTED,
        account_mode=account_mode, opened_at=NOW - timedelta(days=1),
    )


class _Client:
    def __init__(self, positions: list[Position], orders: list[OpenOrder],
                 error: Exception | None = None) -> None:
        self._positions, self._orders, self._error = positions, orders, error
        self.orders_error: Exception | None = None
        self.closed = False
        self.balance_error: Exception | None = None
        self.open_orders_symbols: list[str | None] = []
        self.name = "bingx"

    async def get_positions(self, *, max_attempts=None) -> list[Position]:  # type: ignore[no-untyped-def]
        if self._error:
            raise self._error
        return self._positions

    async def get_open_orders(self, symbol=None, *, max_attempts=None) -> list[OpenOrder]:  # type: ignore[no-untyped-def]
        self.open_orders_symbols.append(symbol)
        if self.orders_error is not None:
            raise self.orders_error
        return self._orders

    async def get_symbols(self, *, max_attempts=None) -> list[SymbolInfo]:  # type: ignore[no-untyped-def]
        return [SymbolInfo("XRP-USDT", 4, 0, D(2), D(2))]

    async def get_balance(self, *, max_attempts=None) -> Balance:  # type: ignore[no-untyped-def]
        if self.balance_error is not None:
            raise self.balance_error
        return Balance("USDT", D(9000), D(100), D(5), D(10000))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def env(monkeypatch):  # type: ignore[no-untyped-def]
    state = SimpleNamespace(
        client=None, auth_error=False, trades=[], imports=[], import_modes=[],
        import_cutoffs=[], import_balances=[], import_levels=[], first_stop=None,
        import_outcome=SimpleNamespace(trade=True, refusal=None),
        slow=None, records=[],
    )

    class Factory:
        def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
            pass

        async def for_user(self, session, user_id, exchange="bingx", mode=None):  # type: ignore[no-untyped-def]
            if state.auth_error:
                raise ExchangeAuthError("Ключи BingX не подключены.")
            return state.client

    class Importer:
        def __init__(self, client, trades, user_id, *, account_mode,  # type: ignore[no-untyped-def]
                     journal_cutoff=None, tz_offset=0) -> None:
            state.import_modes.append(account_mode)
            state.import_cutoffs.append((journal_cutoff, tz_offset))

        async def import_open_position(self, start, end, *, symbol, side, quantity,  # type: ignore[no-untyped-def]
                                       position_id=None, account_balance=None,
                                       stops=(), takes=()):
            state.imports.append((end - start, symbol, side, quantity, position_id))
            state.import_balances.append(account_balance)
            state.import_levels.append((stops, takes))
            outcome = await state.slow() if state.slow else state.import_outcome
            if outcome.trade is not True:
                return outcome
            # Уровни — настоящим правилом импорта (К3), начальный стоп по
            # действиям бота — state.first_stop.
            trade = Trade(id=21, user_id=7, symbol=symbol, side=side,
                          entry_price=D("1.5253"), quantity=quantity)
            apply_import_levels(trade, stops, takes, state.first_stop)
            return SimpleNamespace(trade=trade, refusal=None)

    async def record(db, meta, *, chat_id, message_id, text):  # type: ignore[no-untyped-def]
        state.records.append((meta.kind, meta.trade_id, chat_id, message_id, text))

    async def list_open(self, user_id, limit=50):  # type: ignore[no-untyped-def]
        return list(state.trades)

    async def opening_in_flight(session, user_id, symbol, side):  # type: ignore[no-untyped-def]
        return (symbol, side) in state.openings

    state.openings = set()
    monkeypatch.setattr(screen, "ExchangeFactory", Factory)
    monkeypatch.setattr(screen, "HistoryImporter", Importer)
    monkeypatch.setattr(screen, "opening_in_flight", opening_in_flight)
    monkeypatch.setattr(screen.outbox, "record", record)
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
    settings = SimpleNamespace(
        active_exchange_mode=ExchangeKeyMode.DEMO, journal_cutoff_at=CUTOFF,
        timezone="Asia/Yekaterinburg",
    )
    return SimpleNamespace(id=7, settings=settings)


CUTOFF = datetime(2026, 10, 3, 20, 38, 34, tzinfo=UTC)


async def _open(env, data: str = MenuCallback.OPEN_POSITIONS):  # type: ignore[no-untyped-def]
    callback = _callback(data)
    if data.startswith(PositionsCB.IMPORT):
        await screen.import_position(
            callback, MagicMock(), _user(), settings=None, cipher=None,  # type: ignore[arg-type]
            redis=FakeRedis(),
        )
    else:
        handler = screen.show_actions if data.startswith(PositionsCB.ACTIONS) else (
            screen.show_positions
        )
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
    # positionId живой позиции — связь для reconciler после её закрытия (02.10, #12).
    assert env.imports == [
        (timedelta(days=screen.IMPORT_DAYS), "XRP-USDT", TradeSide.LONG, D(30),
         "2105907655281221634")
    ]
    assert "📥 XRP-USDT LONG: в журнале — сделка #21. Стопа на бирже нет" in text
    # 03.10.2026: счёт из настроек пишется в сделку — лимиты по своему счёту.
    assert env.import_modes == [ExchangeKeyMode.DEMO]
    # M4: отсечка журнала и пояс пользователя передаются импортёру.
    assert env.import_cutoffs == [(CUTOFF, 5)]


async def test_import_refused_while_opening_in_flight(env) -> None:  # type: ignore[no-untyped-def]
    """05.10.2026: позицию открывает бот (лимит исполнен частично) — «В журнал»
    отказывает, иначе сделка задвоится."""
    env.client = _Client([_position()], [])
    env.openings = {("XRP-USDT", TradeSide.LONG)}
    callback = _callback(f"{PositionsCB.IMPORT}XRP-USDT:L")
    await screen.import_position(
        callback, MagicMock(), _user(), settings=None, cipher=None,  # type: ignore[arg-type]
        redis=FakeRedis(),
    )
    assert env.imports == []
    args, kwargs = callback.answer.call_args
    assert "открывается из бота" in args[0] and kwargs.get("show_alert") is True


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


async def test_manual_trade_without_account_not_linked(env) -> None:  # type: ignore[no-untyped-def]
    """03.10.2026, блокер live Б2: ручная запись без счёта по символу позиции
    не становится «В журнале» — позиция «Не в журнале», запись — в «Только в
    журнале»."""
    env.client = _Client([_position()], [])
    env.trades = [_trade(14, "XRP-USDT", account_mode=None)]
    text, buttons = await _open(env)
    assert "⚠️ Не в журнале" in text
    assert "📒 #14 XRP-USDT LONG" in text and buttons["XRP-USDT LONG #14"] == f"{TradeCB.CLOSE}14"


async def test_import_writes_entry_balance_without_unrealized_pnl(env) -> None:  # type: ignore[no-untyped-def]
    """03.10.2026, блокер live Б1: баланс на входе = equity 10000 − PnL этой
    позиции 0.03."""
    env.client = _Client([_position()], [])
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert env.import_balances == [D("9999.97")]
    assert "Баланс счёта не получен" not in text


async def test_import_without_balance_marked_and_warned(env, caplog) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.client.balance_error = ExchangeUnavailableError("down")
    with caplog.at_level("WARNING"):
        text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert env.import_balances == [None]
    assert "тейка на бирже нет. ⚠️ Баланс счёта не получен" in text
    assert any("баланс счёта не получен" in r.getMessage() for r in caplog.records)



async def test_partial_stop_shown_with_quantity_and_import_button(env) -> None:  # type: ignore[no-untyped-def]
    """08.10.2026: ручная позиция со стопом «на часть позиции» (не closePosition) —
    экран пишет объём ордера, не «на всю позицию»; «В журнал» доступна."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    partial_stop = OpenOrder(
        order_id="77", client_order_id="", symbol="XRP-USDT", side="SELL", position_side="LONG",
        order_type="STOP_MARKET", quantity=D(30), executed_qty=D(0), price=D(0),
        stop_price=D("1.4795"), status="NEW", leverage=20, reduce_only=True,
        close_position=False, working_type="MARK_PRICE", created_at=now, updated_at=now,
        take_profit=None, stop_loss=None,
    )
    env.client = _Client([_position()], [partial_stop])
    text, buttons = await _open(env)
    assert "Стоп: 1.4795 (30)" in text and "1.4795 (на всю позицию)" not in text
    assert any("В журнал" in b for b in buttons)


# --- /start pos-… — ссылка «⚙️ Действия в чате» из Mini App (П.1) ------------------------


def _command(args: str | None) -> SimpleNamespace:
    return SimpleNamespace(args=args)


async def _start(env, args: str | None) -> MagicMock:  # type: ignore[no-untyped-def]
    from app.bot.handlers import common

    message = _message()
    state = SimpleNamespace(clear=AsyncMock())
    await common.cmd_start(message, _user(), state, _command(args), MagicMock(),  # type: ignore[arg-type]
                           settings=None, cipher=None)  # type: ignore[arg-type]
    state.clear.assert_awaited_once()
    return message


async def test_start_link_opens_position_actions(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position(), _position(side=TradeSide.SHORT)], [_stop()])
    message = await _start(env, "pos-XRP-USDT-S")
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "<b>⚙️ Действия · XRP-USDT SHORT</b>" in text
    buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert buttons["❌ Закрыть всё"] == "pa:cf:XRP-USDT:S"


async def test_start_link_position_gone(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [])
    message = await _start(env, "pos-XRP-USDT-L")
    text = message.answer.await_args.args[0]
    assert "Позиции XRP-USDT LONG на бирже уже нет." in text and "<b>💼 Позиции · " in text


@pytest.mark.parametrize("args", [
    None, "", "pos-", "pos-XRP-USDT", "pos-XRP-USDT-X", "pos-xrp-usdt-L", "pos-XRP-USDC-L",
    "pos-XRP-USDT-L-1", "pos-XRP-USDT-LS", "x pos-XRP-USDT-L", "pos-XRP_USDT-L", "abc",
    "pos-" + "A" * 21 + "-USDT-L",
])
async def test_start_garbage_shows_menu_without_exchange(env, monkeypatch, args) -> None:  # type: ignore[no-untyped-def]
    from app.bot.handlers import common

    class NoExchange:
        def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("к бирже ходить нельзя")

    monkeypatch.setattr(screen, "ExchangeFactory", NoExchange)
    message = await _start(env, args)
    assert message.answer.await_args.args[0] == common.GREETING


def test_start_param_roundtrip() -> None:
    from app.execution.position_view import parse_start_param, start_param

    for symbol in ("XRP-USDT", "1000PEPE-USDT", "BTC-USDT"):
        for side in TradeSide:
            param = start_param(symbol, side)
            assert len(param) <= 64 and parse_start_param(param) == (symbol, side)
    assert start_param("XRP-USDT", TradeSide.LONG) == "pos-XRP-USDT-L"


# --- п.3 (24.10): «📥 В журнал» в сообщении сверки ORPHAN_POSITION ------------------------

ORPHAN_TEXT = (
    "⚠️ Сверка с биржей, XRP-USDT: позиция LONG 30 по 1.5253 на бирже, сделки в журнале нет"
)
IMPORT_ROW = [
    ("📥 В журнал", f"{IMPORT_ORPHAN}XRP-USDT:L"), ("💼 Позиции", MenuCallback.OPEN_POSITIONS),
]
DONE_ROW = [("💼 Позиции", MenuCallback.OPEN_POSITIONS)]
# К3: итог вместо текста сверки («…журнал не изменён» ему противоречил).
DONE_TEXT = (
    "✅ XRP-USDT LONG: позиция занесена в журнал — сделка #21.\n"
    "Стопа на бирже нет · тейка на бирже нет."
)


def _orphan_callback(
    data: str = f"{IMPORT_ORPHAN}XRP-USDT:L", text: str = ORPHAN_TEXT
) -> MagicMock:
    callback = _callback(data)
    callback.message.html_text = text
    callback.message.chat = SimpleNamespace(id=424242)
    callback.message.message_id = 555
    callback.from_user = SimpleNamespace(id=424242)
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock()
    return callback


async def _orphan(env, callback=None, redis=None):  # type: ignore[no-untyped-def]
    callback = callback or _orphan_callback()
    await screen.import_orphan_position(
        callback, MagicMock(), _user(), settings=None, cipher=None,  # type: ignore[arg-type]
        redis=redis or FakeRedis(), db=None,  # type: ignore[arg-type]
    )
    return callback


def _rows_of(markup) -> list[list[tuple[str, str]]]:  # type: ignore[no-untyped-def]
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _edited(callback) -> tuple[str, list[list[tuple[str, str]]]]:  # type: ignore[no-untyped-def]
    args, kwargs = callback.message.edit_text.await_args
    return args[0], _rows_of(kwargs["reply_markup"])


def _bad_request(text: str):  # type: ignore[no-untyped-def]
    from aiogram.exceptions import TelegramBadRequest

    return AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message=text))


async def test_orphan_import_appends_result_and_keeps_positions_button(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    callback = await _orphan(env)
    assert env.imports == [
        (timedelta(days=screen.IMPORT_DAYS), "XRP-USDT", TradeSide.LONG, D(30),
         "2105907655281221634")
    ]
    assert _edited(callback) == (DONE_TEXT, [DONE_ROW])
    callback.bot.send_message.assert_not_awaited()


async def test_orphan_import_refusal_appended_button_kept(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.import_outcome = SimpleNamespace(trade=None, refusal="Объём <не> сходится.")
    callback = await _orphan(env)
    text, rows = _edited(callback)
    assert text.startswith(
        f"{ORPHAN_TEXT}\n\n📥 Не занесено: XRP-USDT LONG: Объём &lt;не&gt; сходится."
    )
    assert rows == [IMPORT_ROW]
    callback.bot.send_message.assert_not_awaited()


async def test_orphan_repeated_refusal_replaces_previous_reason(env) -> None:  # type: ignore[no-untyped-def]
    """Второй отказ подряд — прежняя причина заменяется, не копится; успех
    после отказа — причина тоже убрана."""
    refused = f"{ORPHAN_TEXT}\n\n📥 Не занесено: старое"
    env.client = _Client([], [])
    callback = await _orphan(env, _orphan_callback(text=refused))
    text, rows = _edited(callback)
    assert text.count("Не занесено") == 1 and "старое" not in text
    assert "позиции на бирже уже нет" in text and rows == [IMPORT_ROW]

    env.client = _Client([_position()], [])
    callback = await _orphan(env, _orphan_callback(text=refused))
    assert _edited(callback) == (DONE_TEXT, [DONE_ROW])


async def test_orphan_import_position_gone_refused(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([], [])
    callback = await _orphan(env)
    assert env.imports == []
    text, rows = _edited(callback)
    assert "позиции на бирже уже нет" in text and rows == [IMPORT_ROW]


async def test_orphan_import_edit_failed_result_as_new_message(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    callback = _orphan_callback()
    callback.message.edit_text = _bad_request("message can't be edited")
    await _orphan(env, callback)
    args, kwargs = callback.bot.send_message.await_args
    assert args == (424242, DONE_TEXT)
    assert _rows_of(kwargs["reply_markup"]) == [DONE_ROW]


async def test_orphan_refusal_edit_failed_as_new_message_with_button(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.import_outcome = SimpleNamespace(trade=None, refusal="Объём не сходится.")
    callback = _orphan_callback()
    callback.message.edit_text = _bad_request("message can't be edited")
    await _orphan(env, callback)
    args, kwargs = callback.bot.send_message.await_args
    assert args[0] == 424242 and "Не занесено" in args[1] and "Объём не сходится." in args[1]
    assert _rows_of(kwargs["reply_markup"]) == [IMPORT_ROW]


async def _until(cond) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    while not cond():
        await asyncio.sleep(0)


async def test_orphan_double_tap_imports_once(env) -> None:  # type: ignore[no-untyped-def]
    """Два нажатия разом — один импорт: лок позиции, второе — отказ под
    текстом, кнопка остаётся; первое — успех."""
    import asyncio

    env.client = _Client([_position()], [])
    gate = asyncio.Event()
    real = env.import_outcome

    async def slow_outcome():  # type: ignore[no-untyped-def]
        await gate.wait()
        return real

    env.slow = slow_outcome
    redis = FakeRedis()
    first, second = _orphan_callback(), _orphan_callback()
    task = asyncio.create_task(_orphan(env, first, redis))
    await asyncio.wait_for(_until(lambda: env.imports), 5)
    # без лока второе нажатие тоже ждёт gate — таймаут, а не зависание теста
    await asyncio.wait_for(_orphan(env, second, redis), 5)
    gate.set()
    await asyncio.wait_for(task, 5)
    assert len(env.imports) == 1
    text, rows = _edited(second)
    assert "уже идёт действие" in text and rows == [IMPORT_ROW]
    assert _edited(first) == (DONE_TEXT, [DONE_ROW])


async def test_orphan_import_busy_position_lock_refused(env) -> None:  # type: ignore[no-untyped-def]
    """Идёт действие этапа 4 с позицией (exec:lock) — импорт не начинается."""
    from app.core.locks import position_lock_key

    env.client = _Client([_position()], [])
    redis = FakeRedis()
    await redis.set(position_lock_key(7, "XRP-USDT", "LONG"), "x")
    callback = await _orphan(env, redis=redis)
    assert env.imports == []
    text, rows = _edited(callback)
    assert "уже идёт действие" in text and rows == [IMPORT_ROW]


@pytest.mark.parametrize("side", [TradeSide.LONG, TradeSide.SHORT])
def test_orphan_callback_data_fits_and_parses(side: TradeSide) -> None:
    data = orphan_import_data("1000PEPE-USDT", side)
    assert len(data.encode()) <= 64
    assert screen._parse_ref(data, IMPORT_ORPHAN) == ("1000PEPE-USDT", side)
    assert not data.startswith(PositionsCB.IMPORT)   # не перехватит кнопка экрана «Позиции»


async def test_orphan_stale_tap_still_edits(env) -> None:  # type: ignore[no-untyped-def]
    """Нажатие устарело, пока шли запросы к бирже (answer падает) — итог
    всё равно дописан к сообщению, и успех, и отказ."""
    for outcome in (env.import_outcome, SimpleNamespace(trade=None, refusal="Объём не сходится.")):
        env.client = _Client([_position()], [])
        env.import_outcome = outcome
        callback = _orphan_callback()
        callback.answer = _bad_request("query is too old")
        await _orphan(env, callback)
        callback.message.edit_text.assert_awaited_once()
        callback.bot.send_message.assert_not_awaited()


async def test_orphan_import_refused_while_opening_in_flight(env) -> None:  # type: ignore[no-untyped-def]
    """Общие проверки «В журнал» и у кнопки сверки: идёт открытие — отказ."""
    env.client = _Client([_position()], [])
    env.openings = {("XRP-USDT", TradeSide.LONG)}
    callback = await _orphan(env)
    assert env.imports == []
    text, rows = _edited(callback)
    assert "открывается из бота" in text and rows == [IMPORT_ROW]


async def test_stale_orphan_button_refused() -> None:
    callback = _orphan_callback(f"{IMPORT_ORPHAN}XRP-USDT:X")
    await screen.import_orphan_position(
        callback, MagicMock(), _user(), settings=None, cipher=None,  # type: ignore[arg-type]
        redis=FakeRedis(), db=None,  # type: ignore[arg-type]
    )
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_awaited()


# --- К3 (27.10): уровни из openOrders того же снимка, итог ORPHAN, outgoing_messages ------


def _take(price: str = "1.60") -> OpenOrder:
    from dataclasses import replace

    return replace(_stop(), order_id="2105910661355233281", order_type="TAKE_PROFIT_MARKET",
                   stop_price=D(price))


async def test_import_passes_levels_from_same_snapshot(env) -> None:  # type: ignore[no-untyped-def]
    """openOrders — под тем же локом, сразу после позиций, по символу; стоп и
    тейк уходят импортёру, итог называет их."""
    env.client = _Client([_position()], [_stop(), _take()])
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    [(stops, takes)] = env.import_levels
    assert [s.trigger_price for s in stops] == [D("1.5241")]
    assert [t.trigger_price for t in takes] == [D("1.60")]
    assert env.client.open_orders_symbols[0] == "XRP-USDT"   # дальше — экран заново
    # формат — как у экрана (fmt_price; хвостовые нули — К4)
    assert "сделка #21. Стоп 1.5241 · тейк 1.6000." in text


async def test_import_refused_when_open_orders_unavailable(env) -> None:  # type: ignore[no-untyped-def]
    """К3, владелец 27.10: openOrders не получен — отказ, сделки нет (иначе
    начальный стоп поставила бы сверка позже и R исказился бы)."""
    env.client = _Client([_position()], [_stop()])
    env.client.orders_error = ExchangeUnavailableError("down")
    text, _ = await _open(env, f"{PositionsCB.IMPORT}XRP-USDT:L")
    assert env.imports == []
    assert f"📥 XRP-USDT LONG: {screen.NO_LEVELS}" in text
    assert env.client.closed


async def test_orphan_open_orders_unavailable_refused_button_kept(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.client.orders_error = ExchangeUnavailableError("down")
    callback = await _orphan(env)
    assert env.imports == []
    text, rows = _edited(callback)
    assert text.startswith(f"{ORPHAN_TEXT}\n\n📥 Не занесено: XRP-USDT LONG: Не удалось получить")
    assert rows == [IMPORT_ROW]


async def test_orphan_done_text_ladder_and_initial_from_actions(env) -> None:  # type: ignore[no-untyped-def]
    """Лестница стопов — стоп не занесён (правило сверки), начальный стоп — по
    действиям бота; он не в убытке — «R не определён»."""
    from dataclasses import replace

    env.client = _Client([_position()], [_stop(), replace(_stop(), order_id="9",
                                                          stop_price=D("1.50"))])
    env.first_stop = D("1.5300")
    callback = await _orphan(env)
    text, _ = _edited(callback)
    assert text == (
        "✅ XRP-USDT LONG: позиция занесена в журнал — сделка #21.\n"
        "Стопов на бирже 2 — в журнал не занесён · тейка на бирже нет. "
        "Начальный стоп 1.5300 — по действиям бота. "
        "R не определён: начальный стоп не в убытке."
    )


async def test_orphan_edit_recorded_in_outgoing_messages(env) -> None:  # type: ignore[no-untyped-def]
    """Правка сообщения сверки пишется в outgoing_messages тем же видом и тем
    же message_id (правка, прежний текст — в edits), с trade_id."""
    env.client = _Client([_position()], [])
    await _orphan(env)
    assert env.records == [("RECON_ORPHAN_POSITION", 21, 424242, 555, DONE_TEXT)]


async def test_orphan_refusal_edit_recorded_without_trade(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    env.import_outcome = SimpleNamespace(trade=None, refusal="Объём не сходится.")
    await _orphan(env)
    [(kind, trade_id, chat_id, message_id, text)] = env.records
    assert (kind, trade_id, chat_id, message_id) == ("RECON_ORPHAN_POSITION", None, 424242, 555)
    assert "Не занесено" in text


async def test_orphan_new_message_recorded_when_edit_failed(env) -> None:  # type: ignore[no-untyped-def]
    env.client = _Client([_position()], [])
    callback = _orphan_callback()
    callback.message.edit_text = _bad_request("message can't be edited")
    callback.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(chat=SimpleNamespace(id=424242), message_id=777)
    )
    await _orphan(env, callback)
    assert env.records == [("RECON_ORPHAN_POSITION", 21, 424242, 777, DONE_TEXT)]
