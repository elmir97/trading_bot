"""Техническая картина по монете для экрана «Анализ рынка» (этап 2).

Только описание рынка — без направления сделки и без советов входа:
тренд H1/H4/D1 (цена против EMA50/200), структура (HH/HL или LH/LL),
RSI(14), ATR в процентах цены, объём последней закрытой свечи к среднему,
ближайшие уровни H4/D1, funding и open interest. Всё считает код; модуль без
I/O — данные собирает AnalysisEngine.market_snapshot(), здесь только факты
и текст, поэтому проверяется тестами на ручных свечах.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.analysis.context import VOLUME_RATIO_PERIOD
from app.analysis.indicators import atr as calc_atr
from app.analysis.indicators import ema, last_value, volume_ratio
from app.analysis.indicators import rsi as calc_rsi
from app.analysis.structure import detect_structure, find_levels
from app.bot.formatting import fmt_price
from app.exchanges.base import Kline, OpenInterest, PremiumIndex
from app.trading.enums import MarketStructure

# Таймфреймы экрана и глубина истории. D1 — 1000 свечей: на 300 значение
# EMA200 ещё примерно на треть держится за стартовую среднюю (SMA), на 1000
# влияние разогрева пренебрежимо.
OVERVIEW_TIMEFRAMES = ("1h", "4h", "1d")
KLINES_LIMIT = {"1h": 300, "4h": 300, "1d": 1000}
# Уровни — со старших таймфреймов: H1-уровни у цены слишком часты и шумны.
LEVEL_TIMEFRAMES = ("4h", "1d")
LEVELS_PER_SIDE = 2
# Соседние уровни ближе этой доли ATR(H4) — один уровень (берётся первый
# по близости к цене): иначе H4 и D1 часто дают «двойной» уровень.
LEVEL_MERGE_ATR = Decimal("0.3")
# Меньше свечей — таймфрейм не считается (и график не строится).
MIN_CANDLES = 50

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14

TF_LABEL = {"1h": "H1", "4h": "H4", "1d": "D1"}
_STRUCTURE_LABEL = {
    MarketStructure.UPTREND: "HH/HL",
    MarketStructure.DOWNTREND: "LH/LL",
    MarketStructure.RANGE: "диапазон",
    MarketStructure.UNDEFINED: "структура н/д",
}
ONE_TENTH = Decimal("0.1")
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class TimeframeFacts:
    timeframe: str
    close: Decimal
    ema50: Decimal | None
    ema200: Decimal | None
    rsi: Decimal | None
    atr: Decimal | None
    atr_percent: Decimal | None
    volume_ratio: Decimal | None
    structure: MarketStructure


@dataclass(frozen=True, slots=True)
class LevelFact:
    price: Decimal
    timeframe: str
    touches: int


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Сырьё экрана — один заход на биржу (AnalysisEngine.market_snapshot):
    закрытые свечи по OVERVIEW_TIMEFRAMES, premiumIndex и open interest.
    premium/open_interest None — ручка не ответила, экран пишет «н/д»."""

    symbol: str
    candles: dict[str, list[Kline]]
    premium: PremiumIndex | None
    open_interest: OpenInterest | None


@dataclass(frozen=True, slots=True)
class MarketOverview:
    symbol: str
    price: Decimal                      # mark price, если есть, иначе закрытие H1
    frames: tuple[TimeframeFacts, ...]  # только посчитанные, в порядке OVERVIEW_TIMEFRAMES
    resistances: tuple[LevelFact, ...]  # от ближайшего
    supports: tuple[LevelFact, ...]     # от ближайшего
    premium: PremiumIndex | None
    open_interest: OpenInterest | None


def timeframe_facts(timeframe: str, candles: list[Kline]) -> TimeframeFacts | None:
    """Факты одного таймфрейма по закрытым свечам; None — мало истории."""
    if len(candles) < MIN_CANDLES:
        return None
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    close = closes[-1]
    atr_value = last_value(calc_atr(highs, lows, closes, ATR_PERIOD))
    return TimeframeFacts(
        timeframe=timeframe,
        close=close,
        ema50=last_value(ema(closes, EMA_FAST)) if len(closes) >= EMA_FAST else None,
        ema200=last_value(ema(closes, EMA_SLOW)) if len(closes) >= EMA_SLOW else None,
        rsi=last_value(calc_rsi(closes, RSI_PERIOD)),
        atr=atr_value,
        atr_percent=atr_value / close * HUNDRED if atr_value is not None and close else None,
        volume_ratio=last_value(volume_ratio([c.volume for c in candles], VOLUME_RATIO_PERIOD)),
        structure=detect_structure(candles).structure,
    )


def nearest_levels(
    candles_by_tf: dict[str, list[Kline]], price: Decimal, merge_distance: Decimal
) -> tuple[tuple[LevelFact, ...], tuple[LevelFact, ...]]:
    """Ближайшие уровни H4/D1 по обе стороны цены: (сопротивления, поддержки).

    Роль — по положению относительно цены (как level_role: выше — сопротивление,
    на уровне цены и ниже — поддержка), а не по тому, чем уровень был при
    обнаружении: пробитое сопротивление ниже цены — поддержка.
    Уровни ближе merge_distance к уже выбранному пропускаются."""
    candidates: list[LevelFact] = []
    for timeframe in LEVEL_TIMEFRAMES:
        candles = candles_by_tf.get(timeframe) or []
        if len(candles) < MIN_CANDLES:
            continue
        atr_value = last_value(
            calc_atr([c.high for c in candles], [c.low for c in candles],
                     [c.close for c in candles], ATR_PERIOD)
        )
        if not atr_value:
            continue
        candidates += [
            LevelFact(lv.price, timeframe, lv.touches) for lv in find_levels(candles, atr_value)
        ]

    def pick(side: list[LevelFact]) -> tuple[LevelFact, ...]:
        chosen: list[LevelFact] = []
        for level in sorted(side, key=lambda lv: abs(lv.price - price)):
            if any(abs(level.price - c.price) < merge_distance for c in chosen):
                continue
            chosen.append(level)
            if len(chosen) == LEVELS_PER_SIDE:
                break
        return tuple(chosen)

    above = [lv for lv in candidates if lv.price > price]
    below = [lv for lv in candidates if lv.price <= price]
    return pick(above), pick(below)


