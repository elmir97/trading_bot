"""Свечной график сетапа для уведомлений сканера.

Строится из тех же свечей, что уже использовал детектор (MarketContext) —
повторный поход к бирже не нужен. render_setup_chart никогда не бросает
исключение наружу: рендер — не критичный путь, если график не построился,
уведомление всё равно должно уйти текстом (см. app/workers/scanner.py).
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

from app.analysis.classify import classify_signal
from app.analysis.indicators import ema
from app.analysis.signals import MarketContext, Signal
from app.analysis.structure import Level, level_role
from app.bot.formatting import fmt_price
from app.core.logging import get_logger
from app.trading.enums import SignalLevel

logger = get_logger(__name__)

CANDLES_DISPLAYED = 100

_COLOR_LEVEL = "#607d8b"
_COLOR_ENTRY = "#2196f3"
_COLOR_STOP = "#e53935"
_COLOR_TARGET = "#43a047"
_COLOR_EMA50 = "#e6a817"
_COLOR_EMA200 = "#7b61ff"

# Ближайших уровней с каждой стороны цены на графике «по запросу».
NEARBY_LEVELS_PER_SIDE = 2

# Окно видимых свечей + запас в ATR — единое определение «что попадает на
# график» для уровней и для линий EMA. Всё, что вне окна, растягивает ось Y
# (ETH 4H: поддержка на 28% ниже цены и EMA200 сжимали свечи в верхние 40-60%),
# а «чего ждём» находится рядом с ценой.
VISIBLE_WINDOW_MARGIN_ATR = Decimal(1)

# Подпись оси времени: без года и без запятой. Формат mplfinance по умолчанию
# '%b %d, %H:%M' слипался на узких экранах. Задаётся для всех графиков модуля,
# включая сканер.
XAXIS_DATETIME_FORMAT = "%d.%m %H:%M"

# Потолок числа делений оси времени — чтобы подписи не налезали друг на друга.
XAXIS_MAX_TICKS = 5

# pyplot держит глобальное состояние (реестр фигур) и не потокобезопасен, а
# рендер идёт из asyncio.to_thread и из сканера, и из хендлера бота — в одном
# процессе. Без блокировки два одновременных рендера могут перемешать фигуры.
_RENDER_LOCK = threading.Lock()


def render_setup_chart(
    context: MarketContext,
    signal: Signal,
    level: SignalLevel,
    price_precision: int | None = None,
) -> bytes | None:
    """price_precision — SymbolInfo.price_precision символа для подписей цен;
    None — подписи по порядку величины цены (см. fmt_price)."""
    label = "READY" if level is SignalLevel.READY else "FORMING"
    try:
        with _RENDER_LOCK:
            return _render(context, signal, label, price_precision=price_precision)
    except Exception:
        logger.exception(
            "Не удалось построить график сетапа",
            extra={"symbol": context.symbol, "timeframe": context.timeframe},
        )
        return None


def render_analysis_chart(
    context: MarketContext, signal: Signal, price_precision: int | None = None
) -> bytes | None:
    """График для экрана «Анализ рынка» — разовый расчёт по кнопке.

    В отличие от графика сканера рисуется всегда, и при WAIT тоже: тогда
    показывает ближайшие уровни и EMA — видно, чего ждём. В заголовке нет
    слов READY/FORMING: они принадлежат сигналам сканера, у которых есть
    signal_id и кнопка входа, а здесь их нет.
    """
    if signal.is_actionable:
        label = signal.direction.value
    elif classify_signal(signal) is SignalLevel.FORMING:
        label = "FORMING"
    else:
        label = "WAIT"
    try:
        with _RENDER_LOCK:
            return _render(
                context, signal, f"{label} · по запросу",
                nearby_levels=True, legend_below=True, price_precision=price_precision,
            )
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


def _render(
    context: MarketContext,
    signal: Signal,
    label: str,
    *,
    nearby_levels: bool = False,
    legend_below: bool = False,
    price_precision: int | None = None,
) -> bytes:
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
        title=f"{context.symbol} · {context.timeframe.upper()} · {label}",
    )
    ax = axes[0]
    # Позиции по оси X у mplfinance — целые индексы свечей, подпись строит его
    # же форматтер; лимитируем только число делений.
    ax.xaxis.set_major_locator(MaxNLocator(nbins=XAXIS_MAX_TICKS, integer=True))

    # Уровень (ретест) и середина зоны входа часто оказываются рядом по
    # цене — это и есть суть ретеста. Подписи по цене на графике в этом
    # случае наложились бы друг на друга, поэтому все линии подписаны
    # через одну легенду, а не текстом у каждой линии.
    if signal.level_price is not None:
        _hline(
            ax, signal.level_price, _COLOR_LEVEL,
            f"Уровень {fmt_price(signal.level_price, price_precision)}",
        )

    if nearby_levels:
        for lv in _nearby_levels(context):
            if signal.level_price is not None and lv.price == signal.level_price:
                continue
            kind = (
                "Сопротивление" if level_role(lv, context.price) == "resistance" else "Поддержка"
            )
            ax.axhline(
                y=float(lv.price), color=_COLOR_LEVEL, linestyle=":", linewidth=1,
                alpha=0.7, label=f"{kind} {fmt_price(lv.price, price_precision)}",
            )

    if signal.entry_zone_low is not None and signal.entry_zone_high is not None:
        low, high = float(signal.entry_zone_low), float(signal.entry_zone_high)
        ax.axhspan(
            low, high, color=_COLOR_ENTRY, alpha=0.15,
            label=(
                f"Вход {fmt_price(signal.entry_zone_low, price_precision)}–"
                f"{fmt_price(signal.entry_zone_high, price_precision)}"
            ),
        )

    if signal.stop_loss is not None:
        _hline(
            ax, signal.stop_loss, _COLOR_STOP,
            f"Стоп {fmt_price(signal.stop_loss, price_precision)}",
        )

    if signal.take_profit_1 is not None:
        _hline(
            ax, signal.take_profit_1, _COLOR_TARGET,
            f"Цель {fmt_price(signal.take_profit_1, price_precision)}",
        )

    if ax.get_legend_handles_labels()[1]:
        if legend_below:
            # Под графиком, а не поверх: в левом верхнем углу легенда с
            # уровнями закрывала свечи. bbox_inches="tight" ниже расширяет
            # картинку, чтобы легенда вошла.
            ax.legend(
                loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2,
                fontsize=8, framealpha=0.85,
            )
        else:
            ax.legend(loc="upper left", fontsize=8, framealpha=0.85)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _as_floats(values: list[Decimal | None]) -> list[float]:
    return [float(v) if v is not None else float("nan") for v in values]


def _hline(ax, price: Decimal, color: str, label: str) -> None:
    ax.axhline(y=float(price), color=color, linestyle="--", linewidth=1, alpha=0.8, label=label)
