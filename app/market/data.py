"""Сервис рыночных данных.

Единственная точка, через которую остальное приложение получает цены и
свечи. Знает про кэш и про то, какие данные можно переиспользовать, а
какие обязаны быть свежими.

Отдельно решается вопрос незакрытой свечи. Биржа всегда возвращает
последнюю, ещё формирующуюся свечу, и её показатели меняются каждую
секунду. Методология требует торговать по закрытию, поэтому анализ
получает данные без неё — иначе сигнал появлялся бы и исчезал по
несколько раз внутри одной свечи.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.exchanges.base import ExchangeClient, Kline, SymbolInfo, Ticker
from app.market.cache import (
    TTL_FUNDING,
    TTL_KLINES,
    TTL_SYMBOLS,
    TTL_TICKER,
    TTLCache,
)

logger = get_logger(__name__)


class MarketDataService:
    def __init__(self, client: ExchangeClient, cache: TTLCache | None = None) -> None:
        self._client = client
        self._cache = cache or TTLCache()

    @property
    def cache(self) -> TTLCache:
        return self._cache

    async def get_price(self, symbol: str) -> Decimal:
        ticker = await self.get_ticker(symbol)
        return ticker.last_price

    async def get_ticker(self, symbol: str) -> Ticker:
        return await self._cache.get_or_fetch(
            f"ticker:{self._client.name}:{symbol}",
            TTL_TICKER,
            lambda: self._client.get_ticker(symbol),
        )

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        *,
        include_unclosed: bool = False,
    ) -> list[Kline]:
        """Свечи для анализа.

        По умолчанию последняя незакрытая свеча отбрасывается: решения
        принимаются по закрытым данным, а незакрытая свеча меняется на
        глазах и порождала бы мерцающие сигналы.
        """
        key = f"klines:{self._client.name}:{symbol}:{interval}:{limit}"
        candles: list[Kline] = await self._cache.get_or_fetch(
            key, TTL_KLINES, lambda: self._client.get_klines(symbol, interval, limit)
        )

        if include_unclosed:
            return candles

        now = datetime.now(UTC)
        return [c for c in candles if c.is_closed(now)]

    async def get_symbols(self) -> list[SymbolInfo]:
        return await self._cache.get_or_fetch(
            f"symbols:{self._client.name}", TTL_SYMBOLS, self._client.get_symbols
        )

    async def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        for info in await self.get_symbols():
            if info.symbol == symbol:
                return info
        return None

    async def get_funding_rate(self, symbol: str) -> Decimal | None:
        return await self._cache.get_or_fetch(
            f"funding:{self._client.name}:{symbol}",
            TTL_FUNDING,
            lambda: self._client.get_funding_rate(symbol),
        )

    async def get_prices(self, symbols: list[str]) -> dict[str, Decimal | None]:
        """Цены нескольких инструментов.

        Ошибка по одному символу не должна лишать пользователя остальных:
        в списке позиций один недоступный инструмент не повод скрыть все.
        """
        result: dict[str, Decimal | None] = {}
        for symbol in symbols:
            try:
                result[symbol] = await self.get_price(symbol)
            except Exception:
                logger.warning("Не удалось получить цену", extra={"symbol": symbol})
                result[symbol] = None
        return result
