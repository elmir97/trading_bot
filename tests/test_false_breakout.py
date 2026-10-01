"""app/analysis/false_breakout.py — ложный пробой по тренду, на синтетике.

Диапазон: свинг-максимумы ровно 110 (сопротивление, много касаний),
свинг-минимумы ровно 90 (поддержка), между ними шум 97–103.4. EMA200 D1
задаётся в контексте явно: 200 — цена ниже (только SHORT), 50 — выше
(только LONG).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.engine import context_from_candles
from app.analysis.false_breakout import FalseBreakout, _atr_before
from app.analysis.setups import STOP_BUFFER_ATR, round_price
from app.analysis.signals import Signal
from app.analysis.structure import find_levels, nearest_level
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection, TargetSource

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
DOWN = D(200)   # EMA200 D1 выше цены — нисходящий тренд, только SHORT
UP = D(50)      # ниже цены — восходящий, только LONG


def bar(i: int, o: str, h: str, low: str, c: str, v: str = "1000") -> Kline:
    t = T0 + H * i
    return Kline(
        open_time=t, open=D(o), high=D(h), low=D(low), close=D(c), volume=D(v),
        close_time=t + H,
    )


def base_range(n: int = 60) -> list[Kline]:
    out: list[Kline] = []
    for i in range(n):
        if i % 10 == 5:
            out.append(bar(i, "103", "110", "101", "104"))
        elif i % 10 == 0:
            out.append(bar(i, "97", "99", "90", "96"))
        else:
            out.append(bar(
                i, "100", str(D(103) + D((i * 7) % 5) / 10), str(D(97) - D((i * 3) % 5) / 10),
                "100",
            ))
    return out


def detect(
    candles: list[Kline], trend: Decimal | None, **params: object
) -> Signal:
    det = FalseBreakout(**{"probe_atr": D("0.1"), "return_bars": 1, **params})  # type: ignore[arg-type]
    return det.detect(context_from_candles("BTC-USDT", "1h", candles, d1_ema200=trend))


def with_tail(*tail: tuple[str, str, str, str] | tuple[str, str, str, str, str]) -> list[Kline]:
    candles = base_range()
    for b in tail:
        candles.append(bar(len(candles), *b))
    return candles


# --- сигнал по тренду ---------------------------------------------------------


def test_short_wick_probe_and_return_by_trend() -> None:
    """Прокол 110 тенью до 116, закрытие 106 под уровнем, цена ниже EMA200 D1."""
    candles = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106"))
    sig = detect(candles, DOWN)

    assert sig.is_actionable and sig.direction is SignalDirection.SHORT
    p = len(candles) - 1
    atr = _atr_before(candles[:p])
    assert atr is not None
    assert sig.level_price == D(110)
    assert sig.entry_zone_low == D(106)   # вход — закрытие свечи возврата
    assert sig.stop_loss == round_price(D(116) + atr * STOP_BUFFER_ATR)
    assert sig.probe_at == candles[p].open_time
    risk = abs(D(106) - sig.stop_loss)
    target = nearest_level(find_levels(candles[:p], atr), D(106), above=False)
    if target is not None and abs(target.price - D(106)) / risk >= D("1.5"):
        assert sig.take_profit_1 == round_price(target.price)
        assert sig.target_source is TargetSource.LEVEL
    else:
        assert sig.take_profit_1 == round_price(D(106) - risk * 2)
        assert sig.target_source is TargetSource.FORMULA_2R


def test_long_mirror() -> None:
    """Прокол поддержки 90 вниз до 84, закрытие 94 над ней, цена выше EMA200 D1."""
    candles = with_tail(("100", "101", "96", "96"), ("96", "97", "84", "94"))
    sig = detect(candles, UP)
    assert sig.is_actionable and sig.direction is SignalDirection.LONG
    assert sig.level_price == D(90)
    assert sig.entry_zone_high == D(94)
    assert sig.stop_loss is not None and sig.stop_loss < D(84)


def test_against_trend_no_signal() -> None:
    """Та же картина прокола вверх, но цена выше EMA200 D1 — ни SHORT, ни LONG."""
    candles = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106"))
    assert not detect(candles, UP).is_actionable


def test_no_d1_ema_no_signal() -> None:
    candles = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106"))
    assert not detect(candles, None).is_actionable


# --- прокол без возврата, возврат позже N ------------------------------------------


def test_probe_without_return_no_signal() -> None:
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "112"),
        ("112", "114", "111", "113"), ("113", "115", "111", "112"),
    )
    for t in range(len(candles) - 3, len(candles) + 1):
        assert not detect(candles[:t], DOWN, return_bars=3).is_actionable


@pytest.mark.parametrize(("bars", "expected"), [(1, False), (2, False), (3, True)])
def test_return_later_than_n(bars: int, expected: bool) -> None:
    """Прокол закрылся над уровнем, ещё две свечи над ним, возврат на 3-й после."""
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "112"),
        ("112", "113", "110.5", "111"), ("111", "112", "110.2", "111"),
        ("111", "111.5", "104", "106"),
    )
    assert detect(candles, DOWN, return_bars=bars).is_actionable is expected


def test_return_on_next_bar_with_n1() -> None:
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "112"), ("112", "112.5", "104", "106"),
    )
    sig = detect(candles, DOWN, return_bars=1)
    assert sig.is_actionable and sig.direction is SignalDirection.SHORT
    assert sig.probe_at == candles[-2].open_time


# --- порог прокола d ------------------------------------------------------------------


@pytest.mark.parametrize(("d", "expected"), [("0.1", True), ("0.3", False)])
def test_probe_depth_threshold(d: str, expected: bool) -> None:
    candles = with_tail(("100", "104", "99", "104"))
    atr = _atr_before(candles)
    assert atr is not None
    high = round_price(D(110) + atr * D("0.2"))
    candles.append(bar(len(candles), "104", str(high), "103", "106"))
    assert detect(candles, DOWN, probe_atr=D(d)).is_actionable is expected


# --- нет заглядывания в будущее ----------------------------------------------------------


def test_level_only_from_future_touch_gives_no_signal() -> None:
    """Касание 110 до прокола одно, вторым было бы касание самим проколом:
    уровни берутся только по свечам до прокола (≥ 2 касаний) — уровня нет,
    сигнала нет."""
    candles = base_range(40)
    candles = [
        c if c.high != D(110) or c.open_time == candles[25].open_time
        else bar(i, "100", "103", "97", "100")
        for i, c in enumerate(candles)
    ]
    candles.append(bar(40, "100", "104", "99", "104"))
    candles.append(bar(41, "104", "110.3", "103", "106"))
    assert not detect(candles, DOWN).is_actionable


def test_signal_only_on_return_candle_not_repeated() -> None:
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "106"), ("106", "107", "101", "102"),
    )
    assert detect(candles[:-1], DOWN).is_actionable
    assert not detect(candles, DOWN).is_actionable


def test_prefix_result_independent_of_future() -> None:
    """Решение на свече t считается только по candles[: t + 1]: добавленные
    позже свечи сигнал на t не меняют (replay подаёт ровно такой префикс)."""
    tail = [("100", "104", "99", "104"), ("104", "116", "103", "106")]
    a = detect(with_tail(*tail), DOWN)
    b = detect(with_tail(*tail, ("106", "130", "60", "70"))[:-1], DOWN)
    key = (a.direction, a.stop_loss, a.take_profit_1)
    assert key == (b.direction, b.stop_loss, b.take_profit_1)


# --- фильтры -----------------------------------------------------------------------------


@pytest.mark.parametrize(("volume", "expected"), [("1000", False), ("5000", True)])
def test_probe_volume_filter(volume: str, expected: bool) -> None:
    candles = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106", volume))
    assert detect(candles, DOWN, min_probe_volume_ratio=D("1.3")).is_actionable is expected
    assert detect(candles, DOWN).is_actionable  # без фильтра объём не важен


def test_confirm_next_enters_on_next_close() -> None:
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "106"), ("106", "107", "100", "101"),
    )
    assert not detect(candles[:-1], DOWN, confirm_next=True).is_actionable
    sig = detect(candles, DOWN, confirm_next=True)
    assert sig.is_actionable and sig.entry_zone_low == D(101)


def test_confirm_next_rejected_when_back_above() -> None:
    candles = with_tail(
        ("100", "104", "99", "104"), ("104", "116", "103", "106"), ("106", "113", "105", "111"),
    )
    assert not detect(candles, DOWN, confirm_next=True).is_actionable


def test_return_bars_bounds() -> None:
    with pytest.raises(ValueError):
        FalseBreakout(probe_atr=D("0.1"), return_bars=4)


def test_cache_does_not_mix_contexts_with_same_times() -> None:
    """Два окна с одинаковыми временами и ценами, но разным объёмом прокола —
    каждое считается по своим свечам (ключ кэша — сам список свечей)."""
    low = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106", "1000"))
    high = with_tail(("100", "104", "99", "104"), ("104", "116", "103", "106", "5000"))
    vol = D("1.3")
    assert not detect(low, DOWN, min_probe_volume_ratio=vol).is_actionable
    assert detect(high, DOWN, min_probe_volume_ratio=vol).is_actionable
    assert not detect(low, DOWN, min_probe_volume_ratio=vol).is_actionable
