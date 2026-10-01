"""Рыночный контекст инструмента — то, что экран «Анализ рынка» и график
знают о рынке на момент анализа.

02.10.2026: вынесен из app/analysis/signals.py, когда сигналы и детекторы
удалены. Собирается один раз (app/analysis/engine.py::context_from_candles)
и читается и текстом экрана, и графиком: пересчёт индикаторов в двух местах
значил бы риск расхождения значений между ними.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.analysis.structure import Level
from app.exchanges.base import Kline
from app.trading.enums import MarketStructure

# Период среднего объёма для «объём к среднему» (бывший
# app/analysis/setups.py::VOLUME_RATIO_PERIOD).
VOLUME_RATIO_PERIOD = 20


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Всё, что известно о рынке инструмента на одном таймфрейме."""

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
        if self.ema200 is None:
            return None
        return self.price > self.ema200
