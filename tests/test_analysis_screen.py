"""Экран «Анализ рынка»: график и вердикт по кнопке.

Проверяется главное: экран считает тем же кодом, что и сканер, не пишет в
signals, не даёт кнопку входа, не путается с READY и не роняет навигацию
под фото. Сеть не участвует — биржа подменена счётчиком вызовов.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.analysis import classify
from app.analysis.engine import AnalysisEngine
from app.analysis.setups import EMAPullback
from app.analysis.signals import MarketContext, Signal, SignalCondition
from app.analysis.structure import Level
from app.bot import messaging
from app.bot.handlers import analysis as screen
from app.bot.handlers.analysis import (
    CAPTION_LIMIT,
    AnalysisCB,
    TimeframeResult,
    analyze_timeframes,
    market_keyboard,
    render_market,
    render_signal,
    render_verdict,
)
from app.database.models.user import UserSettings
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeUnavailableError, Kline, SymbolInfo
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.trading.enums import MarketStructure, SignalDirection
from app.workers import scanner
from tests.test_market_data import FakeClient

D = Decimal
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FORBIDDEN_WORDS = ("READY", "Сетап готов", "Формируется сетап")


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


def _found(timeframe: str = "4h") -> Signal:
    return Signal(
        symbol="BTC-USDT", timeframe=timeframe, direction=SignalDirection.LONG,
        setup="Пробой с ретестом", entry_zone_low=D("100"), entry_zone_high=D("101"),
        stop_loss=D("98"), take_profit_1=D("106"), risk_reward=D("2.5"), confidence=7,
        invalidation="Закрытие ниже 98 отменяет сценарий",
        conditions=[SignalCondition("Фильтр EMA200", True, "")],
    )


def _forming(timeframe: str = "4h") -> Signal:
    return Signal(
        symbol="BTC-USDT", timeframe=timeframe, direction=SignalDirection.WAIT,
        setup="Нет сетапа", note="Цена на ретесте, но подтверждения нет.",
        conditions=[
            SignalCondition("Фильтр EMA200", True, ""),
            SignalCondition("Подтверждающий паттерн", False, "Нет пин-бара"),
        ],
    )


def _waiting(timeframe: str = "4h") -> Signal:
    return Signal(
        symbol="BTC-USDT", timeframe=timeframe, direction=SignalDirection.WAIT,
        setup="Нет сетапа", note="Уровень пробит, но ретеста ещё не было.",
        conditions=[
            SignalCondition("Фильтр EMA200", True, ""),
            SignalCondition("Ретест уровня", False, "Возврата к уровню 99.5 ещё не было"),
        ],
    )


def _results(h1: Signal, h4: Signal) -> dict[str, TimeframeResult]:
    return {
        "1h": TimeframeResult("1h", _context("1h"), h1),
        "4h": TimeframeResult("4h", _context("4h"), h4),
    }


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


# --- render_verdict -------------------------------------------------------


class TestRenderVerdict:
    def test_found_setup_shows_levels_rr_quality(self) -> None:
        text = render_verdict("BTC-USDT", _results(_waiting("1h"), _found()), "4h")
        assert "LONG" in text and "сетап найден" in text
        assert "Вход:" in text and "Стоп:" in text and "Цель:" in text
        assert "RR 1:2.5" in text and "7/10" in text

    def test_forming_is_marked_forming(self) -> None:
        text = render_verdict("BTC-USDT", _results(_waiting("1h"), _forming()), "4h")
        assert "формируется" in text
        assert "Подтверждающий паттерн" in text  # названо, чего не хватает

    def test_wait_names_the_missing_condition(self) -> None:
        text = render_verdict("BTC-USDT", _results(_waiting("1h"), _waiting()), "4h")
        assert "WAIT" in text
        assert "Ретест уровня" in text
        assert "Возврата к уровню" in text

    def test_wait_without_conditions_falls_back_to_note(self) -> None:
        bare = Signal(
            symbol="BTC-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note="Недостаточно истории для EMA200.",
        )
        text = render_verdict("BTC-USDT", _results(bare, bare), "4h")
        assert "Недостаточно истории" in text

    @pytest.mark.parametrize("make", [_found, _forming, _waiting])
    def test_never_uses_scanner_wording(self, make) -> None:  # type: ignore[no-untyped-def]
        text = render_verdict("BTC-USDT", _results(make("1h"), make()), "4h")
        for word in FORBIDDEN_WORDS:
            assert word not in text
        assert "не сигнал сканера" in text

    def test_verdict_covers_both_scanner_timeframes(self) -> None:
        text = render_verdict("BTC-USDT", _results(_found("1h"), _waiting()), "4h")
        assert "1H:" in text and "4H:" in text

    def test_caption_fits_telegram_limit_in_worst_case(self) -> None:
        long = "очень длинное описание условия " * 30
        nasty = Signal(
            symbol="1000SATS-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note=long,
            conditions=[SignalCondition("Ретест уровня " * 5, False, long)],
        )
        for selected_signal in (nasty, _found()):
            results = {
                "1h": TimeframeResult("1h", None, nasty),
                "4h": TimeframeResult("4h", None, selected_signal),
            }
            text = render_verdict("1000SATS-USDT", results, "4h", 8, ready_notifications=False)
            assert len(text) <= CAPTION_LIMIT

    def test_disabled_ready_notifications_are_mentioned(self) -> None:
        on = render_verdict("BTC-USDT", _results(_waiting("1h"), _found()), "4h", None, True)
        off = render_verdict("BTC-USDT", _results(_waiting("1h"), _found()), "4h", None, False)
        assert "выключены" not in on
        assert "выключены" in off

    def test_html_in_details_is_escaped(self) -> None:
        sig = Signal(
            symbol="BTC-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note="RSI < 30 и цена > EMA",
            conditions=[SignalCondition("Условие", False, "a < b")],
        )
        text = render_verdict("BTC-USDT", _results(sig, sig), "4h")
        assert "a < b" not in text and "a &lt; b" in text


RAW_PRICE = re.compile(r"(?<![\d.])\d+\.\d{4}(?!\d)")


class TestRawNumbers:
    """Детекторы вшивают цены в текст как {x:.4f}; Telegram принимает такие
    хвосты за номера и подсвечивает ссылками. Экран обязан отдавать их через
    fmt_price по точности символа."""

    def test_real_detector_output_has_no_raw_decimals(self) -> None:
        signal = EMAPullback().detect(_context())
        assert RAW_PRICE.search(signal.note + signal.conditions[-1].detail), (
            "тест устарел: детектор больше не печатает .4f, проверять нечего"
        )
        text = render_verdict("BTC-USDT", _results(signal, signal), "4h", 2)
        assert not RAW_PRICE.findall(text)
        assert "Расстояние до EMA50: 2.2 (допуск 0.75)" in text
        assert "EMA50 (98)" in text

    @pytest.mark.parametrize(
        "detail",
        [  # формулировки из app/analysis/setups.py, по одной на каждое место с .4f
            "Пробит уровень 2668.5000",
            "Цена возвращалась к уровню 2668.5000 и удержалась",
            "Возврата к уровню 2668.5000 ещё не было",
            "Уровень 2668.5000 пробит, но ретеста ещё не было. Ждём возврата цены к уровню.",
            "Расстояние до EMA50: 102.7574 (допуск 3.4915)",
            "Цена далеко от EMA50 (2529.4926). Ждём отката к динамической поддержке.",
        ],
    )
    def test_every_detector_wording_is_formatted(self, detail: str) -> None:
        signal = Signal(
            symbol="ETH-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note=detail,
            conditions=[SignalCondition("Условие", False, detail)],
        )
        text = render_verdict("ETH-USDT", _results(signal, signal), "4h", 2)
        assert not RAW_PRICE.findall(text)

    def test_precision_of_the_symbol_is_used(self) -> None:
        sig = Signal(
            symbol="ETH-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note="Цена далеко от EMA50 (2529.4926).",
        )
        two = render_verdict("ETH-USDT", _results(sig, sig), "4h", 2)
        three = render_verdict("ETH-USDT", _results(sig, sig), "4h", 3)
        none = render_verdict("ETH-USDT", _results(sig, sig), "4h", None)
        assert "EMA50 (2529.49)" in two
        assert "EMA50 (2529.493)" in three
        assert "EMA50 (2529.49)" in none  # без SymbolInfo: порядок величины (>=1000 → 2 знака)

    def test_invalidation_of_found_setup_is_formatted(self) -> None:
        base = _found()
        found = Signal(
            symbol=base.symbol, timeframe=base.timeframe, direction=base.direction,
            setup=base.setup, entry_zone_low=base.entry_zone_low,
            entry_zone_high=base.entry_zone_high, stop_loss=base.stop_loss,
            take_profit_1=base.take_profit_1, risk_reward=base.risk_reward,
            confidence=base.confidence, conditions=base.conditions,
            invalidation="Закрытие ниже 2500.1234 отменяет сценарий",
        )
        text = render_verdict("ETH-USDT", _results(_waiting("1h"), found), "4h", 2)
        assert "ниже 2500.12 отменяет" in text
        assert not RAW_PRICE.findall(text)

    def test_other_numbers_are_left_alone(self) -> None:
        sig = Signal(
            symbol="ETH-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа", note="RR 1:1.80 ниже минимального 1:2, 12:30, 15 свечей",
        )
        text = render_verdict("ETH-USDT", _results(sig, sig), "4h", 2)
        assert "RR 1:1.80 ниже минимального 1:2, 12:30, 15 свечей" in text


class TestRenderSignalInvalidation:
    """Старый экран «Найти вход» (render_signal): инвалидация из текста
    детектора тоже содержит {stop:.4f}."""

    @staticmethod
    def _signal() -> Signal:
        base = _found()
        return Signal(
            symbol=base.symbol, timeframe=base.timeframe, direction=base.direction,
            setup=base.setup, entry_zone_low=base.entry_zone_low,
            entry_zone_high=base.entry_zone_high, stop_loss=base.stop_loss,
            take_profit_1=base.take_profit_1, risk_reward=base.risk_reward,
            confidence=base.confidence, conditions=base.conditions,
            invalidation="Закрытие ниже 2500.1234 отменяет сценарий",
        )

    def test_invalidation_goes_through_fmt_price(self) -> None:
        text = render_signal(self._signal(), 2)
        line = next(x for x in text.splitlines() if "Инвалидация" in x)
        assert "ниже 2500.12 отменяет" in line
        assert not RAW_PRICE.findall(line)

    def test_invalidation_without_symbol_precision(self) -> None:
        line = next(x for x in render_signal(self._signal()).splitlines() if "Инвалидация" in x)
        assert "ниже 2500.12 отменяет" in line  # порядок величины: >=1000 → 2 знака


class TestRenderSignalDetails:
    """Остальные строки render_signal, куда попадает текст детектора:
    заметка и детали условий (и в WAIT, и у найденного сетапа)."""

    @staticmethod
    def _wait() -> Signal:
        return Signal(
            symbol="ETH-USDT", timeframe="4h", direction=SignalDirection.WAIT,
            setup="Нет сетапа",
            note="Цена далеко от EMA50 (2529.4926). Ждём отката к динамической поддержке.",
            conditions=[
                SignalCondition("Фильтр EMA200", True, "Цена выше EMA200"),
                SignalCondition(
                    "Откат к EMA50", False, "Расстояние до EMA50: 102.7574 (допуск 3.4915)"
                ),
            ],
        )

    @staticmethod
    def _found_with_text() -> Signal:
        base = _found()
        return Signal(
            symbol=base.symbol, timeframe=base.timeframe, direction=base.direction,
            setup=base.setup, entry_zone_low=base.entry_zone_low,
            entry_zone_high=base.entry_zone_high, stop_loss=base.stop_loss,
            take_profit_1=base.take_profit_1, risk_reward=base.risk_reward,
            confidence=base.confidence, invalidation="Закрытие ниже 98.5030 отменяет сценарий",
            note="Цель: следующий уровень 2668.5000",
            conditions=[
                SignalCondition("Ретест уровня", True, "Цена возвращалась к уровню 2668.5000"),
            ],
        )

    def test_wait_note_and_conditions_are_formatted(self) -> None:
        text = render_signal(self._wait(), 2)
        assert not RAW_PRICE.findall(text)
        assert "EMA50 (2529.49)" in text
        assert "Расстояние до EMA50: 102.76 (допуск 3.49)" in text

    def test_found_note_and_conditions_are_formatted(self) -> None:
        text = render_signal(self._found_with_text(), 2)
        assert not RAW_PRICE.findall(text)
        assert "Цель: следующий уровень 2668.5" in text
        assert "к уровню 2668.5" in text
        assert "ниже 98.5 отменяет" in text

    def test_without_precision_uses_price_magnitude(self) -> None:
        """Точность неизвестна: >=1000 — два знака. Для цен 1..1000 fallback
        fmt_price оставляет четыре знака, поэтому 102.7574 здесь не меняется."""
        text = render_signal(self._wait())
        assert "EMA50 (2529.49)" in text


class TestKeyboard:
    def test_no_execution_button(self) -> None:
        markup = market_keyboard("BTC-USDT", "4h")
        for row in markup.inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith("exec:")
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


class TestSharedWithScanner:
    def test_same_classifier_object(self) -> None:
        # Одно определение FORMING на экран и сканер: копия разошлась бы.
        assert scanner.classify_signal is classify.classify_signal
        assert scanner.SCAN_TIMEFRAMES is classify.SCAN_TIMEFRAMES


# --- запросы к бирже -------------------------------------------------------


class TestRequests:
    async def test_second_press_is_served_from_cache(self) -> None:
        client = FakeClient(_candles())
        engine = AnalysisEngine(MarketDataService(client, TTLCache()))  # type: ignore[arg-type]

        await analyze_timeframes(engine, "BTC-USDT")
        # H1, H4 и D1 (старший для H4): H4 общий для обоих контекстов.
        assert client.kline_calls == 3

        await analyze_timeframes(engine, "BTC-USDT")
        assert client.kline_calls == 3  # повторное нажатие — без походов на биржу

    async def test_verdict_equals_scanner_evaluation(self) -> None:
        client = FakeClient(_candles())
        engine = AnalysisEngine(MarketDataService(client, TTLCache()))  # type: ignore[arg-type]

        results = await analyze_timeframes(engine, "BTC-USDT")
        for timeframe, result in results.items():
            context = await engine.build_context("BTC-USDT", timeframe)
            assert context is not None
            expected = engine.evaluate(context)
            assert result.signal.direction is expected.direction
            assert result.signal.note == expected.note


# --- хендлер ---------------------------------------------------------------


class _Client:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(self, signal: Signal, error: Exception | None = None) -> None:
        self._signal = signal
        self._error = error
        self.calls = 0

    async def build_context(self, symbol: str, timeframe: str) -> MarketContext:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _context(timeframe)

    def evaluate(self, context: MarketContext) -> Signal:
        return self._signal

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return SymbolInfo(symbol, 2, 4, D("0.0001"))


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


def _user(notify: bool = True) -> SimpleNamespace:
    settings = UserSettings(notifications={"setup_ready": notify})
    return SimpleNamespace(id=77, settings=settings)


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
    async def test_sends_chart_photo_with_verdict_and_no_execution_button(
        self, engine_factory
    ) -> None:
        client = engine_factory(_Engine(_found()))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_awaited_once()
        args, kwargs = message.answer_photo.await_args
        assert args[0].data[:8] == PNG_MAGIC
        assert "LONG" in kwargs["caption"]
        assert len(kwargs["caption"]) <= CAPTION_LIMIT
        for row in kwargs["reply_markup"].inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith("exec:")
        message.delete.assert_awaited_once()  # плейсхолдер убран
        assert client.closed
        assert not screen._in_flight

    async def test_chart_gets_symbol_price_precision(
        self, engine_factory, monkeypatch
    ) -> None:
        engine_factory(_Engine(_found()))
        received: list[tuple] = []
        monkeypatch.setattr(
            screen, "render_analysis_chart", lambda *args: received.append(args) or None
        )

        await screen.show_market(_callback(_message()), _user(), settings=None)  # type: ignore[arg-type]

        assert received and received[0][2] == 2  # SymbolInfo.price_precision из _Engine

    async def test_shows_progress_while_working(self, engine_factory) -> None:
        engine_factory(_Engine(_found()))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        statuses = [c.args[0] for c in message.edit_text.await_args_list]
        assert any("Загружаю" in s for s in statuses)
        assert any("Рисую график" in s for s in statuses)

    async def test_wait_still_gets_a_chart(self, engine_factory) -> None:
        engine_factory(_Engine(_waiting()))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        _, kwargs = message.answer_photo.await_args
        assert "Ретест уровня" in kwargs["caption"]

    async def test_chart_failure_falls_back_to_text_verdict(
        self, engine_factory, monkeypatch
    ) -> None:
        engine_factory(_Engine(_found()))
        monkeypatch.setattr(screen, "render_analysis_chart", lambda *_: None)
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_not_awaited()
        final_text = message.edit_text.await_args.args[0]
        assert "LONG" in final_text

    async def test_switching_timeframe_edits_media_in_place(self, engine_factory) -> None:
        engine_factory(_Engine(_found()))
        message = _message(photo=[object()])

        await screen.switch_market_timeframe(
            _callback(message, f"{AnalysisCB.MARKET_TF}BTC-USDT:1h"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        message.edit_media.assert_awaited_once()
        message.answer_photo.assert_not_awaited()
        assert "график 1H" in message.edit_media.await_args.args[0].caption

    async def test_repeated_tap_while_running_is_ignored(self, engine_factory) -> None:
        engine = _Engine(_found())
        engine_factory(engine)
        screen._in_flight.add((77, "BTC-USDT"))
        callback = _callback(_message())

        await screen.show_market(callback, _user(), settings=None)  # type: ignore[arg-type]

        assert engine.calls == 0
        callback.answer.assert_awaited_once_with("Уже считаю…")
        assert (77, "BTC-USDT") in screen._in_flight  # чужой флаг не сброшен

    async def test_in_flight_flag_released_after_error(self, engine_factory) -> None:
        engine_factory(_Engine(_found(), error=ExchangeUnavailableError("down")))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        assert not screen._in_flight
        assert "Биржа не отвечает" in message.edit_text.await_args.args[0]

    async def test_unexpected_error_gets_neutral_text_not_exchange_text(
        self, engine_factory
    ) -> None:
        engine_factory(_Engine(_found(), error=ValueError("boom")))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        text = message.edit_text.await_args.args[0]
        assert "Не удалось построить анализ" in text
        assert "Биржа" not in text and "boom" not in text

    async def test_concurrent_taps_run_one_calculation(self, engine_factory) -> None:
        engine = _Engine(_found())
        engine_factory(engine)

        await asyncio.gather(
            screen.show_market(_callback(_message()), _user(), settings=None),  # type: ignore[arg-type]
            screen.show_market(_callback(_message()), _user(), settings=None),  # type: ignore[arg-type]
        )

        assert engine.calls == 2  # H1 + H4 один раз, а не дважды

    async def test_disabled_notifications_line_reaches_caption(self, engine_factory) -> None:
        engine_factory(_Engine(_found()))
        message = _message()

        await screen.show_market(
            _callback(message), _user(notify=False), settings=None  # type: ignore[arg-type]
        )

        _, kwargs = message.answer_photo.await_args
        assert "выключены" in kwargs["caption"]


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
