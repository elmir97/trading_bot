"""Тесты кэша и сервиса рыночных данных.

Кэш опасен тем, что молча отдаёт устаревшее. Поэтому проверяется не
только попадание, но и истечение срока, и то, что незакрытая свеча не
доходит до анализа.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.exchanges.base import Kline, SymbolInfo, Ticker
from app.market.cache import TTLCache
from app.market.data import MarketDataService

D = Decimal
NOW = datetime.now(UTC)


class FakeClient:
    """Считает обращения, чтобы было видно работу кэша."""

    name = "bingx"

    def __init__(self, candles: list[Kline] | None = None) -> None:
        self.ticker_calls = 0
        self.kline_calls = 0
        self._candles = candles or []

    async def get_ticker(self, symbol: str) -> Ticker:
        self.ticker_calls += 1
        return Ticker(
            symbol=symbol,
            last_price=D("100000") + self.ticker_calls,
            volume_24h=D("1"),
            price_change_percent=D("0"),
            timestamp=NOW,
        )

    async def get_klines(self, symbol, interval, limit=500, end_time=None):  # type: ignore[no-untyped-def]
        self.kline_calls += 1
        return list(self._candles)

    async def get_symbols(self) -> list[SymbolInfo]:
        return [SymbolInfo("BTC-USDT", 2, 4, D("0.0001"), 125)]

    async def get_funding_rate(self, symbol: str) -> Decimal | None:
        return D("0.0001")

    async def close(self) -> None: ...


def candle(hours_ago: float, close: str = "100") -> Kline:
    open_time = NOW - timedelta(hours=hours_ago)
    return Kline(
        open_time=open_time,
        open=D("100"), high=D("101"), low=D("99"), close=D(close),
        volume=D("10"),
        close_time=open_time + timedelta(hours=1),
    )


class TestTTLCache:
    async def test_second_call_uses_cache(self) -> None:
        cache = TTLCache()
        calls = {"n": 0}

        async def factory() -> str:
            calls["n"] += 1
            return "value"

        assert await cache.get_or_fetch("k", 10, factory) == "value"
        assert await cache.get_or_fetch("k", 10, factory) == "value"
        assert calls["n"] == 1
        assert cache.hits == 1

    async def test_expired_entry_is_refetched(self) -> None:
        cache = TTLCache()
        calls = {"n": 0}

        async def factory() -> int:
            calls["n"] += 1
            return calls["n"]

        await cache.get_or_fetch("k", 0.01, factory)
        await asyncio.sleep(0.05)
        assert await cache.get_or_fetch("k", 0.01, factory) == 2

    async def test_parallel_misses_fetch_once(self) -> None:
        """Одновременные промахи по ключу не должны бить по лимитам биржи."""
        cache = TTLCache()
        calls = {"n": 0}

        async def slow_factory() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return "value"

        results = await asyncio.gather(
            *[cache.get_or_fetch("same", 10, slow_factory) for _ in range(5)]
        )
        assert results == ["value"] * 5
        assert calls["n"] == 1

    async def test_invalidate_by_prefix(self) -> None:
        cache = TTLCache()
        cache.set("ticker:BTC", 1, 10)
        cache.set("ticker:ETH", 2, 10)
        cache.set("klines:BTC", 3, 10)

        assert cache.invalidate("ticker:") == 2
        assert cache.get("klines:BTC") == 3
        assert cache.get("ticker:BTC") is None


class TestMarketData:
    async def test_price_is_cached(self) -> None:
        client = FakeClient()
        market = MarketDataService(client)

        first = await market.get_price("BTC-USDT")
        second = await market.get_price("BTC-USDT")

        assert first == second
        assert client.ticker_calls == 1

    async def test_unclosed_candle_excluded_by_default(self) -> None:
        """Незакрытая свеча меняется каждую секунду и породила бы
        мерцающие сигналы — анализ её видеть не должен."""
        client = FakeClient([
            candle(3), candle(2), candle(1),
            candle(0.2),  # ещё формируется
        ])
        market = MarketDataService(client)

        closed = await market.get_klines("BTC-USDT", "1h")
        everything = await market.get_klines(
            "BTC-USDT", "1h", include_unclosed=True
        )

        assert len(closed) == 3
        assert len(everything) == 4
        assert all(c.is_closed() for c in closed)

    async def test_klines_fetched_once_for_same_params(self) -> None:
        client = FakeClient([candle(2), candle(1)])
        market = MarketDataService(client)

        await market.get_klines("BTC-USDT", "1h")
        await market.get_klines("BTC-USDT", "1h")
        assert client.kline_calls == 1

        # Другой таймфрейм — отдельный ключ кэша.
        await market.get_klines("BTC-USDT", "4h")
        assert client.kline_calls == 2

    async def test_one_broken_symbol_does_not_hide_others(self) -> None:
        class PartlyBroken(FakeClient):
            async def get_ticker(self, symbol: str) -> Ticker:
                if symbol == "BROKEN":
                    raise RuntimeError("нет такого инструмента")
                return await super().get_ticker(symbol)

        market = MarketDataService(PartlyBroken())
        prices = await market.get_prices(["BTC-USDT", "BROKEN", "ETH-USDT"])

        assert prices["BROKEN"] is None
        assert prices["BTC-USDT"] is not None
        assert prices["ETH-USDT"] is not None

    async def test_symbol_info_lookup(self) -> None:
        market = MarketDataService(FakeClient())
        info = await market.get_symbol_info("BTC-USDT")
        assert info is not None
        assert info.max_leverage == 125
        assert await market.get_symbol_info("UNKNOWN") is None
