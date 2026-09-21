"""Тесты свечных паттернов и структуры рынка.

Проверяются на свечах, собранных вручную под конкретный случай:
описания из методологии («длинный хвост», «полнотелая свеча») здесь
превращаются в числа, и важно убедиться, что превращение верное.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.patterns import (
    detect_confirmation,
    detect_engulfing,
    detect_pin_bar,
    is_doji,
    is_strong_body,
)
from app.analysis.structure import (
    detect_structure,
    find_levels,
    Level,
    find_swings,
    level_broken,
    level_role,
    nearest_level,
)
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure

D = Decimal
BASE = datetime(2026, 3, 2, tzinfo=UTC)


def bar(o: str, h: str, low: str, c: str, index: int = 0, volume: str = "100") -> Kline:
    open_time = BASE + timedelta(hours=index)
    return Kline(
        open_time=open_time,
        open=D(o), high=D(h), low=D(low), close=D(c),
        volume=D(volume),
        close_time=open_time + timedelta(hours=1),
    )


def series(*prices: tuple[str, str, str, str]) -> list[Kline]:
    return [bar(*p, index=i) for i, p in enumerate(prices)]


def wave(pivots: list[tuple[str, str]], steps: int = 3) -> list[Kline]:
    """Строит свечи, ведущие цену через заданные экстремумы.

    Реальные наборы нужны потому, что поиск экстремумов пропускает
    крайние свечи: на коротком ряду подтверждения справа просто нет,
    и тест проверял бы не то, что нужно.
    """
    candles: list[Kline] = []
    index = 0
    previous = Decimal(pivots[0][0])

    for target_str, kind in pivots:
        target = Decimal(target_str)
        step = (target - previous) / steps
        for n in range(1, steps + 1):
            price = previous + step * n
            reached = n == steps
            high = target if (reached and kind == "high") else price + Decimal("0.5")
            low = target if (reached and kind == "low") else price - Decimal("0.5")
            candles.append(
                bar(
                    str(price - step / 2), str(high), str(low), str(price),
                    index=index,
                )
            )
            index += 1
        previous = target

    return candles


class TestBodyStrength:
    def test_full_bodied_candle_confirms_breakout(self) -> None:
        """Пробой требует уверенного закрытия, а не свечи-волчка."""
        strong = bar("100", "110", "99", "109")   # тело 9 из 11
        assert is_strong_body(strong)

    def test_spinning_top_is_not_confirmation(self) -> None:
        weak = bar("100", "110", "90", "101")     # тело 1 из 20
        assert not is_strong_body(weak)

    def test_doji(self) -> None:
        assert is_doji(bar("100", "105", "95", "100.2"))
        assert not is_doji(bar("100", "110", "99", "109"))


class TestPinBar:
    def test_bullish_pin_bar(self) -> None:
        """Длинный нижний хвост: цена снизу отвергнута."""
        candle = bar("104", "105", "95", "104.5")
        match = detect_pin_bar(candle)
        assert match is not None
        assert match.bullish
        assert "нижний хвост" in match.description

    def test_bearish_pin_bar(self) -> None:
        candle = bar("96", "105", "95", "95.5")
        match = detect_pin_bar(candle)
        assert match is not None
        assert not match.bullish

    def test_big_body_is_not_pin_bar(self) -> None:
        assert detect_pin_bar(bar("95", "105", "94", "104")) is None

    def test_two_long_wicks_is_not_pin_bar(self) -> None:
        """Хвосты с обеих сторон — нерешительность, а не отвержение."""
        assert detect_pin_bar(bar("100", "110", "90", "100.5")) is None

    def test_zero_range_candle(self) -> None:
        assert detect_pin_bar(bar("100", "100", "100", "100")) is None


class TestEngulfing:
    def test_bullish_engulfing(self) -> None:
        previous = bar("105", "106", "100", "101", index=0)   # медвежья
        current = bar("100", "108", "99", "107", index=1)     # бычья, поглощает
        match = detect_engulfing(previous, current)
        assert match is not None
        assert match.bullish

    def test_bearish_engulfing(self) -> None:
        previous = bar("100", "106", "99", "105", index=0)
        current = bar("106", "107", "98", "99", index=1)
        match = detect_engulfing(previous, current)
        assert match is not None
        assert not match.bullish

    def test_same_direction_is_not_engulfing(self) -> None:
        """Поглощение означает смену контроля, а не продолжение."""
        previous = bar("100", "103", "99", "102", index=0)
        current = bar("99", "106", "98", "105", index=1)
        assert detect_engulfing(previous, current) is None

    def test_partial_overlap_is_not_engulfing(self) -> None:
        previous = bar("105", "106", "100", "101", index=0)
        current = bar("102", "104", "101", "103.5", index=1)
        assert detect_engulfing(previous, current) is None


class TestConfirmation:
    def test_finds_bullish_confirmation(self) -> None:
        candles = [
            bar("105", "106", "100", "101", index=0),
            bar("100", "108", "99", "107", index=1),
        ]
        match = detect_confirmation(candles, bullish=True)
        assert match is not None
        assert match.bullish

    def test_wrong_direction_is_rejected(self) -> None:
        """Бычье подтверждение не годится для входа в шорт."""
        candles = [
            bar("105", "106", "100", "101", index=0),
            bar("100", "108", "99", "107", index=1),
        ]
        assert detect_confirmation(candles, bullish=False) is None

    def test_no_pattern_is_valid_answer(self) -> None:
        candles = [
            bar("100", "101", "99", "100.5", index=0),
            bar("100.5", "101", "100", "100.6", index=1),
        ]
        assert detect_confirmation(candles, bullish=True) is None


class TestSwings:
    def test_finds_local_high(self) -> None:
        candles = series(
            ("100", "102", "99", "101"),
            ("101", "104", "100", "103"),
            ("103", "110", "102", "108"),   # вершина
            ("108", "107", "104", "105"),
            ("105", "106", "103", "104"),
        )
        highs = [s for s in find_swings(candles) if s.is_high]
        assert any(s.price == D("110") for s in highs)

    def test_edges_are_skipped(self) -> None:
        """У крайних свечей нет подтверждения справа.

        Объявить их экстремумом значит принять решение по неполным
        данным — ровно то, чего методология требует избегать.
        """
        candles = series(
            ("100", "120", "99", "119"),    # максимум на самом краю
            ("101", "104", "100", "103"),
            ("103", "105", "102", "104"),
            ("104", "106", "103", "105"),
            ("105", "125", "104", "124"),   # и на другом краю
        )
        swings = find_swings(candles)
        assert all(s.price not in (D("120"), D("125")) for s in swings)

    def test_too_few_candles(self) -> None:
        assert find_swings(series(("100", "101", "99", "100"))) == []


class TestStructure:
    def test_uptrend(self) -> None:
        """HH + HL — восходящая структура по методологии."""
        candles = wave([
            ("100", "low"), ("110", "high"), ("104", "low"),
            ("120", "high"), ("114", "low"), ("130", "high"),
            ("124", "low"), ("140", "high"),
        ])
        result = detect_structure(candles)
        assert result.structure is MarketStructure.UPTREND
        assert "растут" in result.description

    def test_downtrend(self) -> None:
        candles = wave([
            ("140", "high"), ("130", "low"), ("136", "high"),
            ("120", "low"), ("126", "high"), ("110", "low"),
            ("116", "high"), ("100", "low"),
        ])
        result = detect_structure(candles)
        assert result.structure is MarketStructure.DOWNTREND

    def test_undefined_on_short_data(self) -> None:
        result = detect_structure(series(("100", "101", "99", "100")))
        assert result.structure is MarketStructure.UNDEFINED


class TestLevels:
    def test_repeated_touches_form_level(self) -> None:
        """Уровень — это место, куда цена возвращалась несколько раз."""
        candles = wave([
            ("100", "low"), ("110", "high"), ("102", "low"),
            ("110", "high"), ("101", "low"), ("110", "high"),
            ("103", "low"),
        ])
        levels = find_levels(candles, atr_value=D("2"))
        assert any(abs(level.price - D("110")) < D("1") for level in levels)

    def test_single_touch_is_not_a_level(self) -> None:
        candles = series(
            ("100", "102", "99", "101"),
            ("101", "130", "100", "129"),   # одиночный выброс
            ("129", "128", "104", "105"),
            ("105", "106", "103", "104"),
            ("104", "105", "102", "103"),
        )
        levels = find_levels(candles, atr_value=D("2"))
        assert not any(level.price > D("120") for level in levels)

    def test_tolerance_scales_with_volatility(self) -> None:
        """Допуск в ATR, а не в процентах.

        Иначе «близко» для BTC при 100 000 и для DOGE при 0.4 означало
        бы одно и то же число, что бессмысленно.
        """
        candles = series(
            ("100", "110", "99", "109"),
            ("109", "108", "104", "105"),
            ("105", "111", "104", "110"),   # 111 против 110
            ("110", "109", "105", "106"),
            ("106", "107", "104", "105"),
        )
        wide = find_levels(candles, atr_value=D("10"))    # 110 и 111 сольются
        narrow = find_levels(candles, atr_value=D("0.5"))  # останутся раздельными
        assert len(wide) <= len(narrow)

    def test_zero_atr_returns_nothing(self) -> None:
        assert find_levels(series(("100", "101", "99", "100")), D(0)) == []

    def test_nearest_level_above_and_below(self) -> None:
        candles = series(
            ("100", "110", "99", "109"),
            ("109", "108", "90", "91"),
            ("91", "110", "90", "109"),
            ("109", "108", "90", "91"),
            ("91", "95", "90", "94"),
        )
        levels = find_levels(candles, atr_value=D("3"))
        if levels:
            above = nearest_level(levels, D("100"), above=True)
            below = nearest_level(levels, D("100"), above=False)
            if above:
                assert above.price > D("100")
            if below:
                assert below.price < D("100")

    def test_no_level_in_direction(self) -> None:
        assert nearest_level([], D("100"), above=True) is None


class TestBreakout:
    def _resistance(self):  # type: ignore[no-untyped-def]
        from app.analysis.structure import Level

        return Level(
            price=D("100"), touches=3, last_touch_index=10,
            is_resistance=True, strength=D("0.8"),
        )

    def test_confident_close_is_breakout(self) -> None:
        candle = bar("99", "103", "98", "102")
        assert level_broken(candle, self._resistance(), D("2"), D("0.15"))

    def test_marginal_close_is_not_breakout(self) -> None:
        """Заглянуть за уровень на пару тиков — это ложный пробой."""
        candle = bar("99", "100.5", "98", "100.1")
        assert not level_broken(candle, self._resistance(), D("2"), D("0.15"))

    def test_wick_above_but_close_below(self) -> None:
        """Классический ложный пробой: хвост вышел, тело вернулось."""
        candle = bar("99", "105", "98", "99.5")
        assert not level_broken(candle, self._resistance(), D("2"), D("0.15"))


class TestLevelRole:
    """Роль уровня на выводе — по положению относительно цены, а не по
    is_resistance (признаку обнаружения)."""

    @staticmethod
    def _level(price: str, *, resistance: bool) -> Level:
        return Level(
            price=D(price), touches=2, last_touch_index=1,
            is_resistance=resistance, strength=D("0.5"),
        )

    def test_level_above_price_is_resistance(self) -> None:
        assert level_role(self._level("110", resistance=False), D("100")) == "resistance"

    def test_broken_resistance_below_price_is_support(self) -> None:
        assert level_role(self._level("95", resistance=True), D("100")) == "support"

    def test_price_equal_to_level_is_support(self) -> None:
        assert level_role(self._level("100", resistance=True), D("100")) == "support"

    def test_detection_flag_is_untouched(self) -> None:
        level = self._level("95", resistance=True)
        level_role(level, D("100"))
        assert level.is_resistance is True
