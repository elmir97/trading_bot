"""Финальный тест стратегии (решение владельца 01.10) — чистые функции, без сети.

Две конфигурации, зафиксированы до прогона:

A. Старая стратегия (сканер, H4) + фильтр: сигнал только если ADX(14) D1 ≥ 25
   и направление по EMA200 D1 (LONG — цена выше, SHORT — ниже). Только
   закрытые к сигналу дни.

B. Тренд D1. LONG — закрытие дня выше максимума high предыдущих 20 дней (без
   самого дня) и выше EMA200 D1; SHORT зеркально. Вход по закрытию дня, риск —
   2×ATR(14) D1 (начальный стоп). Выход — трейлинг 3×ATR (текущий ATR) от
   экстремума после входа; стоп только подтягивается и действует со
   следующего дня; гэп за стопом — выход по open. Тейка нет, максимум 120
   дней — выход по закрытию 120-го дня. Новый вход по символу — со дня после
   выхода. Позиция, открытая на конце окна, — «открыт».
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.analysis.indicators import atr as calc_atr
from app.analysis.indicators import ema
from app.exchanges.base import Kline
from app.trading.enums import SignalDirection

ZERO = Decimal(0)
HUNDRED = Decimal(100)
ADX_PERIOD = 14
ADX_MIN = Decimal(25)
EMA_PERIOD = 200
ATR_PERIOD = 14
DONCHIAN_DAYS = 20
STOP_ATR = Decimal(2)
TRAIL_ATR = Decimal(3)
MAX_HOLD_DAYS = 120

EXIT_TRAIL = "трейлинг"
EXIT_INITIAL = "начальный стоп"
EXIT_TIME = f"{MAX_HOLD_DAYS} дней"
EXIT_OPEN = "открыт"


def adx(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal],
    period: int = ADX_PERIOD,
) -> list[Decimal | None]:
    """ADX по Уайлдеру. TR/+DM/−DM с i=1; суммы за первые period значений,
    дальше s = s − s/period + x; DX с индекса period; ADX — среднее первых
    period DX (индекс 2·period − 1), дальше (ADX·(period−1) + DX)/period."""
    n = len(closes)
    out: list[Decimal | None] = [None] * n
    if n < 2 * period:
        return out
    tr: list[Decimal] = []
    pdm: list[Decimal] = []
    mdm: list[Decimal] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm.append(up if up > down and up > ZERO else ZERO)
        mdm.append(down if down > up and down > ZERO else ZERO)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    s_tr = sum(tr[:period], ZERO)
    s_p = sum(pdm[:period], ZERO)
    s_m = sum(mdm[:period], ZERO)
    dx: list[Decimal] = []

    def _dx() -> Decimal:
        if s_tr == ZERO:
            return ZERO
        pdi = HUNDRED * s_p / s_tr
        mdi = HUNDRED * s_m / s_tr
        total = pdi + mdi
        return ZERO if total == ZERO else HUNDRED * abs(pdi - mdi) / total

    dx.append(_dx())   # индекс свечи period
    for j in range(period, len(tr)):
        s_tr = s_tr - s_tr / period + tr[j]
        s_p = s_p - s_p / period + pdm[j]
        s_m = s_m - s_m / period + mdm[j]
        dx.append(_dx())   # индекс свечи j + 1
    value = sum(dx[:period], ZERO) / period
    out[2 * period - 1] = value
    for k in range(period, len(dx)):
        value = (value * (period - 1) + dx[k]) / period
        out[period + k] = value
    return out


@dataclass(frozen=True, slots=True)
class D1Series:
    """Дневные свечи и их индикаторы; значение дня i — по дням ≤ i."""

    days: Sequence[Kline]
    close_times: list[datetime]
    ema200: list[Decimal | None]
    adx: list[Decimal | None]
    atr: list[Decimal | None]

    def last_closed(self, at: datetime) -> int | None:
        """Индекс последнего дня, закрытого к `at`."""
        i = bisect_right(self.close_times, at) - 1
        return i if i >= 0 else None


def d1_series(days: Sequence[Kline]) -> D1Series:
    highs = [d.high for d in days]
    lows = [d.low for d in days]
    closes = [d.close for d in days]
    atr_values = (
        calc_atr(highs, lows, closes, ATR_PERIOD) if len(days) > ATR_PERIOD
        else [None] * len(days)
    )
    return D1Series(
        days=days,
        close_times=[d.close_time for d in days],
        ema200=ema(closes, EMA_PERIOD) if len(days) >= EMA_PERIOD else [None] * len(days),
        adx=adx(highs, lows, closes, ADX_PERIOD),
        atr=atr_values,
    )


def filter_a(
    direction: SignalDirection, price: Decimal, series: D1Series, at: datetime
) -> bool:
    """A: ADX(14) последнего закрытого дня ≥ 25 и направление по EMA200 D1."""
    i = series.last_closed(at)
    if i is None:
        return False
    adx_value, ema_value = series.adx[i], series.ema200[i]
    if adx_value is None or ema_value is None or adx_value < ADX_MIN:
        return False
    if direction is SignalDirection.LONG:
        return price > ema_value
    if direction is SignalDirection.SHORT:
        return price < ema_value
    return False


@dataclass(frozen=True, slots=True)
class TrendTrade:
    symbol: str
    direction: SignalDirection
    entry_at: datetime
    entry: Decimal
    initial_stop: Decimal
    risk: Decimal
    exit_at: datetime | None
    exit_price: Decimal | None
    exit_reason: str
    days_held: int | None

    @property
    def r_gross(self) -> Decimal | None:
        if self.exit_price is None:
            return None
        move = self.exit_price - self.entry
        sign = 1 if self.direction is SignalDirection.LONG else -1
        return sign * move / self.risk

    def r_net(self, fee_rate: Decimal) -> Decimal | None:
        gross = self.r_gross
        if gross is None or self.exit_price is None:
            return None
        return gross - fee_rate * (self.entry + self.exit_price) / self.risk


def simulate_trailing(
    days: Sequence[Kline],
    entry_index: int,
    long_: bool,
    entry: Decimal,
    stop: Decimal,
    atr_values: Sequence[Decimal | None],
    end: datetime,
    max_days: int = MAX_HOLD_DAYS,
) -> tuple[int | None, Decimal | None, str]:
    """(индекс дня выхода, цена, причина). Стоп текущего дня — с закрытия
    прошлого; после проверки выхода — подтяжка: экстремум с входа ∓ 3·ATR дня,
    только в сторону цены. Дни после `end` не смотрятся — «открыт»."""
    extreme = entry
    reason = EXIT_INITIAL
    for k in range(entry_index + 1, len(days)):
        day = days[k]
        if day.close_time > end:
            break
        if long_:
            if day.open <= stop:
                return k, day.open, reason
            if day.low <= stop:
                return k, stop, reason
        else:
            if day.open >= stop:
                return k, day.open, reason
            if day.high >= stop:
                return k, stop, reason
        if k - entry_index >= max_days:
            return k, day.close, EXIT_TIME
        extreme = max(extreme, day.high) if long_ else min(extreme, day.low)
        atr_value = atr_values[k]
        if atr_value is not None:
            trail = extreme - TRAIL_ATR * atr_value if long_ else extreme + TRAIL_ATR * atr_value
            if (long_ and trail > stop) or (not long_ and trail < stop):
                stop = trail
                reason = EXIT_TRAIL
    return None, None, EXIT_OPEN


def trend_trades(
    symbol: str, series: D1Series, start: datetime, end: datetime,
    max_days: int = MAX_HOLD_DAYS,
) -> list[TrendTrade]:
    """Сделки B по дням, закрытым в [start; end]; одна позиция на символ."""
    days = series.days
    trades: list[TrendTrade] = []
    i = 0
    while i < len(days):
        day = days[i]
        if day.close_time < start or i < DONCHIAN_DAYS:
            i += 1
            continue
        if day.close_time > end:
            break
        ema_value, atr_value = series.ema200[i], series.atr[i]
        if ema_value is None or atr_value is None or atr_value <= ZERO:
            i += 1
            continue
        prior = days[i - DONCHIAN_DAYS: i]
        close = day.close
        if close > max(d.high for d in prior) and close > ema_value:
            direction = SignalDirection.LONG
        elif close < min(d.low for d in prior) and close < ema_value:
            direction = SignalDirection.SHORT
        else:
            i += 1
            continue
        long_ = direction is SignalDirection.LONG
        risk = STOP_ATR * atr_value
        stop = close - risk if long_ else close + risk
        k, price, reason = simulate_trailing(
            days, i, long_, close, stop, series.atr, end, max_days
        )
        trades.append(TrendTrade(
            symbol=symbol, direction=direction, entry_at=day.close_time, entry=close,
            initial_stop=stop, risk=risk,
            exit_at=days[k].close_time if k is not None else None,
            exit_price=price, exit_reason=reason,
            days_held=k - i if k is not None else None,
        ))
        if k is None:
            break
        i = k + 1
    return trades
