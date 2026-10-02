"""Экран «Анализ рынка» (этап 2): техническая картина H1/H4/D1 и график.

Проверяется: подпись (цифры, пересказ модели, пометка, лимит Telegram), нет
вердикта и кнопки входа, кэш снимка (повторный тап и смена таймфрейма на
биржу не ходят), график из того же снимка, отказы без падения навигации под
фото. Сеть не участвует — движок подменён.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.analysis.market_overview import MarketSnapshot
from app.bot import messaging
from app.bot.handlers import analysis as screen
from app.bot.handlers.analysis import (
    CAPTION_LIMIT,
    DISCLAIMER,
    AnalysisCB,
    compose_caption,
    market_keyboard,
)
from app.bot.handlers.exchange import _market_cache
from app.database.repositories.user import UserRepository
from app.exchanges.base import (
    ExchangeUnavailableError,
    Kline,
    OpenInterest,
    PremiumIndex,
    SymbolInfo,
)

D = Decimal
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NOW = datetime.now(UTC)
# Слова вердикта и сигналов — их на экране быть не должно.
VERDICT_WORDS = ("LONG", "SHORT", "WAIT", "READY", "сетап", "Вход:", "Стоп:")


def _candles(n: int = 300) -> list[Kline]:
    price = D("100")
    out = []
    for i in range(n):
        price += D(i % 5 - 2) * D("0.3")
        open_time = NOW - timedelta(hours=n - i + 1)
        out.append(
            Kline(
                open_time=open_time, open=price, high=price + D("1"), low=price - D("1"),
                close=price + D("0.2"), volume=D("1000"),
                close_time=open_time + timedelta(hours=1),
            )
        )
    return out


def _snapshot(*, d1: int = 300, premium: bool = True) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC-USDT",
        candles={"1h": _candles(), "4h": _candles(), "1d": _candles(d1)},
        premium=PremiumIndex(
            symbol="BTC-USDT", mark_price=D("100.4"), index_price=D("100.3"),
            last_funding_rate=D("0.0001"), next_funding_time=NOW + timedelta(hours=2),
            funding_interval_hours=8,
        ) if premium else None,
        open_interest=OpenInterest(symbol="BTC-USDT", value_usdt=D("908418144.5"), time=NOW),
    )


# --- подпись ------------------------------------------------------------------


class TestCaption:
    def test_without_summary_ends_with_disclaimer(self) -> None:
        caption = compose_caption("цифры", None)
        assert caption == f"цифры\n\n{DISCLAIMER}"
        assert "Информация, не торговая рекомендация" in caption

    def test_summary_is_escaped_and_marked(self) -> None:
        caption = compose_caption("цифры", "RSI <61> & тренд")
        assert "Кратко (пересказ цифр выше): RSI &lt;61&gt; &amp; тренд" in caption
        assert caption.endswith(DISCLAIMER)

    def test_summary_dropped_when_over_limit(self) -> None:
        text = "x" * (CAPTION_LIMIT - 100)
        caption = compose_caption(text, "п" * 300)
        assert "Кратко" not in caption and caption.endswith(DISCLAIMER)


class TestKeyboard:
    def test_no_execution_button(self) -> None:
        markup = market_keyboard("BTC-USDT", "4h")
        for row in markup.inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith(("exec:", "exn:"))
                assert "Открыть сделку" not in button.text

    def test_three_timeframes_current_is_noop(self) -> None:
        buttons = market_keyboard("BTC-USDT", "4h").inline_keyboard[0]
        by_text = {b.text: b.callback_data for b in buttons}
        assert set(by_text) == {"1H", "• 4H", "1D"}
        assert by_text["• 4H"] == AnalysisCB.NOOP
        assert by_text["1D"] == f"{AnalysisCB.MARKET_TF}BTC-USDT:1d"

    def test_callback_data_fits_telegram_limit(self) -> None:
        markup = market_keyboard("1000000MOG-USDT", "1h")
        for row in markup.inline_keyboard:
            for button in row:
                assert len((button.callback_data or "").encode()) <= 64


# --- хендлер ---------------------------------------------------------------


class _Client:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(
        self, error: Exception | None = None, snapshot: MarketSnapshot | None = None
    ) -> None:
        self._error = error
        self._snapshot = snapshot or _snapshot()
        self.calls = 0

    async def market_snapshot(self, symbol: str) -> MarketSnapshot:
        self.calls += 1
        await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        return self._snapshot

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return SymbolInfo(symbol, 2, 4, D("0.0001"), D("2"))


class _Summary:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.seen: list[tuple[int, str]] = []

    async def summarize(self, user_id: int, screen_text: str) -> str | None:
        self.seen.append((user_id, screen_text))
        return self.text


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
    _market_cache.invalidate("overview:")
    yield install
    _market_cache.invalidate("overview:")


class TestHandler:
    async def test_sends_chart_photo_with_overview_and_no_execution_button(
        self, engine_factory
    ) -> None:
        client = engine_factory(_Engine())
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_awaited_once()
        args, kwargs = message.answer_photo.await_args
        caption = kwargs["caption"]
        assert args[0].data[:8] == PNG_MAGIC
        assert "🔎 <b>BTC-USDT</b> · цена 100.4" in caption
        for tf in ("<b>H1</b>", "<b>H4</b>", "<b>D1</b>"):
            assert tf in caption
        assert "Funding: +0.0100%" in caption and "Open interest: 908.4 млн USDT" in caption
        assert caption.endswith(DISCLAIMER)
        for word in VERDICT_WORDS:
            assert word not in caption, word
        assert len(caption) <= CAPTION_LIMIT
        for row in kwargs["reply_markup"].inline_keyboard:
            for button in row:
                assert not (button.callback_data or "").startswith(("exec:", "exn:"))
        message.delete.assert_awaited_once()  # плейсхолдер убран
        assert client.closed
        assert not screen._in_flight

    async def test_default_chart_is_h4_from_snapshot(self, engine_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        engine_factory(_Engine())
        received: list[tuple] = []
        monkeypatch.setattr(
            screen, "render_analysis_chart", lambda *args: received.append(args) or None
        )

        await screen.show_market(_callback(_message()), _user(), settings=None)  # type: ignore[arg-type]

        (context, precision), = received
        assert context.timeframe == "4h" and precision == 2
        assert context.candles[-1].close == _candles()[-1].close

    async def test_summary_reaches_caption(self, engine_factory) -> None:
        engine_factory(_Engine())
        summary = _Summary("На H1 цена выше EMA50/200.")
        message = _message()

        await screen.show_market(
            _callback(message), _user(), settings=None, market_summary=summary  # type: ignore[arg-type]
        )

        caption = message.answer_photo.await_args.kwargs["caption"]
        assert "Кратко (пересказ цифр выше): На H1 цена выше EMA50/200." in caption
        assert summary.seen[0][0] == 77 and "<b>H4</b>" in summary.seen[0][1]

    async def test_no_summary_means_numbers_only(self, engine_factory) -> None:
        engine_factory(_Engine())
        message = _message()

        await screen.show_market(
            _callback(message), _user(), settings=None, market_summary=_Summary(None)  # type: ignore[arg-type]
        )

        assert "Кратко" not in message.answer_photo.await_args.kwargs["caption"]

    async def test_switch_to_d1_reuses_snapshot(self, engine_factory) -> None:
        engine = _Engine()
        engine_factory(engine)
        await screen.show_market(_callback(_message()), _user(), settings=None)  # type: ignore[arg-type]
        message = _message(photo=[object()])

        await screen.switch_market_timeframe(
            _callback(message, f"{AnalysisCB.MARKET_TF}BTC-USDT:1d"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        assert engine.calls == 1  # снимок из кэша — на биржу второй раз не ходили
        message.edit_media.assert_awaited_once()
        keyboard = message.edit_media.await_args.kwargs["reply_markup"]
        assert {b.text for b in keyboard.inline_keyboard[0]} == {"1H", "4H", "• 1D"}

    async def test_short_d1_history_no_chart_but_text(self, engine_factory) -> None:
        engine_factory(_Engine(snapshot=_snapshot(d1=20)))
        message = _message()

        await screen.switch_market_timeframe(
            _callback(message, f"{AnalysisCB.MARKET_TF}BTC-USDT:1d"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        message.answer_photo.assert_not_awaited()
        text = message.edit_text.await_args.args[0]
        assert "<b>D1</b>" not in text and "<b>H4</b>" in text

    async def test_missing_premium_shows_na(self, engine_factory) -> None:
        engine_factory(_Engine(snapshot=_snapshot(premium=False)))
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        assert "Funding: н/д" in message.answer_photo.await_args.kwargs["caption"]

    async def test_unknown_timeframe_falls_back_to_h4(self, engine_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        engine_factory(_Engine())
        received: list[tuple] = []
        monkeypatch.setattr(
            screen, "render_analysis_chart", lambda *args: received.append(args) or None
        )

        await screen.switch_market_timeframe(
            _callback(_message(), f"{AnalysisCB.MARKET_TF}BTC-USDT:15m"),
            _user(), settings=None,  # type: ignore[arg-type]
        )

        assert received[0][0].timeframe == "4h"

    async def test_chart_failure_falls_back_to_text(self, engine_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        engine_factory(_Engine())
        monkeypatch.setattr(screen, "render_analysis_chart", lambda *_: None)
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        message.answer_photo.assert_not_awaited()
        final_text = message.edit_text.await_args.args[0]
        assert "<b>H1</b>" in final_text and final_text.endswith(DISCLAIMER)

    async def test_shows_progress_while_working(self, engine_factory) -> None:
        engine_factory(_Engine())
        message = _message()

        await screen.show_market(_callback(message), _user(), settings=None)  # type: ignore[arg-type]

        statuses = [c.args[0] for c in message.edit_text.await_args_list]
        assert any("Загружаю" in s for s in statuses)
        assert any("Рисую график" in s for s in statuses)

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
        assert "funding" in text and "open interest" in text


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
