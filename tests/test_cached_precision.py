"""К4 (28.10.2026): точность цены для карточки сделки журнала и итога лимита — из справочника
инструментов в кэше процесса, без запроса к бирже. Справочника нет — None, цены как раньше
(по порядку величины, без хвостовых нулей). Без сети и БД."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from app.bot.handlers import trades as trades_handlers
from app.bot.keyboards.trade import trade_card
from app.database.models.trade import Trade
from app.exchanges.base import SymbolInfo
from app.market.cache import TTLCache, market_cache
from app.market.data import MarketDataService, cached_price_precision, symbols_key
from app.trading.enums import TradeSide, TradeStatus

D = Decimal
XRP = SymbolInfo("XRP-USDT", 4, 0, D(1), D(5))


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache()


@pytest.fixture
def shared_symbols() -> Iterator[None]:
    market_cache.set(symbols_key("bingx"), [XRP], 60)
    yield
    market_cache.invalidate(symbols_key("bingx"))


def _trade() -> Trade:
    return Trade(
        id=7, exchange="bingx", symbol="XRP-USDT", side=TradeSide.LONG,
        status=TradeStatus.OPEN, entry_price=D("1.5"), quantity=D(30), leverage=1,
        stop_loss=D("1.45"), take_profit=D("1.6"), fees=D(0),
    )


def test_cached_precision_hit_miss_unknown(cache: TTLCache) -> None:
    assert cached_price_precision(cache, "bingx", "XRP-USDT") is None    # кэш пуст
    cache.set(symbols_key("bingx"), [XRP], 60)
    assert cached_price_precision(cache, "bingx", "XRP-USDT") == 4
    assert cached_price_precision(cache, "bingx", "SOL-USDT") is None    # символа нет
    assert cached_price_precision(cache, "other", "XRP-USDT") is None    # другая биржа


def test_cached_precision_never_fetches(cache: TTLCache) -> None:
    """Только чтение кэша: промах не превращается в запрос (get_or_fetch не зовётся)."""
    cached_price_precision(cache, "bingx", "XRP-USDT")
    assert cache.misses == 0 and cache.hits == 0 and cache.size == 0


def test_trade_card_with_precision_keeps_zeros() -> None:
    card = trade_card(_trade(), 4)
    for line in ("Вход: 1.5000", "Стоп: 1.4500", "Цель: 1.6000"):
        assert line in card


def test_trade_card_without_precision_as_before() -> None:
    card = trade_card(_trade())
    for line in ("Вход: 1.5\n", "Стоп: 1.45\n", "Цель: 1.6\n"):
        assert line in card


def test_handler_precision_from_shared_cache(shared_symbols: None) -> None:
    assert trades_handlers._precision(_trade()) == 4


def test_handler_precision_without_cache_is_none() -> None:
    market_cache.invalidate(symbols_key("bingx"))
    assert trades_handlers._precision(_trade()) is None


class _Client:
    name = "k4-fill"

    async def get_symbols(self, *, max_attempts: int | None = None) -> list[SymbolInfo]:
        return [XRP]


async def test_symbols_loaded_by_market_service_are_seen(cache: TTLCache) -> None:
    """Справочник, загруженный обычным путём (MarketDataService.get_symbols), читается тем же
    ключом — иначе в проде кэш никогда бы не находился."""
    await MarketDataService(_Client(), cache).get_symbols()  # type: ignore[arg-type]
    assert cached_price_precision(cache, "k4-fill", "XRP-USDT") == 4
