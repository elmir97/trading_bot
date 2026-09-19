"""Движок анализа рынка.

Собирает рыночный контекст (свечи, индикаторы, структура, уровни) и
прогоняет через него детекторы сетапов. Возвращает лучший найденный
сигнал либо WAIT с объяснением, чего не хватило.

Порядок намеренный: сначала данные, потом индикаторы, потом структура,
и только затем стратегия. Детекторы не ходят за данными сами — иначе
один и тот же индикатор считался бы по нескольку раз, а значения между
детекторами могли бы разойтись.
"""

from __future__ import annotations

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
from app.analysis.setups import DEFAULT_DETECTORS, SetupDetector
from app.analysis.signals import MarketContext, Signal, wait_signal
from app.analysis.structure import detect_structure, find_levels
from app.core.logging import get_logger
from app.exchanges.base import SymbolInfo
from app.market.data import MarketDataService
from app.trading.enums import SignalDirection, Timeframe

logger = get_logger(__name__)

# Свечей достаточно для EMA200 с запасом на разогрев.
CANDLES_REQUIRED = 300


class AnalysisEngine:
    def __init__(
        self,
        market: MarketDataService,
        detectors: list[SetupDetector] | None = None,
    ) -> None:
        self._market = market
        self._detectors = detectors or DEFAULT_DETECTORS

    async def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        """Точность цены/объёма символа — для форматирования на выводе.

        Форвард в MarketDataService.get_symbol_info(): список инструментов
        кэшируется на час (TTL_SYMBOLS), так что вызов на каждый рендер
        карточки не превращается в отдельный поход на биржу.
        """
        return await self._market.get_symbol_info(symbol)

    async def get_symbols(self) -> list[SymbolInfo]:
        """Полный список инструментов — для сканирования сразу по многим
        символам: один вызов и локальный dict вместо N обращений к кэшу
        (сам по себе кэш это тоже не размножит, т.к. ключ один на всю
        биржу — но так это гарантировано структурой кода, а не поведением
        TTLCache, которое не должно быть контрактом для вызывающей стороны).
        """
        return await self._market.get_symbols()

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

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        atr_value = last_value(calc_atr(highs, lows, closes, 14))
        structure = detect_structure(candles)
        levels = find_levels(candles, atr_value) if atr_value else []

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
            volume_ratio=last_value(volume_ratio(volumes, 20)),
            structure=structure.structure,
            levels=levels,
            higher_timeframe=higher_tf,
            higher_structure=higher_structure,
            higher_ema200=higher_ema200,
        )

    async def analyze(self, symbol: str, timeframe: str) -> Signal:
        """Ищет сетап по одному инструменту."""
        context = await self.build_context(symbol, timeframe)
        if context is None:
            return wait_signal(
                symbol, timeframe,
                "Недостаточно рыночных данных для анализа.",
            )

        return self.evaluate(context)

    def evaluate(self, context: MarketContext) -> Signal:
        """Прогоняет контекст через детекторы и выбирает лучший результат.

        Если сетапов несколько, берётся с наибольшей оценкой. Если нет
        ни одного — возвращается WAIT того детектора, который прошёл
        дальше остальных: пользователю полезнее увидеть «ждём ретеста»,
        чем «нет пробоя».
        """
        signals: list[Signal] = []
        for detector in self._detectors:
            try:
                signals.append(detector.detect(context))
            except Exception:
                logger.exception(
                    "Детектор упал",
                    extra={"detector": detector.name, "symbol": context.symbol},
                )

        if not signals:
            return wait_signal(
                context.symbol, context.timeframe,
                "Ни один детектор не отработал.",
            )

        actionable = [s for s in signals if s.is_actionable]
        if actionable:
            return max(actionable, key=lambda s: s.confidence)

        # Ни одного сетапа: показываем тот WAIT, где выполнено больше
        # условий — он ближе всего к готовому входу.
        return max(signals, key=lambda s: len(s.passed_conditions))

    async def scan(
        self, symbols: list[str], timeframe: str
    ) -> list[Signal]:
        """Анализирует несколько инструментов.

        Ошибка по одному символу не должна прерывать сканирование:
        недоступный инструмент не повод скрыть сетапы по остальным.
        """
        results: list[Signal] = []
        for symbol in symbols:
            try:
                results.append(await self.analyze(symbol, timeframe))
            except Exception:
                logger.exception("Ошибка анализа", extra={"symbol": symbol})
                results.append(
                    wait_signal(symbol, timeframe, "Ошибка при анализе.")
                )

        # Сначала готовые сетапы по убыванию оценки, затем ожидания.
        return sorted(
            results,
            key=lambda s: (s.direction is not SignalDirection.WAIT, s.confidence),
            reverse=True,
        )
