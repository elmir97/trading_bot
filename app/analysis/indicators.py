"""Технические индикаторы.

Чистые функции над последовательностями Decimal. Ни биржи, ни базы, ни
времени — только числа на входе и на выходе. Благодаря этому каждый
индикатор проверяется на эталонных значениях.

Соглашение о длине результата: функции возвращают список той же длины,
что и вход, а позиции, для которых значения ещё нет, заполнены None.
Это важнее, чем кажется: если укоротить результат, индексы перестанут
совпадать со свечами, и стратегия начнёт читать значение не от той
свечи. Ошибка при этом будет тихой.

Decimal, а не float, по той же причине, что и везде в проекте: цены
приходят с биржи строками, и терять точность по дороге незачем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, getcontext

# Точности хватает с запасом даже для цепочек EMA на тысячах свечей.
getcontext().prec = 28

ZERO = Decimal(0)


class IndicatorError(ValueError):
    """Некорректные параметры расчёта."""


def _validate(values: Sequence[Decimal], period: int, name: str) -> None:
    if period < 1:
        raise IndicatorError(f"{name}: период должен быть положительным")
    if not values:
        raise IndicatorError(f"{name}: пустой набор данных")


def sma(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Простая скользящая средняя."""
    _validate(values, period, "SMA")

    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result

    window = sum(values[:period], ZERO)
    result[period - 1] = window / period

    for i in range(period, len(values)):
        # Скользящее окно вместо пересчёта суммы: на 500 свечах разница
        # незаметна, но индикаторы считаются для каждого символа и ТФ.
        window += values[i] - values[i - period]
        result[i] = window / period

    return result


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Экспоненциальная скользящая средняя.

    Первое значение — SMA за период: так делает большинство платформ,
    и без этого EMA расходилась бы с тем, что пользователь видит на
    графике TradingView.
    """
    _validate(values, period, "EMA")

    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result

    multiplier = Decimal(2) / (Decimal(period) + 1)
    previous = sum(values[:period], ZERO) / period
    result[period - 1] = previous

    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        result[i] = previous

    return result


def rsi(values: Sequence[Decimal], period: int = 14) -> list[Decimal | None]:
    """Индекс относительной силы по Уайлдеру.

    Сглаживание именно по Уайлдеру, а не простое среднее: это
    классический вариант, и от него зависят уровни 30/70, на которые
    все смотрят.
    """
    _validate(values, period, "RSI")

    result: list[Decimal | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(change if change > ZERO else ZERO)
        losses.append(-change if change < ZERO else ZERO)

    avg_gain = sum(gains[:period], ZERO) / period
    avg_loss = sum(losses[:period], ZERO) / period
    result[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi_value(avg_gain, avg_loss)

    return result


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    # Без убытков RSI равен 100 по определению, деление на ноль тут не
    # ошибка данных, а корректный крайний случай.
    if avg_loss == ZERO:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


@dataclass(frozen=True, slots=True)
class MACDResult:
    macd: list[Decimal | None]
    signal: list[Decimal | None]
    histogram: list[Decimal | None]


def macd(
    values: Sequence[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """MACD: разность быстрой и медленной EMA плюс сигнальная линия."""
    if fast >= slow:
        raise IndicatorError("MACD: быстрый период должен быть меньше медленного")
    _validate(values, slow, "MACD")

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    macd_line: list[Decimal | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema, strict=True)
    ]

    # Сигнальная линия считается только по заполненной части MACD:
    # если подать туда None, EMA молча собьётся.
    filled = [v for v in macd_line if v is not None]
    offset = len(macd_line) - len(filled)
    signal_filled = ema(filled, signal_period) if filled else []

    signal_line: list[Decimal | None] = [None] * offset + list(signal_filled)
    signal_line += [None] * (len(values) - len(signal_line))

    histogram: list[Decimal | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line, strict=True)
    ]

    return MACDResult(macd=macd_line, signal=signal_line, histogram=histogram)


def true_range(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal]
) -> list[Decimal | None]:
    """Истинный диапазон: учитывает гэпы между свечами."""
    if not (len(highs) == len(lows) == len(closes)):
        raise IndicatorError("True Range: длины рядов не совпадают")

    result: list[Decimal | None] = [None]
    for i in range(1, len(highs)):
        result.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    return result


def atr(
    highs: Sequence[Decimal],
    lows: Sequence[Decimal],
    closes: Sequence[Decimal],
    period: int = 14,
) -> list[Decimal | None]:
    """Средний истинный диапазон.

    Ключевой индикатор для методологии: им измеряется, насколько
    уверенно цена вышла за уровень при пробое, и на сколько отступить
    стоп-лосс от хвоста свечи.
    """
    _validate(closes, period, "ATR")

    tr = true_range(highs, lows, closes)
    values = [v for v in tr if v is not None]

    result: list[Decimal | None] = [None] * len(closes)
    if len(values) < period:
        return result

    previous = sum(values[:period], ZERO) / period
    result[period] = previous

    for i in range(period, len(values)):
        # Сглаживание по Уайлдеру, как в оригинальном ATR.
        previous = (previous * (period - 1) + values[i]) / period
        result[i + 1] = previous

    return result


@dataclass(frozen=True, slots=True)
class BollingerResult:
    upper: list[Decimal | None]
    middle: list[Decimal | None]
    lower: list[Decimal | None]


def bollinger_bands(
    values: Sequence[Decimal], period: int = 20, deviations: Decimal = Decimal(2)
) -> BollingerResult:
    """Полосы Боллинджера: SMA плюс-минус стандартное отклонение."""
    _validate(values, period, "Bollinger")

    middle = sma(values, period)
    upper: list[Decimal | None] = [None] * len(values)
    lower: list[Decimal | None] = [None] * len(values)

    for i in range(period - 1, len(values)):
        mean = middle[i]
        if mean is None:
            continue
        window = values[i - period + 1 : i + 1]
        variance = sum(((v - mean) ** 2 for v in window), ZERO) / period
        std = variance.sqrt()
        upper[i] = mean + deviations * std
        lower[i] = mean - deviations * std

    return BollingerResult(upper=upper, middle=middle, lower=lower)


def volume_ratio(
    volumes: Sequence[Decimal], period: int = 20
) -> list[Decimal | None]:
    """Отношение объёма к среднему за период.

    Методология требует всплеска объёма при истинном пробое: значение
    выше 1.5 означает, что объём в полтора раза выше обычного.
    """
    _validate(volumes, period, "Volume Ratio")

    averages = sma(volumes, period)
    result: list[Decimal | None] = [None] * len(volumes)

    for i, average in enumerate(averages):
        if average is None or average == ZERO:
            continue
        result[i] = volumes[i] / average

    return result


def last_value(series: Sequence[Decimal | None]) -> Decimal | None:
    """Последнее рассчитанное значение ряда."""
    for value in reversed(series):
        if value is not None:
            return value
    return None


def find_divergence(
    prices: Sequence[Decimal],
    oscillator: Sequence[Decimal | None],
    lookback: int = 30,
) -> str | None:
    """Ищет дивергенцию между ценой и осциллятором.

    Медвежья: цена делает более высокий максимум, осциллятор — более
    низкий. Бычья: зеркально. Методология использует дивергенцию как
    признак истощения тренда — но только как дополнительный фильтр,
    а не как самостоятельный сигнал на вход.
    """
    if len(prices) != len(oscillator):
        raise IndicatorError("Дивергенция: длины рядов не совпадают")

    points = [
        (i, prices[i], oscillator[i])
        for i in range(max(0, len(prices) - lookback), len(prices))
        if oscillator[i] is not None
    ]
    if len(points) < 10:
        return None

    half = len(points) // 2
    first, second = points[:half], points[half:]

    price_high_1 = max(p[1] for p in first)
    price_high_2 = max(p[1] for p in second)
    osc_high_1 = max(p[2] for p in first)  # type: ignore[type-var]
    osc_high_2 = max(p[2] for p in second)  # type: ignore[type-var]

    if price_high_2 > price_high_1 and osc_high_2 < osc_high_1:
        return "bearish"

    price_low_1 = min(p[1] for p in first)
    price_low_2 = min(p[1] for p in second)
    osc_low_1 = min(p[2] for p in first)  # type: ignore[type-var]
    osc_low_2 = min(p[2] for p in second)  # type: ignore[type-var]

    if price_low_2 < price_low_1 and osc_low_2 > osc_low_1:
        return "bullish"

    return None
