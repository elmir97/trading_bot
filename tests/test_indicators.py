"""Тесты индикаторов.

Проверяются на наборах с известным результатом. Ошибка в индикаторе не
роняет бота — она выдаёт правдоподобное, но неверное число, на основе
которого будет принято торговое решение. Поэтому проверяется и точность,
и выравнивание рядов по индексам.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.analysis.indicators import (
    IndicatorError,
    atr,
    bollinger_bands,
    ema,
    find_divergence,
    last_value,
    macd,
    rsi,
    sma,
    true_range,
    volume_ratio,
)

D = Decimal


def decimals(*values: str | int | float) -> list[Decimal]:
    return [D(str(v)) for v in values]


class TestAlignment:
    """Длина результата обязана совпадать с длиной входа.

    Если укоротить ряд, индексы перестанут соответствовать свечам, и
    стратегия прочитает значение индикатора не от той свечи. Ошибка
    будет тихой — цифры останутся правдоподобными.
    """

    def test_sma_length_and_padding(self) -> None:
        result = sma(decimals(1, 2, 3, 4, 5), 3)
        assert len(result) == 5
        assert result[:2] == [None, None]
        assert result[2] == D(2)

    def test_ema_length(self) -> None:
        assert len(ema(decimals(*range(1, 21)), 10)) == 20

    def test_rsi_length(self) -> None:
        assert len(rsi(decimals(*range(1, 31)), 14)) == 30

    def test_atr_length(self) -> None:
        values = decimals(*range(1, 31))
        assert len(atr(values, values, values, 14)) == 30

    def test_macd_lengths_match(self) -> None:
        values = decimals(*range(1, 61))
        result = macd(values)
        assert len(result.macd) == len(result.signal) == len(result.histogram) == 60

    def test_bollinger_lengths_match(self) -> None:
        values = decimals(*range(1, 31))
        result = bollinger_bands(values, 20)
        assert len(result.upper) == len(result.middle) == len(result.lower) == 30


class TestSMA:
    def test_known_values(self) -> None:
        result = sma(decimals(10, 20, 30, 40, 50), 3)
        assert result[2] == D(20)
        assert result[3] == D(30)
        assert result[4] == D(40)

    def test_shorter_than_period(self) -> None:
        assert sma(decimals(1, 2), 5) == [None, None]

    def test_period_one_returns_input(self) -> None:
        assert sma(decimals(5, 7, 9), 1) == decimals(5, 7, 9)

    def test_rejects_bad_period(self) -> None:
        with pytest.raises(IndicatorError):
            sma(decimals(1, 2, 3), 0)

    def test_rejects_empty(self) -> None:
        with pytest.raises(IndicatorError):
            sma([], 3)


class TestEMA:
    def test_first_value_is_sma(self) -> None:
        """Первое значение EMA — SMA за период, как на биржевых графиках."""
        values = decimals(10, 20, 30, 40, 50)
        assert ema(values, 3)[2] == D(20)

    def test_known_progression(self) -> None:
        # multiplier = 2/(3+1) = 0.5; после SMA=20 следующее:
        # (40 - 20) * 0.5 + 20 = 30
        values = decimals(10, 20, 30, 40, 50)
        result = ema(values, 3)
        assert result[3] == D(30)
        assert result[4] == D(40)

    def test_reacts_faster_than_sma(self) -> None:
        """Смысл EMA — больший вес свежим данным."""
        values = decimals(*([10] * 20 + [50] * 5))
        ema_last = last_value(ema(values, 10))
        sma_last = last_value(sma(values, 10))
        assert ema_last is not None and sma_last is not None
        assert ema_last > sma_last

    def test_ema200_needs_enough_history(self) -> None:
        """EMA200 — главный фильтр тренда: на коротком ряду её быть не должно."""
        assert last_value(ema(decimals(*range(1, 150)), 200)) is None
        assert last_value(ema(decimals(*range(1, 250)), 200)) is not None


class TestRSI:
    def test_all_gains_gives_hundred(self) -> None:
        result = rsi(decimals(*range(1, 30)), 14)
        assert last_value(result) == D(100)

    def test_all_losses_gives_zero(self) -> None:
        result = rsi(decimals(*range(30, 1, -1)), 14)
        assert last_value(result) == D(0)

    def test_flat_market_is_neutral(self) -> None:
        """Без движения RSI не определён по формуле — отдаём 100, не падаем."""
        result = rsi(decimals(*([100] * 30)), 14)
        assert last_value(result) is not None

    def test_stays_in_range(self) -> None:
        values = decimals(
            44, 44.3, 44.1, 44.5, 45.1, 45.4, 45.4, 46.6, 46.3, 46.3,
            46.0, 46.0, 46.4, 46.2, 45.6, 46.2, 46.2, 46.0, 46.0, 46.4,
        )
        for value in rsi(values, 14):
            if value is not None:
                assert D(0) <= value <= D(100)

    def test_oversold_after_drop(self) -> None:
        values = decimals(*([100] * 15 + [95, 90, 85, 80, 75]))
        result = last_value(rsi(values, 14))
        assert result is not None
        assert result < D(30)


class TestATR:
    def test_true_range_accounts_for_gap(self) -> None:
        """Гэп между свечами обязан войти в диапазон.

        Без учёта закрытия предыдущей свечи стоп, рассчитанный от ATR,
        оказался бы заниженным именно там, где волатильность выше.
        """
        highs = decimals(10, 20)
        lows = decimals(9, 19)
        closes = decimals(9.5, 19.5)
        result = true_range(highs, lows, closes)
        assert result[0] is None
        # max(20-19, |20-9.5|, |19-9.5|) = 10.5, а не 1
        assert result[1] == D("10.5")

    def test_atr_on_constant_range(self) -> None:
        highs = decimals(*([11] * 20))
        lows = decimals(*([10] * 20))
        closes = decimals(*([10.5] * 20))
        assert last_value(atr(highs, lows, closes, 14)) == D(1)

    def test_rejects_mismatched_lengths(self) -> None:
        with pytest.raises(IndicatorError):
            true_range(decimals(1, 2), decimals(1), decimals(1, 2))


class TestMACD:
    def test_positive_in_uptrend(self) -> None:
        values = decimals(*range(1, 61))
        result = macd(values)
        last = last_value(result.macd)
        assert last is not None and last > 0

    def test_negative_in_downtrend(self) -> None:
        values = decimals(*range(60, 0, -1))
        last = last_value(macd(values).macd)
        assert last is not None and last < 0

    def test_histogram_is_difference(self) -> None:
        values = decimals(*range(1, 61))
        result = macd(values)
        for m, s, h in zip(result.macd, result.signal, result.histogram, strict=True):
            if m is not None and s is not None:
                assert h == m - s

    def test_rejects_fast_above_slow(self) -> None:
        with pytest.raises(IndicatorError):
            macd(decimals(*range(1, 61)), fast=26, slow=12)


class TestBollinger:
    def test_bands_around_mean(self) -> None:
        values = decimals(*([100] * 25))
        result = bollinger_bands(values, 20)
        # Без волатильности полосы схлопываются на средней.
        assert result.upper[-1] == result.middle[-1] == result.lower[-1] == D(100)

    def test_wider_bands_on_volatility(self) -> None:
        calm = decimals(*([100, 101] * 15))
        wild = decimals(*([80, 120] * 15))

        calm_result = bollinger_bands(calm, 20)
        wild_result = bollinger_bands(wild, 20)

        calm_width = calm_result.upper[-1] - calm_result.lower[-1]  # type: ignore[operator]
        wild_width = wild_result.upper[-1] - wild_result.lower[-1]  # type: ignore[operator]
        assert wild_width > calm_width

    def test_upper_above_lower(self) -> None:
        values = decimals(*[100 + (i % 7) for i in range(40)])
        result = bollinger_bands(values, 20)
        for upper, lower in zip(result.upper, result.lower, strict=True):
            if upper is not None and lower is not None:
                assert upper >= lower


class TestVolumeRatio:
    def test_spike_detected(self) -> None:
        """Методология требует всплеска объёма при истинном пробое."""
        volumes = decimals(*([100] * 20 + [200]))
        result = last_value(volume_ratio(volumes, 20))
        assert result is not None
        assert result >= D("1.5")

    def test_normal_volume_is_about_one(self) -> None:
        volumes = decimals(*([100] * 25))
        assert last_value(volume_ratio(volumes, 20)) == D(1)


class TestDivergence:
    def test_bearish_divergence(self) -> None:
        """Цена выше, осциллятор ниже — признак истощения роста."""
        prices = decimals(*([100 + i for i in range(15)] + [120 + i for i in range(15)]))
        oscillator = [D(80 - i) for i in range(30)]
        assert find_divergence(prices, oscillator) == "bearish"

    def test_bullish_divergence(self) -> None:
        prices = decimals(*([100 - i for i in range(15)] + [80 - i for i in range(15)]))
        oscillator = [D(20 + i) for i in range(30)]
        assert find_divergence(prices, oscillator) == "bullish"

    def test_no_divergence_when_aligned(self) -> None:
        prices = decimals(*range(1, 31))
        oscillator = [D(i) for i in range(1, 31)]
        assert find_divergence(prices, oscillator) is None

    def test_insufficient_data(self) -> None:
        assert find_divergence(decimals(1, 2, 3), [D(1), D(2), D(3)]) is None
