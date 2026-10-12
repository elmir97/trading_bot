"""Поиск контракта BingX по тикеру — общий для чат-мастера и Mini App
(docs/miniapp-plan.md §9а, подэтап 0).

Только USDT-M: точное совпадение — одно; иначе по началу и по вхождению
базового актива. Список контрактов — публичный, из кэша рыночных данных.
"""

from __future__ import annotations

from app.core.config import Settings
from app.exchanges.base import SymbolInfo
from app.market.cache import TTLCache
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory


def normalize_query(query: str) -> str:
    """«xrp/usdt», «XRPUSDT», « xrp-usdt » → «XRP»."""
    q = query.strip().upper().replace("/", "-").removesuffix("-USDT").removesuffix("USDT")
    return q.strip("-")


def match_contracts(symbols: list[SymbolInfo], query: str, limit: int) -> list[SymbolInfo]:
    q = normalize_query(query)
    if not q:
        return []
    usdt = [s for s in symbols if s.symbol.endswith("-USDT")]
    exact = [s for s in usdt if s.symbol == f"{q}-USDT"]
    if exact:
        return exact
    starts = sorted((s for s in usdt if s.symbol.startswith(q)), key=lambda s: s.symbol)
    inside = sorted(
        (s for s in usdt if q in s.symbol.removesuffix("-USDT") and s not in starts),
        key=lambda s: s.symbol,
    )
    return [*starts, *inside][:limit]


async def search_contracts(
    settings: Settings, query: str, cache: TTLCache, *, limit: int
) -> list[SymbolInfo]:
    if not normalize_query(query):
        return []
    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    try:
        symbols = await MarketDataService(client, cache).get_symbols()
    finally:
        await client.close()
    return match_contracts(symbols, query, limit)


__all__ = ["match_contracts", "normalize_query", "search_contracts"]
