"""Торговый сигнал и рыночный контекст.

Сигнал — структурированный объект, а не текст. Это позволяет его
сохранить, потом сверить с фактическим исходом и понять, работают ли
детекторы. Текстовое представление строится из объекта, а не наоборот.

WAIT — полноценный результат. Отсутствие сетапа означает, что условия
методологии не выполнены, и это ценная информация: количество сигналов
не является целью системы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.analysis.structure import Level
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure, SignalDirection, TradeSide

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Всё, что известно о рынке на момент анализа.

    Собирается один раз и передаётся всем детекторам: пересчитывать
    индикаторы в каждом детекторе значило бы тратить время впустую и
    рисковать расхождением значений между ними.
    """

    symbol: str
    timeframe: str
    candles: list[Kline]
    price: Decimal

    ema20: Decimal | None
    ema50: Decimal | None
    ema200: Decimal | None
    rsi: Decimal | None
    atr: Decimal | None
    macd_histogram: Decimal | None
    volume_ratio: Decimal | None

    structure: MarketStructure
    levels: list[Level]

    higher_timeframe: str | None = None
    higher_structure: MarketStructure | None = None
    higher_ema200: Decimal | None = None

    @property
    def above_ema200(self) -> bool | None:
        """Главный фильтр методологии: лонги выше EMA200, шорты ниже."""
        if self.ema200 is None:
            return None
        return self.price > self.ema200

    @property
    def trend_allows(self) -> SignalDirection:
        """Какое направление разрешает глобальный фильтр тренда."""
        above = self.above_ema200
        if above is None:
            return SignalDirection.WAIT
        return SignalDirection.LONG if above else SignalDirection.SHORT

    @property
    def has_enough_data(self) -> bool:
        # EMA200 требует минимум 200 свечей; без неё главный фильтр
        # методологии не работает, и анализ теряет смысл.
        return len(self.candles) >= 200 and self.atr is not None


@dataclass(frozen=True, slots=True)
class SignalCondition:
    """Одно проверенное условие сетапа.

    Хранятся и выполненные, и невыполненные: пользователю важно
    понимать, чего именно не хватает, а не только видеть вердикт.
    """

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    timeframe: str
    direction: SignalDirection
    setup: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None
    risk_reward: Decimal | None = None
    # Пробитый/тестируемый уровень — для графика (app/analysis/charting.py).
    # Есть только у BreakoutRetest: у EMAPullback роль уровня играет сама
    # EMA50, отдельной горизонтальной линии для неё не нужно.
    level_price: Decimal | None = None

    confidence: int = 0            # 0..10
    confirmation: str = ""
    invalidation: str = ""
    conditions: list[SignalCondition] = field(default_factory=list)
    note: str = ""

    @property
    def is_actionable(self) -> bool:
        return (
            self.direction is not SignalDirection.WAIT
            and self.stop_loss is not None
            and self.entry_zone_low is not None
        )

    @property
    def side(self) -> TradeSide | None:
        if self.direction is SignalDirection.LONG:
            return TradeSide.LONG
        if self.direction is SignalDirection.SHORT:
            return TradeSide.SHORT
        return None

    @property
    def failed_conditions(self) -> list[SignalCondition]:
        return [c for c in self.conditions if not c.passed]

    @property
    def passed_conditions(self) -> list[SignalCondition]:
        return [c for c in self.conditions if c.passed]


def wait_signal(
    symbol: str,
    timeframe: str,
    reason: str,
    conditions: list[SignalCondition] | None = None,
    level_price: Decimal | None = None,
) -> Signal:
    """Отсутствие сетапа — это ответ, а не ошибка.

    Методология прямо требует не выдумывать сигналы: «Входа сейчас
    нет» с объяснением полезнее, чем натянутый сетап.
    """
    return Signal(
        symbol=symbol,
        timeframe=timeframe,
        direction=SignalDirection.WAIT,
        setup="Нет сетапа",
        note=reason,
        conditions=conditions or [],
        level_price=level_price,
    )