def build_overview(
    symbol: str,
    candles_by_tf: dict[str, list[Kline]],
    premium: PremiumIndex | None,
    open_interest: OpenInterest | None,
) -> MarketOverview | None:
    """None — нет ни одного таймфрейма с достаточной историей."""
    frames = tuple(
        f for tf in OVERVIEW_TIMEFRAMES
        if (f := timeframe_facts(tf, candles_by_tf.get(tf) or [])) is not None
    )
    if not frames:
        return None
    price = premium.mark_price if premium is not None else frames[0].close
    h4 = next((f for f in frames if f.timeframe == "4h"), None)
    merge_atr = (h4.atr if h4 is not None and h4.atr else frames[0].atr) or Decimal(0)
    resistances, supports = nearest_levels(candles_by_tf, price, merge_atr * LEVEL_MERGE_ATR)
    return MarketOverview(
        symbol=symbol,
        price=price,
        frames=frames,
        resistances=resistances,
        supports=supports,
        premium=premium,
        open_interest=open_interest,
    )


# --- текст -------------------------------------------------------------------


def trend_label(facts: TimeframeFacts) -> str:
    """Цена против EMA50/EMA200 — описание, не направление сделки."""
    price = facts.close
    if facts.ema50 is None:
        return "EMA н/д"
    above50 = price > facts.ema50
    if facts.ema200 is None:
        return f"{'выше' if above50 else 'ниже'} EMA50, EMA200 н/д"
    above200 = price > facts.ema200
    if above50 and above200:
        return "↑ выше EMA50/200"
    if not above50 and not above200:
        return "↓ ниже EMA50/200"
    if above50:
        return "↗ выше EMA50, ниже EMA200"
    return "↘ ниже EMA50, выше EMA200"


def _one_decimal(value: Decimal) -> str:
    text = format(value.quantize(ONE_TENTH, rounding=ROUND_HALF_UP), "f")
    return text


def _percent(value: Decimal) -> str:
    """ATR в процентах: один знак, у совсем малых — два (0.05%, не 0.1%)."""
    step = ONE_TENTH if value >= ONE_TENTH else Decimal("0.01")
    return f"{format(value.quantize(step, rounding=ROUND_HALF_UP), 'f')}%"


def frame_line(facts: TimeframeFacts) -> str:
    parts = [trend_label(facts), _STRUCTURE_LABEL[facts.structure]]
    if facts.rsi is not None:
        parts.append(f"RSI {facts.rsi.quantize(Decimal(1), rounding=ROUND_HALF_UP)}")
    if facts.atr_percent is not None:
        parts.append(f"ATR {_percent(facts.atr_percent)}")
    if facts.volume_ratio is not None:
        parts.append(f"объём ×{_one_decimal(facts.volume_ratio)}")
    return f"<b>{TF_LABEL[facts.timeframe]}</b> " + " · ".join(parts)


def _levels_line(levels: tuple[LevelFact, ...], precision: int | None) -> str:
    if not levels:
        return "нет в пределах истории"
    return " · ".join(
        f"{fmt_price(lv.price, precision)} ({TF_LABEL[lv.timeframe]})" for lv in levels
    )


def funding_text(premium: PremiumIndex, now: datetime) -> str:
    rate = premium.last_funding_rate * HUNDRED
    sign = "+" if rate > 0 else ""
    rate_text = f"{sign}{format(rate.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP), 'f')}%"
    seconds = int((premium.next_funding_time - now).total_seconds())
    if seconds <= 0:
        return f"{rate_text} · начисление сейчас"
    hours, minutes = divmod(seconds // 60, 60)
    left = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    return f"{rate_text} · следующее через {left}"


def open_interest_text(oi: OpenInterest) -> str:
    value = oi.value_usdt
    if value >= Decimal("1e9"):
        billions = (value / Decimal("1e9")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{format(billions, 'f')} млрд USDT"
    if value >= Decimal("1e6"):
        return f"{_one_decimal(value / Decimal('1e6'))} млн USDT"
    return f"{_one_decimal(value / Decimal('1e3'))} тыс. USDT"


def render_overview(
    overview: MarketOverview, price_precision: int | None, now: datetime
) -> str:
    """Текст экрана без пометки — её добавляет хендлер, вместе с пересказом."""
    lines = [
        f"🔎 <b>{overview.symbol}</b> · цена {fmt_price(overview.price, price_precision)}",
        "",
        *(frame_line(f) for f in overview.frames),
        "",
        f"Сопротивление: {_levels_line(overview.resistances, price_precision)}",
        f"Поддержка: {_levels_line(overview.supports, price_precision)}",
        "Funding: "
        + (funding_text(overview.premium, now) if overview.premium is not None else "н/д"),
        "Open interest: "
        + (open_interest_text(overview.open_interest)
           if overview.open_interest is not None else "н/д"),
        "",
        "<i>Тренд — цена против EMA50/200; объём — последняя закрытая свеча "
        f"к среднему за {VOLUME_RATIO_PERIOD}.</i>",
    ]
    return "\n".join(lines)
