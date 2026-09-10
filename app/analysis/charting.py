"""Свечной график сетапа для уведомлений сканера.

Строится из тех же свечей, что уже использовал детектор (MarketContext) —
повторный поход к бирже не нужен. render_setup_chart никогда не бросает
исключение наружу: рендер — не критичный путь, если график не построился,
уведомление всё равно должно уйти текстом (см. app/workers/scanner.py).
"""

from __future__ import annotations

import io
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")  # без этого matplotlib в потоке воркера полезет искать GUI-бэкенд

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from app.analysis.indicators import ema
from app.analysis.signals import MarketContext, Signal
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


def render_setup_chart(
    context: MarketContext, signal: Signal, level: SignalLevel
) -> bytes | None:
    try:
        return _render(context, signal, level)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Не удалось построить график сетапа",
            extra={"symbol": context.symbol, "timeframe": context.timeframe},
        )
        return None


def _render(context: MarketContext, signal: Signal, level: SignalLevel) -> bytes:
    candles = context.candles[-CANDLES_DISPLAYED:]
    closes = [c.close for c in context.candles]
    # EMA считается по всей истории (иначе разогрев обрежет линию у левого
    # края) и только потом обрезается под то же окно, что и свечи.
    ema50 = ema(closes, 50)[-CANDLES_DISPLAYED:]
    ema200 = ema(closes, 200)[-CANDLES_DISPLAYED:]

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
    label = "READY" if level is SignalLevel.READY else "FORMING"
    fig, axes = mpf.plot(
        df,
        type="candle",
        style="charles",
        addplot=addplots or None,
        volume=False,
        returnfig=True,
        figsize=(9, 5.5),
        title=f"{context.symbol} · {context.timeframe.upper()} · {label}",
    )
    ax = axes[0]

    # Уровень (ретест) и середина зоны входа часто оказываются рядом по
    # цене — это и есть суть ретеста. Подписи по цене на графике в этом
    # случае наложились бы друг на друга, поэтому все линии подписаны
    # через одну легенду, а не текстом у каждой линии.
    if signal.level_price is not None:
        _hline(ax, signal.level_price, _COLOR_LEVEL, f"Уровень {signal.level_price:.4f}")

    if signal.entry_zone_low is not None and signal.entry_zone_high is not None:
        low, high = float(signal.entry_zone_low), float(signal.entry_zone_high)
        ax.axhspan(
            low, high, color=_COLOR_ENTRY, alpha=0.15,
            label=f"Вход {low:.4f}–{high:.4f}",
        )

    if signal.stop_loss is not None:
        _hline(ax, signal.stop_loss, _COLOR_STOP, f"Стоп {signal.stop_loss:.4f}")

    if signal.take_profit_1 is not None:
        _hline(ax, signal.take_profit_1, _COLOR_TARGET, f"Цель {signal.take_profit_1:.4f}")

    if ax.get_legend_handles_labels()[1]:
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
