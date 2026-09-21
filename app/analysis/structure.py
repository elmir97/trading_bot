"""Структура рынка и ключевые уровни.

Определяет то, что в методологии описано словами: тренд через
последовательность максимумов и минимумов, сильные исторические уровни
поддержки и сопротивления.

Главная сложность — перевести «сильный уровень» в число. Здесь уровень
считается тем сильнее, чем больше раз цена его касалась, чем свежее
касания и чем выше был объём. Порог значимости задан в ATR, а не в
процентах: на BTC и на DOGE «близко» означает совершенно разные числа,
и привязка к волатильности инструмента убирает эту разницу.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.base import Kline
from app.trading.enums import MarketStructure

ZERO = Decimal(0)

# Свинг-точка: экстремум, вокруг которого по указанному числу свечей
# с каждой стороны нет более сильного экстремума.
SWING_STRENGTH = 2

# Насколько близко должны быть касания, чтобы считаться одним уровнем.
LEVEL_CLUSTER_ATR = Decimal("0.5")

# Минимальное число касаний, чтобы уровень считался подтверждённым.
MIN_TOUCHES = 2


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    price: Decimal
    is_high: bool


@dataclass(frozen=True, slots=True)
class Level:
    """Уровень поддержки или сопротивления."""

    price: Decimal
    touches: int
    last_touch_index: int
    is_resistance: bool
    strength: Decimal   # 0..1

    def distance_to(self, price: Decimal) -> Decimal:
        return abs(price - self.price)

    def is_near(self, price: Decimal, tolerance: Decimal) -> bool:
        return self.distance_to(price) <= tolerance


@dataclass(frozen=True, slots=True)
class StructureResult:
    structure: MarketStructure
    swings: list[SwingPoint]
    last_high: Decimal | None
    last_low: Decimal | None
    description: str


def find_swings(candles: list[Kline], strength: int = SWING_STRENGTH) -> list[SwingPoint]:
    """Находит локальные экстремумы (фракталы).

    Свеча считается свинг-максимумом, если по strength свечей слева и
    справа нет более высокого максимума. Крайние свечи пропускаются:
    для них справа ещё нет подтверждения, и объявлять их экстремумом
    значит принимать решение по неполным данным.
    """
    swings: list[SwingPoint] = []
    if len(candles) < strength * 2 + 1:
        return swings

    for i in range(strength, len(candles) - strength):
        window = candles[i - strength : i + strength + 1]
        candle = candles[i]

        if candle.high == max(c.high for c in window):
            swings.append(SwingPoint(i, candle.high, is_high=True))
        elif candle.low == min(c.low for c in window):
            swings.append(SwingPoint(i, candle.low, is_high=False))

    return swings


def detect_structure(
    candles: list[Kline], strength: int = SWING_STRENGTH
) -> StructureResult:
    """Определяет тренд по последовательности экстремумов.

    Восходящий — Higher High и Higher Low. Нисходящий — Lower High и
    Lower Low. Всё остальное считается диапазоном: методология не
    торгует против структуры, и честнее сказать «диапазон», чем
    натянуть тренд на неоднозначную картину.
    """
    swings = find_swings(candles, strength)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]

    if len(highs) < 2 or len(lows) < 2:
        return StructureResult(
            structure=MarketStructure.UNDEFINED,
            swings=swings,
            last_high=highs[-1].price if highs else None,
            last_low=lows[-1].price if lows else None,
            description="Недостаточно экстремумов для определения структуры",
        )

    higher_high = highs[-1].price > highs[-2].price
    higher_low = lows[-1].price > lows[-2].price
    lower_high = highs[-1].price < highs[-2].price
    lower_low = lows[-1].price < lows[-2].price

    if higher_high and higher_low:
        structure = MarketStructure.UPTREND
        description = "Восходящая структура: максимумы и минимумы растут"
    elif lower_high and lower_low:
        structure = MarketStructure.DOWNTREND
        description = "Нисходящая структура: максимумы и минимумы снижаются"
    else:
        structure = MarketStructure.RANGE
        description = "Диапазон: последовательность экстремумов не выражена"

    return StructureResult(
        structure=structure,
        swings=swings,
        last_high=highs[-1].price,
        last_low=lows[-1].price,
        description=description,
    )


def find_levels(
    candles: list[Kline],
    atr_value: Decimal,
    strength: int = SWING_STRENGTH,
    min_touches: int = MIN_TOUCHES,
) -> list[Level]:
    """Находит уровни, группируя близкие экстремумы.

    Допуск задаётся в долях ATR: «близко» для BTC при цене 100 000 и
    для DOGE при цене 0.4 — совершенно разные абсолютные величины, и
    единый процент тут не работает.
    """
    if atr_value <= ZERO:
        return []

    swings = find_swings(candles, strength)
    if not swings:
        return []

    tolerance = atr_value * LEVEL_CLUSTER_ATR
    clusters: list[list[SwingPoint]] = []

    for swing in sorted(swings, key=lambda s: s.price):
        if clusters and abs(swing.price - clusters[-1][-1].price) <= tolerance:
            clusters[-1].append(swing)
        else:
            clusters.append([swing])

    total = len(candles)
    levels: list[Level] = []

    for cluster in clusters:
        if len(cluster) < min_touches:
            continue

        prices = [s.price for s in cluster]
        level_price = sum(prices, ZERO) / len(prices)
        last_index = max(s.index for s in cluster)

        # Сила уровня: количество касаний и их свежесть. Уровень,
        # который трогали месяц назад, слабее того, что работал вчера.
        touch_score = min(Decimal(len(cluster)) / Decimal(4), Decimal(1))
        recency_score = Decimal(last_index) / Decimal(total) if total else ZERO
        strength_score = (touch_score * Decimal("0.7")) + (
            recency_score * Decimal("0.3")
        )

        levels.append(
            Level(
                price=level_price,
                touches=len(cluster),
                last_touch_index=last_index,
                # Сопротивление, если большинство касаний — максимумы.
                is_resistance=sum(1 for s in cluster if s.is_high) > len(cluster) / 2,
                strength=min(strength_score, Decimal(1)),
            )
        )

    return sorted(levels, key=lambda level: level.price)


def level_role(level: Level, price: Decimal) -> str:
    """Текущая роль уровня для вывода: "resistance" выше цены, иначе "support".

    is_resistance описывает, чем уровень был при обнаружении (по касаниям), и
    после пробоя расходится с реальностью. Подписи на выводе берут роль
    отсюда; детекторы продолжают читать is_resistance. Равенство цены и уровня
    относится к поддержке — как в charting._nearby_levels.
    """
    return "resistance" if level.price > price else "support"


def nearest_level(
    levels: list[Level], price: Decimal, *, above: bool | None = None
) -> Level | None:
    """Ближайший уровень к цене.

    above=True — только выше цены (цель для лонга или сопротивление),
    above=False — только ниже (стоп или поддержка).
    """
    candidates = levels
    if above is True:
        candidates = [level for level in levels if level.price > price]
    elif above is False:
        candidates = [level for level in levels if level.price < price]

    if not candidates:
        return None
    return min(candidates, key=lambda level: level.distance_to(price))


def level_broken(
    candle: Kline, level: Level, atr_value: Decimal, min_close_atr: Decimal
) -> bool:
    """Проверяет истинность пробоя уровня закрытием свечи.

    Одного пересечения мало: методология требует уверенного закрытия за
    уровнем. Запас в долях ATR отсекает случаи, когда цена заглянула за
    уровень на пару тиков — именно так выглядит ложный пробой.
    """
    if atr_value <= ZERO:
        return False

    buffer = atr_value * min_close_atr
    if level.is_resistance:
        return candle.close > level.price + buffer
    return candle.close < level.price - buffer
