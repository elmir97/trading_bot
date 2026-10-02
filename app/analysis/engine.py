"""Движок анализа рынка.

Снимок рынка по монете для экрана «Анализ рынка» (свечи H1/H4/D1, funding,
open interest — market_snapshot) и контекст графика по готовым свечам
(context_from_candles). Потребители за данными сами не ходят — иначе один и
тот же индикатор считался бы по нескольку раз, а значения могли бы разойтись.

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
from app.analysis.market_overview import KLINES_LIMIT, OVERVIEW_TIMEFRAMES, MarketSnapshot
from app.analysis.structure import detect_structure, find_levels
from app.core.logging import get_logger
from app.exchanges.base import ExchangeError, Kline, OpenInterest, PremiumIndex, SymbolInfo
from app.market.data import MarketDataService
from app.trading.enums import MarketStructure

logger = get_logger(__name__)


def context_from_candles(
    symbol: str,
    timeframe: str,
    candles: list[Kline],
    *,
    higher_timeframe: str | None = None,
    higher_structure: MarketStructure | None = None,
    higher_ema200: Decimal | None = None,
) -> MarketContext:
    """Индикаторы, структура и уровни по готовым свечам — контекст графика
    «Анализа рынка» (свечи — из снимка AnalysisEngine.market_snapshot)."""
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

    async def market_snapshot(self, symbol: str) -> MarketSnapshot:
        """Данные экрана «Анализ рынка»: закрытые свечи H1/H4/D1, funding и
        open interest — пять публичных запросов. Свечи обязательны (сбой —
        исключение наверх); funding и OI — нет: без них экран остаётся
        полезным, строки пишут «н/д», сбой — в лог."""
        candles = {
            tf: await self._market.get_klines(symbol, tf, limit=KLINES_LIMIT[tf])
            for tf in OVERVIEW_TIMEFRAMES
        }
        premium: PremiumIndex | None = None
        open_interest: OpenInterest | None = None
        try:
            premium = await self._market.get_premium_index(symbol)
        except ExchangeError:
            logger.warning("premiumIndex недоступен", extra={"symbol": symbol})
        try:
            open_interest = await self._market.get_open_interest(symbol)
        except ExchangeError:
            logger.warning("openInterest недоступен", extra={"symbol": symbol})
        return MarketSnapshot(symbol, candles, premium, open_interest)
