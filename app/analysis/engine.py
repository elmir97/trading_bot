"""Движок анализа рынка.

Собирает рыночный контекст инструмента: свечи, индикаторы, структура,
уровни, старший таймфрейм. Порядок намеренный: сначала данные, потом
индикаторы, потом структура. Потребители (экран «Анализ рынка», график)
за данными сами не ходят — иначе один и тот же индикатор считался бы по
нескольку раз, а значения между ними могли бы разойтись.

02.10.2026: детекторы сетапов и вердикт LONG/SHORT/WAIT удалены — движок
только описывает рынок, решений не принимает.
"""

from __future__ import annotations

from decimal import Decimal

from app.analysis.context import VOLUME_RATIO_PERIOD, MarketContext
from app.analysis.indicators import (
    atr as calc_atr,
)
from app.analysis.indicators import (
    ema,
    last_value,
    macd,
    volume_ratio,
)
from app.analysis.indicators import (
    rsi as calc_rsi,
)
from app.analysis.structure import detect_structure, find_levels
from app.core.logging import get_logger
from app.exchanges.base import Kline, SymbolInfo
from app.market.data import MarketDataService
from app.trading.enums import MarketStructure, Timeframe

logger = get_logger(__name__)

# Свечей достаточно для EMA200 с запасом на разогрев.
CANDLES_REQUIRED = 300


def context_from_candles(
    symbol: str,
    timeframe: str,
    candles: list[Kline],
    *,
    higher_timeframe: str | None = None,
    higher_structure: MarketStructure | None = None,
    higher_ema200: Decimal | None = None,
) -> MarketContext:
    """Индикаторы, структура и уровни по готовым свечам — ядро build_context."""
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    atr_value = last_value(calc_atr(highs, lows, closes, 14))
    structure = detect_structure(candles)
    levels = find_levels(candles, atr_value) if atr_value else []

    return MarketContext(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        price=closes[-1],
        ema20=last_value(ema(closes, 20)),
        ema50=last_value(ema(closes, 50)),
        ema200=last_value(ema(closes, 200)),
        rsi=last_value(calc_rsi(closes, 14)),
        atr=atr_value,
        macd_histogram=last_value(macd(closes).histogram),
        volume_ratio=last_value(volume_ratio(volumes, VOLUME_RATIO_PERIOD)),
        structure=structure.structure,
        levels=levels,
        higher_timeframe=higher_timeframe,
        higher_structure=higher_structure,
        higher_ema200=higher_ema200,
    )


class AnalysisEngine:
    def __init__(self, market: MarketDataService) -> None:
        self._market = market

    async def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        """Точность цены/объёма символа — для форматирования на выводе.

        Форвард в MarketDataService.get_symbol_info(): список инструментов
        кэшируется на час (TTL_SYMBOLS), так что вызов на каждый рендер
        карточки не превращается в отдельный поход на биржу.
        """
        return await self._market.get_symbol_info(symbol)

    async def build_context(
        self, symbol: str, timeframe: str, *, with_higher: bool = True
    ) -> MarketContext | None:
        """Собирает всё, что нужно для анализа одного инструмента."""
        candles = await self._market.get_klines(
            symbol, timeframe, limit=CANDLES_REQUIRED
        )
        if len(candles) < 50:
            logger.info(
                "Мало свечей для анализа",
                extra={"symbol": symbol, "count": len(candles)},
            )
            return None

        higher_tf: str | None = None
        higher_structure = None
        higher_ema200 = None

        if with_higher:
            # Старший таймфрейм даёт контекст: методология требует
            # сверяться с ним, потому что пробой на H1 часто оказывается
            # лишь тенью свечи на H4.
            try:
                higher = Timeframe(timeframe).higher
            except ValueError:
                higher = None

            if higher is not None:
                higher_tf = higher.value
                higher_candles = await self._market.get_klines(
                    symbol, higher_tf, limit=CANDLES_REQUIRED
                )
                if len(higher_candles) >= 50:
                    higher_structure = detect_structure(higher_candles).structure
                    higher_ema200 = last_value(
                        ema([c.close for c in higher_candles], 200)
                    )

        return context_from_candles(
            symbol,
            timeframe,
            candles,
            higher_timeframe=higher_tf,
            higher_structure=higher_structure,
            higher_ema200=higher_ema200,
        )
