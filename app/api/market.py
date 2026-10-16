"""Рыночные данные экрана «Рынок» Mini App (§3, §5а): публичные ручки BingX
через общий кэш процесса API; с ключа пользователя — только плечо.

Сроки кэша: цена — 2 с, свечи 1m — 5 с, остальные свечи — 30 с, но не дольше
конца последней свечи (`candles_ttl`), фандинг и открытый интерес — 60 с, плечо —
30 с, список контрактов — 1 ч (как у бота).
Клиент биржи открывается только на промах кэша и закрывается в конце запроса.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from types import TracebackType
from typing import Any

from app.api.deps import ApiState
from app.exchanges.base import (
    ExchangeClient,
    Kline,
    LeverageInfo,
    OpenInterest,
    PremiumIndex,
    SymbolInfo,
    Ticker,
)
from app.market.cache import TTL_SYMBOLS

INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
CANDLES_LIMIT = 200
TTL_PRICE = 2
TTL_CANDLES = {"1m": 5}
TTL_CANDLES_DEFAULT = 30
# Граница свечи прошла, а биржа новую ещё не отдала — перечитать почти сразу.
TTL_CANDLES_LAG = TTL_PRICE
TTL_DETAILS = 60
TTL_LEVERAGE = 30


class MarketReader:
    """Один запрос API — один публичный клиент (лениво)."""

    def __init__(self, state: ApiState) -> None:
        self._state = state
        self._cache = state.market_cache
        self._client: ExchangeClient | None = None

    async def __aenter__(self) -> MarketReader:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.close()

    def _public(self) -> ExchangeClient:
        if self._client is None:
            self._client = self._state.factory.public_client()
        return self._client

    async def _get(self, key: str, ttl: float | Callable[[Any], float], fetch: Any) -> Any:
        return await self._cache.get_or_fetch(f"api:{key}", ttl, fetch)

    async def symbols(self) -> list[SymbolInfo]:
        result: list[SymbolInfo] = await self._get(
            "symbols", TTL_SYMBOLS, lambda: self._public().get_symbols()
        )
        return result

    async def symbol(self, symbol: str) -> SymbolInfo | None:
        if not symbol.endswith("-USDT"):
            return None
        return next((s for s in await self.symbols() if s.symbol == symbol), None)

    async def ticker(self, symbol: str) -> Ticker:
        result: Ticker = await self._get(
            f"ticker:{symbol}", TTL_PRICE, lambda: self._public().get_ticker(symbol)
        )
        return result

    async def candles(self, symbol: str, interval: str) -> list[Kline]:
        """С незакрытой последней свечой — график и «изменение за свечу»."""
        result: list[Kline] = await self._get(
            f"candles:{symbol}:{interval}",
            candles_ttl(interval),
            lambda: self._public().get_klines(symbol, interval, CANDLES_LIMIT),
        )
        return result

    async def premium(self, symbol: str) -> PremiumIndex:
        result: PremiumIndex = await self._get(
            f"premium:{symbol}", TTL_DETAILS, lambda: self._public().get_premium_index(symbol)
        )
        return result

    async def open_interest(self, symbol: str) -> OpenInterest:
        result: OpenInterest = await self._get(
            f"oi:{symbol}", TTL_DETAILS, lambda: self._public().get_open_interest(symbol)
        )
        return result

    async def leverage(self, session: Any, user_id: int, symbol: str) -> LeverageInfo | None:
        """Плечо счёта по символу (ключ пользователя); ключа нет — None."""
        factory = self._state.factory
        mode = self._state.settings.bingx_allowed_exchange_mode

        async def fetch() -> LeverageInfo | None:
            if await factory.get_credentials(session, user_id, mode=mode) is None:
                return None
            client = await factory.for_user(session, user_id, mode=mode)
            try:
                info: LeverageInfo = await client.get_leverage(symbol)
                return info
            finally:
                await client.close()

        result: LeverageInfo | None = await self._get(
            f"leverage:{user_id}:{mode.value}:{symbol}", TTL_LEVERAGE, fetch
        )
        return result


def wall_time() -> float:
    """Часы для срока свечей (тесты подменяют)."""
    return time.time()


def candles_ttl(
    interval: str, now: Callable[[], float] | None = None,
) -> Callable[[list[Kline]], float]:
    """Срок кэша свечей по ответу: min(TTL интервала, до закрытия последней свечи).

    Иначе до 30 с после открытия новой свечи её нет ни на графике, ни в
    «изменении за свечу» (считалось бы от open прошлой). Граница уже прошла
    (биржа новую свечу ещё не отдала) — TTL_CANDLES_LAG. Свечей нет — TTL интервала.
    """
    base = TTL_CANDLES.get(interval, TTL_CANDLES_DEFAULT)

    def ttl(rows: list[Kline]) -> float:
        if not rows:
            return base
        left = rows[-1].close_time.timestamp() - (now or wall_time)()
        return min(base, left) if left > 0 else TTL_CANDLES_LAG

    return ttl


def change_from(last: Decimal, base: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """(изменение, изменение %) цены от базы; базы нет или 0 — (None, None)."""
    if base is None or base == 0:
        return None, None
    delta = last - base
    return delta, delta / base * 100


__all__ = ["CANDLES_LIMIT", "INTERVALS", "MarketReader", "candles_ttl", "change_from"]
