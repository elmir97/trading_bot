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
from app.trading.enums import MarketStructure, SignalDirection, TargetSource, TradeSide

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
    # EMA200 по закрытым дневным свечам — тренд для FalseBreakout
    # (app/analysis/false_breakout.py). Не higher_ema200: у H1 старший
    # таймфрейм H4. Сканер не заполняет — детектор в нём не подключён.
    d1_ema200: Decimal | None = None

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
    # Источник take_profit_1 (28.09): уровень или 2R по формуле. None — у
    # WAIT и у сигналов, собранных не детектором.
    target_source: TargetSource | None = None
    # Признаки для отчёта исходов (28.09, scripts/signal_outcomes.py) —
    # сканер пишет их в слот и снимок уведомления. Только у READY.
    # BreakoutRetest: объём пробойной свечи к среднему за 20 и её open_time.
    breakout_volume_ratio: Decimal | None = None
    breakout_at: datetime | None = None
    # EMAPullback: |цена − EMA50| / ATR — глубина касания.
    ema50_distance_atr: Decimal | None = None
    # FalseBreakout: open_time свечи прокола и её объём к среднему за 20.
    # В БД пока не пишутся — решим при подключении к сканеру.
    probe_at: datetime | None = None
    probe_volume_ratio: Decimal | None = None

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


def target_line(price_text: str, source: TargetSource | None) -> str:
    """Строка «Цель» уведомления и карточки: «Цель: уровень X» или
    «Цель: 2R по формуле — X». Без источника (старые уведомления) — как
    раньше, «Цель: X»."""
    if source is TargetSource.LEVEL:
        return f"Цель: уровень {price_text}"
    if source is TargetSource.FORMULA_2R:
        return f"Цель: 2R по формуле — {price_text}"
    return f"Цель: {price_text}"


def detector_entry(
    direction: SignalDirection | None,
    entry_low: Decimal | None,
    entry_high: Decimal | None,
) -> Decimal | None:
    """Вход детектора — закрытие подтверждающей свечи: край зоны, дальний
    от стопа (LONG — верхний, SHORT — нижний); другой край — уровень или
    EMA50. От него считаются stop_pct в слоте и RR в отчёте исходов
    (scripts/signal_outcomes.py). Не путать с signal_reference_price
    исполнения — там середина зоны."""
    if direction is SignalDirection.LONG:
        return entry_high
    if direction is SignalDirection.SHORT:
        return entry_low
    return None


def stop_percent(
    direction: SignalDirection | None,
    entry_low: Decimal | None,
    entry_high: Decimal | None,
    stop_loss: Decimal | None,
) -> Decimal | None:
    """|вход − стоп| / вход × 100, вход — detector_entry. None без цен."""
    entry = detector_entry(direction, entry_low, entry_high)
    if entry is None or stop_loss is None or entry <= 0:
        return None
    return abs(entry - stop_loss) / entry * Decimal(100)


def validate_geometry(
    direction: SignalDirection,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal,
) -> str | None:
    """Проверяет, что стоп лежит по правильную сторону всей зоны входа.

    LONG: стоп ниже нижней границы зоны; SHORT: выше верхней. Иначе вход в
    части зоны даёт стоп с неверной стороны от цены входа. Возвращает текст
    причины для note WAIT-сигнала или None, если геометрия корректна.
    Детекторы вызывают её перед сборкой Signal; в is_actionable она не
    встроена — там проверяется готовность, а не арифметика цен.
    """
    if direction is SignalDirection.LONG and stop_loss >= entry_low:
        return (
            f"Стоп {stop_loss} не ниже нижней границы зоны входа {entry_low}: "
            f"вход в нижней части зоны оказался бы за стопом. Сетап не выдан."
        )
    if direction is SignalDirection.SHORT and stop_loss <= entry_high:
        return (
            f"Стоп {stop_loss} не выше верхней границы зоны входа {entry_high}: "
            f"вход в верхней части зоны оказался бы за стопом. Сетап не выдан."
        )
    return None


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
