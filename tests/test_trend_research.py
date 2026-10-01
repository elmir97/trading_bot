"""scripts/trend_research.py — ADX, фильтр A, тренд D1 (B) с трейлингом. Синтетика,
без сети. Каждый тест падает без модуля."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from app.exchanges.base import Kline
from app.trading.enums import SignalDirection
from scripts import trend_research as tr

D = Decimal
T0 = datetime(2023, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def day(i: int, o: str, h: str, low: str, c: str) -> Kline:
    t = T0 + DAY * i
    return Kline(
        open_time=t, open=D(o), high=D(h), low=D(low), close=D(c), volume=D(1),
        close_time=t + DAY,
    )


# --- ADX -------------------------------------------------------------------------


def _adx_reference(h: list[int], lo: list[int], c: list[int], p: int) -> list[Fraction | None]:
    """Учебный ADX Уайлдера на дробях — независимо от реализации."""
    n = len(c)
    trs, pdms, mdms = [], [], []
    for i in range(1, n):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdms.append(Fraction(up if up > down and up > 0 else 0))
        mdms.append(Fraction(down if down > up and down > 0 else 0))
        trs.append(Fraction(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))))

    def smooth(xs: list[Fraction]) -> list[Fraction]:
        s = [sum(xs[:p], Fraction(0))]
        for x in xs[p:]:
            s.append(s[-1] - s[-1] / p + x)
        return s

    st, sp, sm = smooth(trs), smooth(pdms), smooth(mdms)
    dxs = []
    for a, b, t in zip(sp, sm, st, strict=True):
        pdi, mdi = 100 * a / t, 100 * b / t
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    out: list[Fraction | None] = [None] * n
    value = sum(dxs[:p], Fraction(0)) / p
    out[2 * p - 1] = value
    for k in range(p, len(dxs)):
        value = (value * (p - 1) + dxs[k]) / p
        out[p + k] = value
    return out


def test_adx_matches_reference_period_3() -> None:
    h = [10, 12, 11, 14, 13, 16, 15, 17, 16]
    lo = [8, 9, 9, 11, 10, 12, 13, 14, 13]
    c = [9, 11, 10, 13, 12, 15, 14, 16, 14]
    got = tr.adx([D(x) for x in h], [D(x) for x in lo], [D(x) for x in c], 3)
    ref = _adx_reference(h, lo, c, 3)
    assert [g is None for g in got] == [r is None for r in ref]
    for g, r in zip(got, ref, strict=True):
        if g is not None and r is not None:
            assert abs(float(g) - float(r)) < 1e-9


def test_adx_trend_high_and_sawtooth_low() -> None:
    up = list(range(100, 160))
    trend = tr.adx([D(x + 1) for x in up], [D(x - 1) for x in up], [D(x) for x in up], 14)
    saw = [100 + (5 if i % 2 else -5) for i in range(60)]
    flat = tr.adx([D(x + 1) for x in saw], [D(x - 1) for x in saw], [D(x) for x in saw], 14)
    assert trend[-1] is not None and trend[-1] > 90
    assert flat[-1] is not None and flat[-1] < 20


def test_adx_too_short_is_none() -> None:
    xs = [D(i) for i in range(27)]
    assert all(v is None for v in tr.adx(xs, xs, xs, 14))


# --- фильтр A ------------------------------------------------------------------------


def _series(close: str, adx_value: str | None, ema_value: str, n: int = 3) -> tr.D1Series:
    days = [day(i, close, close, close, close) for i in range(n)]
    return tr.D1Series(
        days=days, close_times=[d.close_time for d in days],
        ema200=[D(ema_value)] * n,
        adx=[None if adx_value is None else D(adx_value)] * n,
        atr=[D(1)] * n,
    )


@pytest.mark.parametrize(
    ("direction", "price", "adx_value", "expected"),
    [
        (SignalDirection.LONG, "110", "25", True),
        (SignalDirection.LONG, "110", "24.9", False),     # ADX ниже 25
        (SignalDirection.LONG, "90", "30", False),        # против тренда
        (SignalDirection.SHORT, "90", "30", True),
        (SignalDirection.SHORT, "110", "30", False),
        (SignalDirection.LONG, "110", None, False),       # ADX нет
    ],
)
def test_filter_a(
    direction: SignalDirection, price: str, adx_value: str | None, expected: bool
) -> None:
    series = _series("100", adx_value, "100")
    assert tr.filter_a(direction, D(price), series, series.days[-1].close_time) is expected


def test_filter_a_uses_only_closed_days() -> None:
    days = [day(i, "100", "100", "100", "100") for i in range(3)]
    series = tr.D1Series(
        days=days, close_times=[d.close_time for d in days],
        ema200=[D(100)] * 3, adx=[D(10), D(10), D(40)], atr=[D(1)] * 3,
    )
    # День 2 закрывается в конце — до его закрытия ADX ещё 10
    before_close = days[2].close_time - timedelta(hours=1)
    assert not tr.filter_a(SignalDirection.LONG, D(110), series, before_close)
    assert tr.filter_a(SignalDirection.LONG, D(110), series, days[2].close_time)
    assert not tr.filter_a(SignalDirection.LONG, D(110), series, days[0].open_time)


# --- B: вход -----------------------------------------------------------------------------


def _flat(n: int, price: str = "100") -> list[Kline]:
    p = D(price)
    return [day(i, price, str(p + 2), str(p - 2), price) for i in range(n)]


def _b_series(days: list[Kline], ema_value: str = "50", atr_value: str = "2") -> tr.D1Series:
    n = len(days)
    return tr.D1Series(
        days=days, close_times=[d.close_time for d in days],
        ema200=[D(ema_value)] * n, adx=[None] * n, atr=[D(atr_value)] * n,
    )


def test_b_long_breakout_above_20_day_high_and_ema() -> None:
    days = [*_flat(25), day(25, "100", "106", "99", "105")]
    trades = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction is SignalDirection.LONG and t.entry == D(105)
    assert t.risk == D(4) and t.initial_stop == D(101)
    assert t.exit_reason == tr.EXIT_OPEN and t.exit_price is None


def test_b_no_long_below_ema() -> None:
    days = [*_flat(25), day(25, "100", "106", "99", "105")]
    series = _b_series(days, ema_value="200")
    assert tr.trend_trades("BTC-USDT", series, T0, days[-1].close_time) == []


def test_b_breakout_level_excludes_today() -> None:
    """Сегодняшний high не входит в 20-дневный максимум: закрытие 101 выше
    прошлых максимумов 102? нет — сигнала нет; 103 — есть."""
    no = [*_flat(25), day(25, "100", "110", "99", "101")]
    yes = [*_flat(25), day(25, "100", "110", "99", "103")]
    assert tr.trend_trades("BTC-USDT", _b_series(no), T0, no[-1].close_time) == []
    assert len(tr.trend_trades("BTC-USDT", _b_series(yes), T0, yes[-1].close_time)) == 1


def test_b_short_mirror() -> None:
    days = [*_flat(25), day(25, "100", "101", "94", "95")]
    trades = tr.trend_trades("BTC-USDT", _b_series(days, ema_value="200"), T0, days[-1].close_time)
    assert len(trades) == 1 and trades[0].direction is SignalDirection.SHORT
    assert trades[0].initial_stop == D(99)


# --- B: трейлинг и выход ----------------------------------------------------------------------


def _after_entry(*tail: tuple[str, str, str, str]) -> list[Kline]:
    days = [*_flat(25), day(25, "100", "106", "99", "105")]
    for b in tail:
        days.append(day(len(days), *b))
    return days


def test_b_trailing_ratchets_and_exits_at_stop() -> None:
    """ATR 2: экстремум 120 → стоп 114; откат до 113 — выход по 114, трейлинг."""
    days = _after_entry(("105", "120", "104", "118"), ("118", "119", "113", "115"))
    [t] = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert t.exit_reason == tr.EXIT_TRAIL and t.exit_price == D(114)
    assert t.days_held == 2
    assert t.r_gross == D(9) / D(4)


def test_b_stop_never_loosens() -> None:
    """ATR растёт — трейлинг ниже прежнего стопа не уходит."""
    days = _after_entry(("105", "120", "104", "118"), ("118", "119", "114.5", "116"))
    series = _b_series(days)
    atr = list(series.atr)
    atr[-1] = D(10)   # на второй день ATR 10: «трейлинг» 120 − 30 = 90 < 114
    loose = tr.D1Series(series.days, series.close_times, series.ema200, series.adx, atr)
    [t] = tr.trend_trades("BTC-USDT", loose, T0, days[-1].close_time)
    assert t.exit_reason == tr.EXIT_OPEN   # 114.5 выше 114 — не выбило, стоп не отъехал к 90
    days2 = [*days, day(len(days), "116", "117", "100", "101")]
    s2 = _b_series(days2)
    atr2 = list(s2.atr)
    atr2[-2] = D(10)
    [t2] = tr.trend_trades(
        "BTC-USDT", tr.D1Series(s2.days, s2.close_times, s2.ema200, s2.adx, atr2),
        T0, days2[-1].close_time,
    )
    assert t2.exit_price == D(114)


def test_b_gap_below_stop_exits_at_open() -> None:
    days = _after_entry(("105", "120", "104", "118"), ("110", "111", "108", "109"))
    [t] = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert t.exit_price == D(110)   # open 110 ниже стопа 114


def test_b_initial_stop_reason_when_never_trailed() -> None:
    days = _after_entry(("105", "106", "100", "101"))
    [t] = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert t.exit_reason == tr.EXIT_INITIAL and t.exit_price == D(101)
    assert t.r_gross == D(-1)


def test_b_time_exit_at_close() -> None:
    days = _after_entry(*[("106", "107", "105", "106")] * 5)
    [t] = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time, max_days=3)
    assert t.exit_reason == tr.EXIT_TIME and t.days_held == 3 and t.exit_price == D(106)


def test_b_no_new_entry_while_open_then_reentry_after_exit() -> None:
    """Пока позиция открыта, новые пробои не входят; после выхода — со следующего дня."""
    days = _after_entry(
        ("105", "120", "104", "118"),
        ("118", "125", "117", "124"),   # ещё пробой — позиция открыта, не вход
        ("124", "124", "110", "112"),   # выбило по трейлингу (125 − 6 = 119)
        ("112", "130", "111", "129"),   # новый пробой — вход
    )
    trades = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert len(trades) == 2
    assert trades[0].exit_at == days[-2].close_time
    assert trades[1].entry_at == days[-1].close_time


def test_b_future_days_do_not_change_past_exit() -> None:
    base = _after_entry(("105", "120", "104", "118"), ("118", "119", "113", "115"))
    more = [*base, day(len(base), "115", "200", "50", "150")]
    a = tr.trend_trades("BTC-USDT", _b_series(base), T0, base[-1].close_time)
    b = tr.trend_trades("BTC-USDT", _b_series(more), T0, more[-1].close_time)
    assert (a[0].exit_at, a[0].exit_price) == (b[0].exit_at, b[0].exit_price)


def test_b_r_net_fee() -> None:
    days = _after_entry(("105", "106", "100", "101"))
    [t] = tr.trend_trades("BTC-USDT", _b_series(days), T0, days[-1].close_time)
    assert t.r_net(D("0.0005")) == D(-1) - D("0.0005") * (D(105) + D(101)) / D(4)


def test_d1_series_values_only_from_past() -> None:
    days = _flat(230)
    s1 = tr.d1_series(days)
    s2 = tr.d1_series([*days, day(230, "100", "500", "1", "400")])
    assert s1.ema200[-1] == s2.ema200[229]
    assert s1.adx[-1] == s2.adx[229]
    assert s1.atr[-1] == s2.atr[229]
