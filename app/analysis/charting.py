"""Свечной график для экрана «Анализ рынка».

Строится из тех же свечей, что и текст экрана (MarketContext) — повторный
поход к бирже не нужен. render_analysis_chart никогда не бросает исключение
наружу: рендер — не критичный путь, если график не построился, экран всё
равно уходит текстом.

02.10.2026: сигналы удалены — на графике только рынок (свечи, EMA50/200,
ближайшие уровни), без зоны входа, стопа и цели.
"""

from __future__ import annotations

import io
import threading
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")  # без этого matplotlib в потоке воркера полезет искать GUI-бэкенд

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from matplotlib.ticker import MaxNLocator

from app.analysis.context import MarketContext
from app.analysis.indicators import ema
from app.analysis.structure import Level, level_role
from app.bot.formatting import fmt_price
from app.core.logging import get_logger

logger = get_logger(__name__)

CANDLES_DISPLAYED = 100

_COLOR_LEVEL = "#607d8b"
_COLOR_EMA50 = "#e6a817"
_COLOR_EMA200 = "#7b61ff"

# Ближайших уровней с каждой стороны цены на графике.
NEARBY_LEVELS_PER_SIDE = 2

# Окно видимых свечей + запас в ATR — единое определение «что попадает на
# график» для уровней и для линий EMA. Всё, что вне окна, растягивает ось Y
# (ETH 4H: поддержка на 28% ниже цены и EMA200 сжимали свечи в верхние 40-60%),
# а «чего ждём» находится рядом с ценой.
VISIBLE_WINDOW_MARGIN_ATR = Decimal(1)

# Подпись оси времени: без года и без запятой. Формат mplfinance по умолчанию
# '%b %d, %H:%M' слипался на узких экранах.
XAXIS_DATETIME_FORMAT = "%d.%m %H:%M"

# Потолок числа делений оси времени — чтобы подписи не налезали друг на друга.
XAXIS_MAX_TICKS = 5

# pyplot держит глобальное состояние (реестр фигур) и не потокобезопасен, а
# рендер идёт из asyncio.to_thread — два запроса экрана подряд в одном
# процессе. Без блокировки два одновременных рендера могут перемешать фигуры.
_RENDER_LOCK = threading.Lock()


def render_analysis_chart(
    context: MarketContext, price_precision: int | None = None
) -> bytes | None:
    """График для экрана «Анализ рынка»: свечи, EMA50/200 и ближайшие уровни.

    price_precision — SymbolInfo.price_precision символа для подписей цен;
    None — подписи по порядку величины цены (см. fmt_price)."""
    try:
        with _RENDER_LOCK:
            return _render(context, price_precision=price_precision)
    except Exception:
        logger.exception(
            "Не удалось построить график анализа",
            extra={"symbol": context.symbol, "timeframe": context.timeframe},
        )
        return None


def _visible_bounds(context: MarketContext) -> tuple[Decimal, Decimal] | None:
    """Окно видимых свечей: [min(low), max(high)] последних CANDLES_DISPLAYED
    свечей, расширенное на VISIBLE_WINDOW_MARGIN_ATR × ATR с каждой стороны.
    None, если свечей нет."""
    window = context.candles[-CANDLES_DISPLAYED:]
    if not window:
        return None
    margin = (context.atr or Decimal(0)) * VISIBLE_WINDOW_MARGIN_ATR
    return min(c.low for c in window) - margin, max(c.high for c in window) + margin


def _clip_to_bounds(
    values: list[Decimal | None], bounds: tuple[Decimal, Decimal] | None
) -> list[Decimal | None]:
    """Значения вне окна заменяются на None: линия остаётся там, где есть
    свечи, и не растягивает ось Y. Ничего не выключается целиком: если часть
    линии в окне, она рисуется."""
    if bounds is None:
        return values
    low, high = bounds
    return [v if v is not None and low <= v <= high else None for v in values]


def _nearby_levels(context: MarketContext) -> list[Level]:
    """Ближайшие к цене уровни в окне видимых свечей: по N сверху и снизу."""
    bounds = _visible_bounds(context)
    if bounds is None:
        return []
    low, high = bounds
    visible = [lv for lv in context.levels if low <= lv.price <= high]

    above = sorted((lv for lv in visible if lv.price > context.price), key=lambda lv: lv.price)
    below = sorted(
        (lv for lv in visible if lv.price <= context.price),
        key=lambda lv: lv.price,
        reverse=True,
    )
    return above[:NEARBY_LEVELS_PER_SIDE] + below[:NEARBY_LEVELS_PER_SIDE]


def _render(context: MarketContext, *, price_precision: int | None = None) -> bytes:
    candles = context.candles[-CANDLES_DISPLAYED:]
    closes = [c.close for c in context.candles]
    # EMA считается по всей истории (иначе разогрев обрежет линию у левого
    # края) и только потом обрезается под то же окно, что и свечи.
    # Затем линии обрезаются по окну видимых свечей (см. _clip_to_bounds).
    bounds = _visible_bounds(context)
    ema50 = _clip_to_bounds(ema(closes, 50)[-CANDLES_DISPLAYED:], bounds)
    ema200 = _clip_to_bounds(ema(closes, 200)[-CANDLES_DISPLAYED:], bounds)

    df = pd.DataFrame(
        {
            "Open": [float(c.open) for c in candles],
            "High": [float(c.high) for c in candles],
            "Low": [float(c.low) for c in candles],
            "Close": [float(c.close) for c in candles],
            "Volume": [float(c.volume) for c in candles],
        },
        index=pd.DatetimeIndex([c.open_time for c in candles]),
    )

    addplots = []
    if any(v is not None for v in ema50):
        addplots.append(mpf.make_addplot(_as_floats(ema50), color=_COLOR_EMA50, width=1.1))
    if any(v is not None for v in ema200):
        addplots.append(mpf.make_addplot(_as_floats(ema200), color=_COLOR_EMA200, width=1.1))

    # Без эмодзи в заголовке: у шрифта matplotlib (DejaVu Sans) нет глифов
    # для них, вместо иконки на графике был бы битый квадрат.
    fig, axes = mpf.plot(
        df,
        type="candle",
        style="charles",
        addplot=addplots or None,
        volume=False,
        returnfig=True,
        figsize=(9, 5.5),
        datetime_format=XAXIS_DATETIME_FORMAT,
        title=f"{context.symbol} · {context.timeframe.upper()}",
    )
    ax = axes[0]
    # Позиции по оси X у mplfinance — целые индексы свечей, подпись строит его
    # же форматтер; лимитируем только число делений.
    ax.xaxis.set_major_locator(MaxNLocator(nbins=XAXIS_MAX_TICKS, integer=True))

    # Все линии подписаны одной легендой, а не текстом у каждой линии:
    # соседние уровни по цене иначе наложились бы подписями.
    for lv in _nearby_levels(context):
        kind = "Сопротивление" if level_role(lv, context.price) == "resistance" else "Поддержка"
        ax.axhline(
            y=float(lv.price), color=_COLOR_LEVEL, linestyle=":", linewidth=1,
            alpha=0.7, label=f"{kind} {fmt_price(lv.price, price_precision)}",
        )

    if ax.get_legend_handles_labels()[1]:
        # Под графиком, а не поверх: в левом верхнем углу легенда с
        # уровнями закрывала свечи. bbox_inches="tight" ниже расширяет
        # картинку, чтобы легенда вошла.
        ax.legend(
            loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2,
            fontsize=8, framealpha=0.85,
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _as_floats(values: list[Decimal | None]) -> list[float]:
    return [float(v) if v is not None else float("nan") for v in values]
