"""Экран «Анализ рынка»: график и техническая картина по кнопке.

02.10.2026: сигналы удалены — экран не выносит вердикт LONG/SHORT/WAIT, не
даёт кнопку входа и помечен «Информация, не торговая рекомендация».
Проверяется: текст экрана, кэш запросов, график и подпись, отказ без
падения навигации под фото. Сеть не участвует — биржа подменена.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.analysis.context import MarketContext
from app.analysis.engine import AnalysisEngine
from app.analysis.structure import Level
from app.bot import messaging
from app.bot.handlers import analysis as screen
from app.bot.handlers.analysis import (
    CAPTION_LIMIT,
    DISCLAIMER,
    AnalysisCB,
    market_keyboard,
    render_market,
    render_market_caption,
)
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeUnavailableError, Kline, SymbolInfo
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import MarketStructure
from tests.test_market_data import FakeClient

D = Decimal
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Слова вердикта и сигналов — их на экране больше нет.
VERDICT_WORDS = ("LONG", "SHORT", "WAIT", "READY", "сетап", "Вход:", "Стоп:")


# --- данные ----------------------------------------------------------------


def _candles(n: int = 300) -> list[Kline]:
    now = datetime.now(UTC)
    price = D("100")
    out = []
    for i in range(n):
        price += D(i % 5 - 2) * D("0.3")
        open_time = now - timedelta(hours=n - i + 1)
        out.append(
            Kline(
                open_time=open_time, open=price, high=price + D("1"), low=price - D("1"),
                close=price + D("0.2"), volume=D("1000"),
                close_time=open_time + timedelta(hours=1),
            )
        )
    return out


def _context(timeframe: str = "4h") -> MarketContext:
    candles = _candles()
    return MarketContext(
        symbol="BTC-USDT", timeframe=timeframe, candles=candles, price=candles[-1].close,
        ema20=D("99"), ema50=D("98"), ema200=D("95"), rsi=D("55"), atr=D("1.5"),
        macd_histogram=D("0.1"), volume_ratio=D("1.2"), structure=MarketStructure.UPTREND,
        levels=[],
    )


def _context(timeframe: str = "4h") -> MarketContext:
    candles = _candles()
    return MarketContext(
        symbol="BTC-USDT", timeframe=timeframe, candles=candles, price=candles[-1].close,
        ema20=D("99"), ema50=D("98"), ema200=D("95"), rsi=D("55"), atr=D("1.5"),
        macd_histogram=D("0.1"), volume_ratio=D("1.2"), structure=MarketStructure.UPTREND,
        levels=[],
    )


# --- render_market: роль уровня --------------------------------------------


class TestMarketLevelRoles:
    """Справка печатает роль по положению уровня относительно цены, а не по
    is_resistance — иначе разойдётся с легендой графика."""

    @staticmethod
    def _lines(levels: list[Level]) -> list[str]:
        context = replace(_context(), levels=levels)
        text = render_market(context, 2)
        return [line for line in text.splitlines() if "касаний" in line]

    @staticmethod
    def _level(offset: str, *, resistance: bool, price: Decimal) -> Level:
        return Level(
            price=price + D(offset), touches=3, last_touch_index=1,
            is_resistance=resistance, strength=D("0.5"),
        )

    def test_broken_resistance_below_price_is_support(self) -> None:
        price = _context().price
        (line,) = self._lines([self._level("-5", resistance=True, price=price)])
        assert "поддержка" in line and "сопротивление" not in line

    def test_support_flag_above_price_is_resistance(self) -> None:
        price = _context().price
        (line,) = self._lines([self._level("5", resistance=False, price=price)])
        assert "сопротивление" in line and "поддержка" not in line


# --- подпись экрана ---------------------------------------------------------


class TestCaption:
    def test_has_disclaimer_and_no_verdict(self) -> None:
        text = render_market_caption(_context(), 2)
        assert text.endswith(DISCLAIMER)
        assert "Информация, не торговая рекомендация" in text
        for word in VERDICT_WORDS:
            assert word not in text, word

    def test_market_facts_are_shown(self) -> None:
        text = render_market_caption(_context(), 2)
        for fragment in ("Цена выше EMA200", "Структура:", "RSI:", "ATR:", "Объём к среднему"):
            assert fragment in text, fragment

    def test_caption_fits_telegram_limit_with_many_levels(self) -> None:
        price = _context().price
        levels = [
            Level(price=price + D(i), touches=9, last_touch_index=1,
                  is_resistance=True, strength=D("0.9"))
            for i in range(-20, 21) if i
        ]
        context = replace(_context(), levels=levels, higher_timeframe="1d",
                          higher_structure=MarketStructure.DOWNTREND)
        assert len(render_market_caption(context, 8)) <= CAPTION_LIMIT


class TestKeyboard:
    def test_no_execution_button(self) -> None:
        markup = market_keyboard("BTC-USDT", "4h")
        for row in markup.inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith(("exec:", "exn:"))
                assert "Открыть сделку" not in button.text

    def test_current_timeframe_is_noop_and_other_switches(self) -> None:
        buttons = market_keyboard("BTC-USDT", "4h").inline_keyboard[0]
        by_text = {b.text: b.callback_data for b in buttons}
        assert by_text["• 4H"] == AnalysisCB.NOOP
        assert by_text["1H"] == f"{AnalysisCB.MARKET_TF}BTC-USDT:1h"

    def test_callback_data_fits_telegram_limit(self) -> None:
        markup = market_keyboard("1000000MOG-USDT", "1h")
        for row in markup.inline_keyboard:
            for button in row:
                assert len((button.callback_data or "").encode()) <= 64


# --- запросы к бирже -------------------------------------------------------


class TestRequests:
    async def test_second_press_is_served_from_cache(self) -> None:
        client = FakeClient(_candles())
        engine = AnalysisEngine(MarketDataService(client, TTLCache()))  # type: ignore[arg-type]

        await engine.build_context("BTC-USDT", "4h")
        # H4 и D1 (старший для H4).
        assert client.kline_calls == 2

        await engine.build_context("BTC-USDT", "4h")
        assert client.kline_calls == 2  # повторное нажатие — без походов на биржу


# --- хендлер ---------------------------------------------------------------


class _Client:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(self, error: Exception | None = None, *, empty: bool = False) -> None:
        self._error = error
        self._empty = empty
        self.calls = 0

    async def build_context(self, symbol: str, timeframe: str) -> MarketContext | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return None if self._empty else _context(timeframe)

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return SymbolInfo(symbol, 2, 4, D("0.0001"), D("2"))


def _message(photo: object = None) -> MagicMock:
    message = MagicMock(spec=Message)
    message.photo = photo
    message.chat = SimpleNamespace(id=1)
    for name in ("edit_text", "edit_caption", "edit_media", "answer", "answer_photo", "delete"):
        setattr(message, name, AsyncMock())
    return message


def _callback(message: MagicMock, data: str = "an:market:BTC-USDT") -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = message
    callback.bot = None
    callback.answer = AsyncMock()
    return callback


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=77)


@pytest.fixture
def engine_factory(monkeypatch):  # type: ignore[no-untyped-def]
    def install(engine: _Engine) -> _Client:
        client = _Client()

        async def fake_engine(_settings):  # type: ignore[no-untyped-def]
            return engine, client

        monkeypatch.setattr(screen, "_engine", fake_engine)
        return client

    screen._in_flight.clear()
    return install


class TestHandler:
    async def test_sends_chart_photo_with_summary_and_no_execution_button(
        self, engine_factory
    ) -> None:
        client = engine_factory(_Engine())
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_awaited_once()
        args, kwargs = message.answer_photo.await_args
        assert args[0].data[:8] == PNG_MAGIC
        assert "BTC-USDT · 4H" in kwargs["caption"]
        assert DISCLAIMER in kwargs["caption"]
        assert len(kwargs["caption"]) <= CAPTION_LIMIT
        for row in kwargs["reply_markup"].inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith(("exec:", "exn:"))
        message.delete.assert_awaited_once()  # плейсхолдер убран
        assert client.closed
        assert not screen._in_flight

    async def test_chart_gets_symbol_price_precision(
        self, engine_factory, monkeypatch
    ) -> None:
        engine_factory(_Engine())
        received: list[tuple] = []
        monkeypatch.setattr(
            screen, "render_analysis_chart", lambda *args: received.append(args) or None
        )

        await screen.show_market(_callback(_message()), _user(), settings=None)  # type: ignore[arg-type]

        assert received and received[0][1] == 2  # SymbolInfo.price_precision из _Engine

    async def test_shows_progress_while_working(self, engine_factory) -> None:
        engine_factory(_Engine())
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        statuses = [c.args[0] for c in message.edit_text.await_args_list]
        assert any("Загружаю" in s for s in statuses)
        assert any("Рисую график" in s for s in statuses)

    async def test_chart_failure_falls_back_to_text(
        self, engine_factory, monkeypatch
    ) -> None:
        engine_factory(_Engine())
        monkeypatch.setattr(screen, "render_analysis_chart", lambda *_: None)
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_not_awaited()
        final_text = message.edit_text.await_args.args[0]
        assert "Структура:" in final_text and DISCLAIMER in final_text

    async def test_not_enough_data_is_text_without_chart(self, engine_factory) -> None:
        engine_factory(_Engine(empty=True))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_not_awaited()
        assert "Недостаточно рыночных данных" in message.edit_text.await_args.args[0]
        assert not screen._in_flight

    async def test_switching_timeframe_edits_media_in_place(self, engine_factory) -> None:
        engine = _Engine()
        engine_factory(engine)
        message = _message(photo=[object()])

        await screen.switch_market_timeframe(
            _callback(message, f"{AnalysisCB.MARKET_TF}BTC-USDT:1h"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        message.edit_media.assert_awaited_once()
        message.answer_photo.assert_not_awaited()
        assert "BTC-USDT · 1H" in message.edit_media.await_args.args[0].caption
        assert engine.calls == 1  # только выбранный таймфрейм

    async def test_unknown_timeframe_falls_back_to_h4(self, engine_factory) -> None:
        engine_factory(_Engine())
        message = _message(photo=[object()])

        await screen.switch_market_timeframe(
            _callback(message, f"{AnalysisCB.MARKET_TF}BTC-USDT:15m"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        assert "BTC-USDT · 4H" in message.edit_media.await_args.args[0].caption

    async def test_repeated_tap_while_running_is_ignored(self, engine_factory) -> None:
        engine = _Engine()
        engine_factory(engine)
        screen._in_flight.add((77, "BTC-USDT"))
        callback = _callback(_message())

        await screen.show_market(callback, _user(), settings=None)  # type: ignore[arg-type]

        assert engine.calls == 0
        callback.answer.assert_awaited_once_with("Уже считаю…")
        assert (77, "BTC-USDT") in screen._in_flight  # чужой флаг не сброшен

    async def test_in_flight_flag_released_after_error(self, engine_factory) -> None:
        engine_factory(_Engine(error=ExchangeUnavailableError("down")))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        assert not screen._in_flight
        assert "Биржа не отвечает" in message.edit_text.await_args.args[0]

    async def test_unexpected_error_gets_neutral_text_not_exchange_text(
        self, engine_factory
    ) -> None:
        engine_factory(_Engine(error=ValueError("boom")))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        text = message.edit_text.await_args.args[0]
        assert "Не удалось построить анализ" in text
        assert "Биржа" not in text and "boom" not in text

    async def test_concurrent_taps_run_one_calculation(self, engine_factory) -> None:
        engine = _Engine()
        engine_factory(engine)

        await asyncio.gather(
            screen.show_market(_callback(_message()), _user(), settings=None),  # type: ignore[arg-type]
            screen.show_market(_callback(_message()), _user(), settings=None),  # type: ignore[arg-type]
        )

        assert engine.calls == 1  # второй тап отбит флагом «уже считаю»

    async def test_intro_has_disclaimer(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        message = _message()
        plan = SimpleNamespace(allowed_symbols=["BTC-USDT"], allowed_timeframes=["1h"])
        monkeypatch.setattr(UserRepository, "get_trading_plan", AsyncMock(return_value=plan))

        await screen.ask_market_symbol(
            _callback(message, "menu:analysis"), MagicMock(), _user()  # type: ignore[arg-type]
        )

        text = message.edit_text.await_args.args[0]
        assert DISCLAIMER in text
        assert "вердикт" not in text.lower() and "сканер" not in text.lower()


# --- навигация под фото ------------------------------------------------------


class TestEditOrReplace:
    async def test_text_message_is_edited(self) -> None:
        message = _message()
        await messaging.edit_or_replace(message, "текст", None)
        message.edit_text.assert_awaited_once_with("текст", reply_markup=None)
        message.delete.assert_not_awaited()

    async def test_photo_message_is_deleted_and_replaced(self) -> None:
        message = _message(photo=[object()])
        await messaging.edit_or_replace(message, "текст", None)
        message.edit_text.assert_not_awaited()  # на фото он бы упал
        message.delete.assert_awaited_once()
        message.answer.assert_awaited_once_with("текст", reply_markup=None)

    async def test_failed_delete_does_not_block_the_screen(self) -> None:
        message = _message(photo=[object()])
        message.delete.side_effect = TelegramBadRequest(method=MagicMock(), message="old")
        await messaging.edit_or_replace(message, "текст", None)
        message.answer.assert_awaited_once()

    async def test_back_from_chart_reaches_symbol_list(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """«Назад» под графиком: ask_market_symbol не должен падать на фото."""
        message = _message(photo=[object()])
        plan = SimpleNamespace(allowed_symbols=["BTC-USDT"], allowed_timeframes=["1h"])
        monkeypatch.setattr(
            UserRepository, "get_trading_plan", AsyncMock(return_value=plan)
        )

        await screen.ask_market_symbol(
            _callback(message, "menu:analysis"), MagicMock(), _user()  # type: ignore[arg-type]
        )

        message.delete.assert_awaited_once()
        message.answer.assert_awaited_once()
