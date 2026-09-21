"""Тесты детекторов сетапов.

Графики строятся вручную под конкретный сценарий: сначала проверяется,
что при выполнении всех условий методологии сетап находится, затем —
что при нарушении любого из них детектор честно отвечает WAIT.

Второе важнее первого. Методология прямо запрещает выдумывать сигналы,
и главная опасность здесь — детектор, который находит сетап там, где
его нет.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analysis import setups
from app.analysis.engine import AnalysisEngine
from app.analysis.indicators import atr as calc_atr, ema, last_value, volume_ratio
from app.analysis.setups import BreakoutRetest, EMAPullback
from app.analysis.signals import MarketContext, validate_geometry, wait_signal
from app.analysis.structure import detect_structure, find_levels
from app.exchanges.base import Kline
from app.execution.service import signal_reference_price
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


# --- геометрия стопа относительно зоны входа ---------------------------------

MIRROR = 400.0


def mirrored(candles: list[Kline]) -> list[Kline]:
    """Зеркало графика по цене: рост превращается в падение, лонг в шорт."""
    return [
        candle(
            i, MIRROR - float(c.open), MIRROR - float(c.low),
            MIRROR - float(c.high), MIRROR - float(c.close), float(c.volume),
        )
        for i, c in enumerate(candles)
    ]


def retest_chart_with_high_confirmation(low: float) -> list[Kline]:
    """Ретест был у уровня 200, а подтверждающая свеча закрылась далеко над
    ним: минимум свечи (low) выше уровня. Геометрия сигнала BNB-USDT 4H:
    зона от уровня до закрытия, а стоп «под минимумом свечи» — внутри зоны."""
    chart = breakout_retest_chart()
    n = len(chart) - 1
    prev = candle(n - 1, low + 1.0, low + 1.4, low, low + 0.2)
    last = candle(n, low + 0.2, low + 5.5, low - 0.2, low + 5.0, volume=2000)
    return chart[:-2] + [prev, last]


class TestStopBelowBrokenLevel:
    """Вариант B: стоп за пробитым уровнем — инвалидация пробоя это возврат
    под уровень. Стоп над уровнем выбивался бы ретестом, ради которого сетап."""

    def test_long_stop_goes_below_level_not_candle_low(self) -> None:
        # Минимум свечи ~203.2 при уровне 200 (как 766.38 против 758.46 у BNB).
        context = build_context(retest_chart_with_high_confirmation(203.4))
        signal = BreakoutRetest().detect(context)

        assert signal.direction is SignalDirection.LONG, signal.note
        assert signal.level_price == D(200)
        assert signal.stop_loss is not None
        candle_low_stop = context.candles[-1].low - context.atr * setups.STOP_BUFFER_ATR
        assert candle_low_stop > D(200), "фикстура должна воспроизводить дефект"
        assert signal.stop_loss < D(200)
        assert signal.stop_loss == setups.round_price(
            D(200) - context.atr * setups.STOP_BUFFER_ATR
        )

    def test_short_stop_goes_above_level(self) -> None:
        context = build_context(mirrored(retest_chart_with_high_confirmation(203.4)))
        signal = BreakoutRetest().detect(context)

        assert signal.direction is SignalDirection.SHORT, signal.note
        assert signal.stop_loss is not None
        assert signal.stop_loss > signal.level_price

    @pytest.mark.parametrize("mirror", [False, True])
    def test_stop_is_outside_whole_entry_zone(self, mirror: bool) -> None:
        chart = retest_chart_with_high_confirmation(203.4)
        signal = BreakoutRetest().detect(build_context(mirrored(chart) if mirror else chart))
        assert signal.is_actionable
        assert validate_geometry(
            signal.direction, signal.entry_zone_low, signal.entry_zone_high, signal.stop_loss
        ) is None

    def test_long_reference_price_is_above_stop(self) -> None:
        """Геометрия ETH 4H (id 54 на проде): середина зоны лежала НИЖЕ стопа
        LONG — SIGNAL_STALE и дрейф считались от цены, которой за стопом
        не бывает. Подтверждающая свеча высоко над уровнем."""
        signal = BreakoutRetest().detect(build_context(retest_chart_with_high_confirmation(212.5)))
        assert signal.direction is SignalDirection.LONG, signal.note
        record = SimpleNamespace(entry_low=signal.entry_zone_low, entry_high=signal.entry_zone_high)
        assert signal_reference_price(record) > signal.stop_loss  # type: ignore[arg-type]

    def test_short_reference_price_is_below_stop(self) -> None:
        chart = mirrored(retest_chart_with_high_confirmation(212.5))
        signal = BreakoutRetest().detect(build_context(chart))
        assert signal.direction is SignalDirection.SHORT, signal.note
        record = SimpleNamespace(entry_low=signal.entry_zone_low, entry_high=signal.entry_zone_high)
        assert signal_reference_price(record) < signal.stop_loss  # type: ignore[arg-type]

    def test_existing_fixture_geometry_unchanged(self) -> None:
        """Когда минимум свечи и так ниже уровня, стоп прежний."""
        signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        assert signal.stop_loss == D("198.5030")
        assert signal.risk_reward == D("2.00")


def pullback_context(*, mirror: bool = False) -> MarketContext:
    """Малая свеча у EMA50: EMA50 ниже минимума свечи с буфером. Контекст
    собирается вручную — EMAPullback нужна структура тренда, а подобрать её
    свечами вместе с касанием EMA50 ненадёжно."""
    chart = breakout_retest_chart()
    n = len(chart) - 1
    prev = candle(n - 1, 203.0, 203.3, 202.4, 202.5)
    last = candle(n, 202.5, 203.6, 202.4, 203.5, volume=2000)
    candles = chart[:-2] + [prev, last]
    if mirror:
        return replace(
            build_context(mirrored(candles)),
            ema50=D(str(MIRROR - 201.2)), structure=MarketStructure.DOWNTREND,
        )
    return replace(
        build_context(candles), ema50=D("201.2"), structure=MarketStructure.UPTREND
    )


class TestEMAPullbackStopBeyondEma50:
    def test_long_stop_goes_below_ema50(self) -> None:
        context = pullback_context()
        signal = EMAPullback().detect(context)
        assert signal.direction is SignalDirection.LONG, signal.note
        candle_low_stop = context.candles[-1].low - context.atr * setups.STOP_BUFFER_ATR
        assert candle_low_stop > context.ema50, "фикстура должна воспроизводить дефект"
        assert signal.stop_loss < context.ema50
        assert signal.stop_loss < signal.entry_zone_low

    def test_short_stop_goes_above_ema50(self) -> None:
        context = pullback_context(mirror=True)
        signal = EMAPullback().detect(context)
        assert signal.direction is SignalDirection.SHORT, signal.note
        assert signal.stop_loss > context.ema50
        assert signal.stop_loss > signal.entry_zone_high


class TestValidateGeometry:
    L, S = SignalDirection.LONG, SignalDirection.SHORT

    def test_long_ok_when_stop_below_zone(self) -> None:
        assert validate_geometry(self.L, D(100), D(105), D(99)) is None

    @pytest.mark.parametrize("stop", ["100", "102", "105", "110"])
    def test_long_violated_when_stop_not_below_zone_low(self, stop: str) -> None:
        assert validate_geometry(self.L, D(100), D(105), D(stop)) is not None

    def test_short_ok_when_stop_above_zone(self) -> None:
        assert validate_geometry(self.S, D(100), D(105), D(106)) is None

    @pytest.mark.parametrize("stop", ["105", "103", "100", "95"])
    def test_short_violated_when_stop_not_above_zone_high(self, stop: str) -> None:
        assert validate_geometry(self.S, D(100), D(105), D(stop)) is not None

    @pytest.mark.parametrize("which", ["breakout", "pullback"])
    def test_detector_returns_wait_with_note_on_violation(
        self, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        monkeypatch.setattr(setups, "validate_geometry", lambda *a: "геометрия нарушена")
        if which == "breakout":
            signal = BreakoutRetest().detect(build_context(breakout_retest_chart()))
        else:
            signal = EMAPullback().detect(pullback_context())
        assert signal.direction is SignalDirection.WAIT
        assert signal.note == "геометрия нарушена"
        assert not signal.is_actionable
