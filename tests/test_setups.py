"""Тесты детекторов сетапов.

Графики строятся вручную под конкретный сценарий: сначала проверяется,
что при выполнении всех условий методологии сетап находится, затем —
что при нарушении любого из них детектор честно отвечает WAIT.

Второе важнее первого. Методология прямо запрещает выдумывать сигналы,
и главная опасность здесь — детектор, который находит сетап там, где
его нет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.indicators import atr as calc_atr, ema, last_value, volume_ratio
from app.analysis.setups import BreakoutRetest, EMAPullback
from app.analysis.signals import MarketContext, wait_signal
from app.analysis.structure import detect_structure, find_levels
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure, SignalDirection

D = Decimal
BASE = datetime(2026, 3, 2, tzinfo=UTC)


def candle(
    index: int, o: float, h: float, low: float, c: float, volume: float = 1000
) -> Kline:
    t = BASE + timedelta(hours=index)
    return Kline(
        open_time=t,
        open=D(str(round(o, 4))),
        high=D(str(round(h, 4))),
        low=D(str(round(low, 4))),
        close=D(str(round(c, 4))),
        volume=D(str(round(volume, 2))),
        close_time=t + timedelta(hours=1),
    )


def build_context(candles: list[Kline], symbol: str = "BTC-USDT") -> MarketContext:
    """Собирает контекст так же, как это делает движок."""
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    atr_value = last_value(calc_atr(highs, lows, closes, 14))

    return MarketContext(
        symbol=symbol,
        timeframe="4h",
        candles=candles,
        price=closes[-1],
        ema20=last_value(ema(closes, 20)),
        ema50=last_value(ema(closes, 50)),
        ema200=last_value(ema(closes, 200)),
        rsi=None,
        atr=atr_value,
        macd_histogram=None,
        volume_ratio=last_value(volume_ratio(volumes, 20)),
        structure=detect_structure(candles).structure,
        levels=find_levels(candles, atr_value) if atr_value else [],
    )


def breakout_retest_chart() -> list[Kline]:
    """График с полным сетапом «пробой с ретестом» вверх.

    Сценарий по методологии:
      1. Долгий рост — цена уходит выше EMA200.
      2. Диапазон под уровнем 200 с несколькими касаниями.
      3. Пробой полнотелой свечой на объёме.
      4. Вторая свеча удерживается за уровнем.
      5. Возврат к уровню.
      6. Бычье поглощение на ретесте.
    """
    candles: list[Kline] = []
    i = 0

    # Рост от 100 до 190, чтобы EMA200 оказалась заметно ниже цены.
    price = 100.0
    for _ in range(200):
        nxt = price + 0.45
        candles.append(candle(i, price, nxt + 0.5, price - 0.5, nxt))
        price = nxt
        i += 1

    # Диапазон 192..200: уровень 200 набирает касания.
    for _ in range(4):
        candles.append(candle(i, 192, 200, 191, 194)); i += 1
        candles.append(candle(i, 194, 196, 192, 193)); i += 1
        candles.append(candle(i, 193, 200, 192, 195)); i += 1
        candles.append(candle(i, 195, 197, 192, 193)); i += 1

    # Пробой: полнотелая свеча с закрытием заметно выше 200, объём выше.
    candles.append(candle(i, 194, 206, 193.5, 205.5, volume=3000)); i += 1
    # Вторая свеча удерживается за уровнем.
    candles.append(candle(i, 205.5, 207, 203, 204.5, volume=1500)); i += 1
    # Откат к уровню.
    candles.append(candle(i, 204.5, 205, 201, 201.5)); i += 1
    # Подтверждение: медвежья свеча, затем поглощающая бычья.
    candles.append(candle(i, 201.5, 202, 199.5, 200.0)); i += 1
    candles.append(candle(i, 199.8, 203, 199.5, 202.5, volume=2000)); i += 1

    return candles


class TestBreakoutRetestFindsSetup:
    def test_full_scenario_produces_long(self) -> None:
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))

        assert signal.direction is SignalDirection.LONG, signal.note
        assert signal.setup == "Пробой с ретестом"
        assert signal.stop_loss is not None
        assert signal.take_profit_1 is not None

    def test_stop_is_below_entry_and_tight(self) -> None:
        """Короткий стоп на ретесте — смысл этого сетапа."""
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert signal.stop_loss is not None
        assert signal.entry_zone_high is not None
        assert signal.stop_loss < signal.entry_zone_high

    def test_risk_reward_meets_methodology(self) -> None:
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert signal.risk_reward is not None
        assert signal.risk_reward >= D(2)

    def test_all_conditions_reported(self) -> None:
        """Пользователь должен видеть, что именно проверено."""
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        names = {c.name for c in signal.conditions}
        assert "Фильтр EMA200" in names
        assert "Пробой уровня" in names
        assert "Ретест уровня" in names
        assert "Подтверждающий паттерн" in names
        assert "Risk/Reward" in names

    def test_invalidation_is_stated(self) -> None:
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert "отменяет сценарий" in signal.invalidation


class TestBreakoutRetestRejects:
    """Проверки, что детектор не выдумывает сигналы."""

    def test_no_setup_on_flat_market(self) -> None:
        candles = [candle(i, 100, 100.5, 99.5, 100) for i in range(250)]
        signal = BreakoutRetest().detect(build_context(candles))
        assert signal.direction is SignalDirection.WAIT

    def test_no_setup_without_history(self) -> None:
        """EMA200 требует 200 свечей: без неё главный фильтр не работает."""
        candles = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(50)]
        signal = BreakoutRetest().detect(build_context(candles))
        assert signal.direction is SignalDirection.WAIT
        assert "истории" in signal.note.lower()

    def test_no_entry_at_breakout_moment(self) -> None:
        """Ключевое правило: не входить в момент пробоя, ждать ретеста."""
        chart = breakout_retest_chart()
        # Обрезаем до пробойной свечи — ретеста ещё не было.
        at_breakout = chart[:-3]
        signal = BreakoutRetest().detect(build_context(at_breakout))

        assert signal.direction is SignalDirection.WAIT
        assert "ретест" in signal.note.lower()

    def test_no_entry_without_confirmation(self) -> None:
        """Цена на ретесте, но подтверждающей свечи нет."""
        chart = breakout_retest_chart()[:-1]  # убираем поглощение
        signal = BreakoutRetest().detect(build_context(chart))

        assert signal.direction is SignalDirection.WAIT
        assert "подтвержд" in signal.note.lower()

    def test_false_breakout_rejected(self) -> None:
        """Ложный пробой: цена вернулась за уровень сразу.

        Методология считает это ловушкой, а не сетапом.
        """
        candles: list[Kline] = []
        i = 0
        price = 100.0
        for _ in range(200):
            nxt = price + 0.45
            candles.append(candle(i, price, nxt + 0.5, price - 0.5, nxt))
            price = nxt
            i += 1
        for _ in range(4):
            candles.append(candle(i, 192, 200, 191, 194)); i += 1
            candles.append(candle(i, 194, 196, 192, 193)); i += 1
            candles.append(candle(i, 193, 200, 192, 195)); i += 1
            candles.append(candle(i, 195, 197, 192, 193)); i += 1

        # Пробой, но следующая свеча закрылась обратно под уровнем.
        candles.append(candle(i, 194, 206, 193.5, 205.5, volume=3000)); i += 1
        candles.append(candle(i, 205.5, 206, 196, 197, volume=2000)); i += 1
        candles.append(candle(i, 197, 201, 196, 200.2)); i += 1
        candles.append(candle(i, 200, 203, 199, 202.5, volume=2000)); i += 1

        signal = BreakoutRetest().detect(build_context(candles))
        assert signal.direction is SignalDirection.WAIT

    def test_long_not_taken_below_ema200(self) -> None:
        """Против глобального тренда методология не торгует."""
        candles: list[Kline] = []
        price = 300.0
        for i in range(250):
            nxt = price - 0.6
            candles.append(candle(i, price, price + 0.5, nxt - 0.5, nxt))
            price = nxt

        context = build_context(candles)
        assert context.above_ema200 is False
        assert context.trend_allows is SignalDirection.SHORT


class TestEMAPullback:
    def test_requires_trending_structure(self) -> None:
        """Откат к EMA50 работает только в тренде, не в диапазоне."""
        candles = [
            candle(i, 100 + (i % 5), 101 + (i % 5), 99 + (i % 5), 100 + (i % 5))
            for i in range(250)
        ]
        signal = EMAPullback().detect(build_context(candles))
        assert signal.direction is SignalDirection.WAIT

    def test_no_signal_far_from_ema(self) -> None:
        candles: list[Kline] = []
        price = 100.0
        for i in range(240):
            nxt = price + 0.5
            candles.append(candle(i, price, nxt + 0.3, price - 0.3, nxt))
            price = nxt
        # Резкий отрыв вверх от EMA50.
        for i in range(240, 250):
            price += 8
            candles.append(candle(i, price - 8, price + 1, price - 9, price))

        signal = EMAPullback().detect(build_context(candles))
        assert signal.direction is SignalDirection.WAIT


class TestEngineSelection:
    def test_wait_with_most_progress_is_shown(self) -> None:
        """Полезнее увидеть «ждём ретеста», чем «нет пробоя».

        Когда сетапа нет ни у одного детектора, показывается тот,
        который прошёл дальше по списку условий.
        """
        engine = AnalysisEngine(market=None)  # type: ignore[arg-type]
        chart = breakout_retest_chart()[:-3]
        signal = engine.evaluate(build_context(chart))

        assert signal.direction is SignalDirection.WAIT
        assert signal.passed_conditions

    def test_actionable_signal_wins_over_wait(self) -> None:
        engine = AnalysisEngine(market=None)  # type: ignore[arg-type]
        signal = engine.evaluate(build_context(breakout_retest_chart()))
        assert signal.is_actionable

    def test_detector_failure_does_not_break_analysis(self) -> None:
        """Падение одного детектора не должно ронять весь анализ."""
        from app.analysis.setups import SetupDetector

        class BrokenDetector(SetupDetector):
            name = "Сломанный"

            def detect(self, context):  # type: ignore[no-untyped-def]
                raise RuntimeError("сбой в детекторе")

        engine = AnalysisEngine(
            market=None,  # type: ignore[arg-type]
            detectors=[BrokenDetector(), BreakoutRetest()],
        )
        signal = engine.evaluate(build_context(breakout_retest_chart()))
        assert signal.is_actionable


class TestSignalStructure:
    def test_wait_signal_is_valid_result(self) -> None:
        """WAIT — полноценный ответ, а не ошибка."""
        signal = wait_signal("BTC-USDT", "4h", "нет условий")
        assert signal.direction is SignalDirection.WAIT
        assert not signal.is_actionable
        assert signal.side is None

    def test_confidence_within_scale(self) -> None:
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert 0 <= signal.confidence <= 10

    def test_side_maps_to_trade_direction(self) -> None:
        from app.trading.enums import TradeSide

        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert signal.side is TradeSide.LONG
