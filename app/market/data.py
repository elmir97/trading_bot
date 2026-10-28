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
from app.exchanges.base import (
    ExchangeClient,
    Kline,
    OpenInterest,
    PremiumIndex,
    SymbolInfo,
    Ticker,
)
from app.market.cache import (
    TTL_KLINES,
    TTL_SYMBOLS,
    TTL_TICKER,
    TTLCache,
)

logger = get_logger(__name__)


def symbols_key(exchange: str) -> str:
    """Ключ справочника инструментов биржи в TTLCache."""
    return f"symbols:{exchange}"


def cached_price_precision(cache: TTLCache, exchange: str, symbol: str) -> int | None:
    """Точность цены символа из уже загруженного справочника — без запроса к бирже (К4,
    28.10.2026): путь рендера (карточка сделки журнала, итог лимита) в сеть не ходит.
    Справочника в кэше нет (истёк, процесс только стартовал) или символа в нём нет — None:
    цена выводится как раньше, по порядку величины."""
    infos = cache.get(symbols_key(exchange))
    if not infos:
        return None
    return next((i.price_precision for i in infos if i.symbol == symbol), None)


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

    async def get_mark_prices(self, symbols: list[str]) -> dict[str, Decimal | None]:
        """Mark price нескольких символов (кэш — как у тикера). Ошибка по
        символу — None для него, не last price вместо mark."""
        result: dict[str, Decimal | None] = {}
        for symbol in symbols:
            try:
                result[symbol] = await self._cache.get_or_fetch(
                    f"mark:{self._client.name}:{symbol}",
                    TTL_TICKER,
                    lambda symbol=symbol: self._client.get_mark_price(symbol),
                )
            except Exception:
                logger.warning("Не удалось получить mark price", extra={"symbol": symbol})
                result[symbol] = None
        return result

    async def get_premium_index(self, symbol: str) -> PremiumIndex:
        """Mark price и funding (кэш — как у тикера)."""
        premium: PremiumIndex = await self._cache.get_or_fetch(
            f"premium:{self._client.name}:{symbol}",
            TTL_TICKER,
            lambda: self._client.get_premium_index(symbol),
        )
        return premium

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        """Открытый интерес в USDT (кэш — как у тикера)."""
        oi: OpenInterest = await self._cache.get_or_fetch(
            f"oi:{self._client.name}:{symbol}",
            TTL_TICKER,
            lambda: self._client.get_open_interest(symbol),
        )
        return oi

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

    async def get_symbols(self, *, max_attempts: int | None = None) -> list[SymbolInfo]:
        """max_attempts — см. ExchangeClient.get_ticker. Не входит в ключ
        кэша: число повторов не меняет полученное значение, только то, как
        быстро его достали, а промах по кэшу и так бьёт по сети не чаще,
        чем раз в TTL_SYMBOLS."""
        return await self._cache.get_or_fetch(
            symbols_key(self._client.name),
            TTL_SYMBOLS,
            lambda: self._client.get_symbols(max_attempts=max_attempts),
        )

    async def get_symbol_info(
        self, symbol: str, *, max_attempts: int | None = None
    ) -> SymbolInfo | None:
        for info in await self.get_symbols(max_attempts=max_attempts):
            if info.symbol == symbol:
                return info
        return None

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
