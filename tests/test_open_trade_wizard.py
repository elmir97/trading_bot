"""Чат-мастер «Открыть на бирже» (app/bot/handlers/open_trade.py): поиск
монеты по контрактам BingX, клавиатуры карточки и итога, переписка мастера.
Полный проход по экранам — smoke_check, сценарий [16]."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot.handlers import open_trade
from app.bot.handlers.open_trade import (
    OpenCB,
    card_keyboard,
    result_keyboard,
    search_contracts,
)
from app.bot.wizard_trail import _in_wizard
from app.core.config import Settings
from app.exchanges.base import SymbolInfo
from app.execution.opening.service import ConfirmOutcome
from app.trading.enums import OpeningStatus, TradeSide

D = Decimal
CONTRACTS = [
    SymbolInfo(s, 4, 0, D(1), D(2))
    for s in ("XRP-USDT", "ETH-USDT", "ETHFI-USDT", "PEPE-USDT", "1000PEPE-USDT",
              "DOGE-USDT", "BTC-USDT", "BTC-USDC")
]


@pytest.fixture
def contracts(monkeypatch):  # type: ignore[no-untyped-def]
    class Market:
        def __init__(self, client, cache) -> None:  # type: ignore[no-untyped-def]
            pass

        async def get_symbols(self) -> list[SymbolInfo]:
            return CONTRACTS

    class Client:
        async def close(self) -> None: ...

    class Factory:
        def __init__(self, settings, cipher) -> None:  # type: ignore[no-untyped-def]
            pass

        def public_client(self) -> Client:
            return Client()

    monkeypatch.setattr(open_trade, "MarketDataService", Market)
    monkeypatch.setattr(open_trade, "ExchangeFactory", Factory)


def _settings() -> Settings:
    return Settings(bot_token="1:x", database_url="postgresql+asyncpg://u:p@h/d",  # type: ignore[call-arg]
                    encryption_key="0" * 43 + "=")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("eth", ["ETH-USDT"]),                      # точное — одно
        ("ETH-USDT", ["ETH-USDT"]),
        ("ethusdt", ["ETH-USDT"]),
        ("xrp/usdt", ["XRP-USDT"]),
        ("PEP", ["PEPE-USDT", "1000PEPE-USDT"]),     # начало, затем вхождение
        ("btc", ["BTC-USDT"]),                      # только USDT-контракты
        ("zzz", []),
        ("  ", []),
    ],
)
async def test_search_contracts(contracts, query: str, expected: list[str]) -> None:  # type: ignore[no-untyped-def]
    found = await search_contracts(_settings(), query)
    assert [s.symbol for s in found] == expected


def _labels(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.text for row in markup.inline_keyboard for b in row]


def test_card_keyboard_variants() -> None:
    assert _labels(card_keyboard(7, can_open=True, warnings=False))[0] == "✅ Открыть"
    warn = card_keyboard(7, can_open=True, warnings=True)
    assert _labels(warn)[0] == "⚠️ Открыть всё равно"
    assert warn.inline_keyboard[0][0].callback_data == f"{OpenCB.YES_WARN}7"
    blocked = _labels(card_keyboard(7, can_open=False, warnings=False))
    assert "✅ Открыть" not in blocked and blocked == ["✏️ Изменить", "✖️ Отмена"]


def test_result_keyboard() -> None:
    done = result_keyboard(ConfirmOutcome("ok", OpeningStatus.DONE, 31), 7, "XRP-USDT",
                           TradeSide.LONG)
    assert done is not None and done.inline_keyboard[0][0].callback_data == "pos:act:XRP-USDT:L"
    working = result_keyboard(ConfirmOutcome("⏳", OpeningStatus.WORKING), 7, "XRP-USDT",
                              TradeSide.LONG)
    assert working is not None
    assert working.inline_keyboard[0][0].callback_data == f"{OpenCB.CANCEL_LIMIT}7"
    refused = result_keyboard(ConfirmOutcome("⛔", OpeningStatus.REFUSED), 7, "XRP-USDT",
                              TradeSide.LONG)
    assert refused is not None and _labels(refused)[0] == "🔄 Пересчитать"
    assert result_keyboard(ConfirmOutcome("🧪", OpeningStatus.DRY_RUN), 7, "X", TradeSide.LONG) \
        is None


def test_callback_prefixes_do_not_overlap() -> None:
    """«Отмена» (ot:n:) не перехватывает «Без тейка» (ot:notake)."""
    assert not OpenCB.NO_TAKE.startswith(OpenCB.NO)
    data = [v for k, v in vars(OpenCB).items() if k.isupper()]
    assert all(len(d.encode()) + 12 <= 64 for d in data)   # + id не превысит лимит Telegram


def test_open_wizard_is_part_of_trail() -> None:
    assert _in_wizard("OpenTradeStates:leverage")
    assert _in_wizard("AddTradeStates:symbol")
    assert not _in_wizard("CloseTradeStates:exit_price")
    assert not _in_wizard(None)


def test_emergency_closed_keyboard_has_no_position_button() -> None:
    """A.1 (Т2): под аварийным закрытием — «К позициям / В меню»; «Позиция»
    вела на экран «уже нет» и затирала итог."""
    from app.bot.handlers.open_trade import opening_keyboard
    from app.bot.keyboards.main import MenuCallback

    closed = result_keyboard(ConfirmOutcome("🚨", OpeningStatus.EMERGENCY_CLOSED, 26), 21,
                             "XRP-USDT", TradeSide.LONG)
    assert closed is not None
    assert [b.callback_data for b in closed.inline_keyboard[0]] == [
        MenuCallback.OPEN_POSITIONS, MenuCallback.MAIN,
    ]
    assert _labels(closed) == ["📋 К позициям", "◀️ В меню"]
    # цикл восстановления — та же клавиатура
    cycle = opening_keyboard(OpeningStatus.EMERGENCY_CLOSED, 21, ("XRP-USDT", TradeSide.LONG))
    assert cycle == closed
    assert opening_keyboard(OpeningStatus.WORKING, 21, None).inline_keyboard[0][0] \
        .callback_data == f"{OpenCB.CANCEL_LIMIT}21"  # type: ignore[union-attr]
    assert opening_keyboard(OpeningStatus.EXPIRED, 21, None) is None


@pytest.mark.parametrize(("status", "trade_id", "text"), [
    (OpeningStatus.DONE, 24, "Лимит уже исполнился (сделка #24) — отменять нечего."),
    (OpeningStatus.EXPIRED, None, "Срок лимита уже вышел — лимит снят."),
    (OpeningStatus.CANCELLED, None, "Лимит уже отменён."),
])
async def test_old_cancel_button_answers_by_fact(monkeypatch, status, trade_id, text) -> None:  # type: ignore[no-untyped-def]
    """Л1–Л4: старая «Отменить» под исполненным/снятым лимитом — ответ по
    факту, без «Отменяю…», на биржу ничего, кнопка снимается."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot import outbox

    service = MagicMock()
    service.load = AsyncMock(return_value=SimpleNamespace(status=status, trade_id=trade_id))
    service.cancel_limit = AsyncMock()
    monkeypatch.setattr(open_trade, "_service", lambda *a, **k: service)
    monkeypatch.setattr(open_trade, "_audit", AsyncMock(return_value=True))
    stripped: list[int] = []

    async def strip(bot, chat_id, message_id):  # type: ignore[no-untyped-def]
        stripped.append(message_id)

    monkeypatch.setattr(open_trade, "_strip", strip)
    marks = AsyncMock()
    monkeypatch.setattr(outbox, "mark", marks)
    message = MagicMock(spec=Message)
    message.bot = object()
    message.chat = SimpleNamespace(id=777)
    message.message_id = 485
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"{OpenCB.CANCEL_LIMIT}16"
    callback.message = message
    callback.answer = AsyncMock()
    await open_trade.cancel_limit(callback, MagicMock(), MagicMock(), _settings(),
                                  MagicMock(), MagicMock(), MagicMock())
    callback.answer.assert_awaited_once_with(text, show_alert=True)
    service.cancel_limit.assert_not_awaited()
    assert stripped == [485]
    marks.assert_awaited_once()
