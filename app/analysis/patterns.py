"""Свечные паттерны.

Реализованы ровно те, что описаны в методологии: пин-бар, поглощение и
полнотелая свеча для подтверждения пробоя. Каждый порог вынесен в
именованную константу — цифры вроде «тело больше 60% диапазона» взяты
не из воздуха, но и не являются законом природы, и их стоит уметь
поменять, не разыскивая по коду.

Все функции работают со свечами, а не с рядами чисел: паттерн — это
свойство отдельной свечи или пары, а не последовательности.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.base import Kline

ZERO = Decimal(0)

# Пин-бар: длинный хвост в одну сторону, тело прижато к другому краю.
PIN_BAR_MIN_WICK_TO_BODY = Decimal("2.0")   # хвост минимум вдвое длиннее тела
PIN_BAR_MAX_BODY_RATIO = Decimal("0.35")    # тело не больше трети свечи
PIN_BAR_MAX_OPPOSITE_WICK = Decimal("0.3")  # противоположный хвост короткий

# Полнотелая свеча для подтверждения пробоя: методология требует
# «уверенного закрытия», а не свечи-волчка с длинными тенями.
STRONG_BODY_RATIO = Decimal("0.6")

# Доджи: тело почти отсутствует, рынок в нерешительности.
DOJI_MAX_BODY_RATIO = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class PatternMatch:
    name: str
    bullish: bool
    strength: Decimal   # 0..1, насколько выражен паттерн
    description: str


def is_strong_body(candle: Kline) -> bool:
    """Полнотелая свеча — условие истинного пробоя по методологии."""
    return candle.body_ratio >= STRONG_BODY_RATIO


def is_doji(candle: Kline) -> bool:
    return candle.body_ratio <= DOJI_MAX_BODY_RATIO


def detect_pin_bar(candle: Kline) -> PatternMatch | None:
    """Пин-бар: длинный хвост показывает отвергнутую цену.

    Бычий — длинный нижний хвост: покупатели вернули цену снизу.
    Медвежий — длинный верхний: продавцы сбили цену сверху.
    """
    if candle.range == ZERO:
        return None
    if candle.body_ratio > PIN_BAR_MAX_BODY_RATIO:
        return None

    body = candle.body if candle.body > ZERO else candle.range / 100

    lower_ratio = candle.lower_wick / body
    upper_ratio = candle.upper_wick / body

    bullish_pin = (
        lower_ratio >= PIN_BAR_MIN_WICK_TO_BODY
        and candle.upper_wick <= candle.range * PIN_BAR_MAX_OPPOSITE_WICK
    )
    bearish_pin = (
        upper_ratio >= PIN_BAR_MIN_WICK_TO_BODY
        and candle.lower_wick <= candle.range * PIN_BAR_MAX_OPPOSITE_WICK
    )

    if bullish_pin:
        strength = min(candle.lower_wick / candle.range, Decimal(1))
        return PatternMatch(
            name="Пин-бар",
            bullish=True,
            strength=strength,
            description="Длинный нижний хвост: цена снизу отвергнута",
        )
    if bearish_pin:
        strength = min(candle.upper_wick / candle.range, Decimal(1))
        return PatternMatch(
            name="Пин-бар",
            bullish=False,
            strength=strength,
            description="Длинный верхний хвост: цена сверху отвергнута",
        )
    return None


def detect_engulfing(previous: Kline, current: Kline) -> PatternMatch | None:
    """Поглощение: тело текущей свечи перекрывает тело предыдущей.

    Сравниваются именно тела, а не полные диапазоны — так этот паттерн
    определяется в классическом price action, и так он означает смену
    контроля между покупателями и продавцами.
    """
    if previous.body == ZERO or current.body == ZERO:
        return None

    prev_top = max(previous.open, previous.close)
    prev_bottom = min(previous.open, previous.close)
    curr_top = max(current.open, current.close)
    curr_bottom = min(current.open, current.close)

    if not (curr_top >= prev_top and curr_bottom <= prev_bottom):
        return None
    if previous.is_bullish == current.is_bullish:
        return None  # поглощение меняет направление, иначе это продолжение

    strength = min(current.body / previous.body / Decimal(3), Decimal(1))

    return PatternMatch(
        name="Поглощение",
        bullish=current.is_bullish,
        strength=strength,
        description=(
            "Бычье поглощение: покупатели перехватили контроль"
            if current.is_bullish
            else "Медвежье поглощение: продавцы перехватили контроль"
        ),
    )


def detect_confirmation(
    candles: list[Kline], bullish: bool
) -> PatternMatch | None:
    """Ищет подтверждающий паттерн на последней свече.

    Методология допускает вход только после подтверждения: пин-бара
    или поглощения в нужную сторону. Отсутствие паттерна — валидный
    результат, означающий «ждём».
    """
    if len(candles) < 2:
        return None

    current = candles[-1]

    pin = detect_pin_bar(current)
    if pin is not None and pin.bullish == bullish:
        return pin

    engulfing = detect_engulfing(candles[-2], current)
    if engulfing is not None and engulfing.bullish == bullish:
        return engulfing

    return None
