"""premiumIndex и openInterest для «Анализа рынка» (этап 2): строгий разбор
живых форм BingX (tests/fixtures/bingx_public_20261002.json и 20260929)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import ExchangeResponseError
from tests.bingx_fixtures import MARKET_QUOTES, PUBLIC, live_items
from tests.test_bingx_client import make_client, ok

D = Decimal


def _serve(payload: object, path_part: str):  # type: ignore[no-untyped-def]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert path_part in str(request.url)
        seen.append(str(request.url))
        return ok(payload)

    return handler, seen


class TestPremiumIndex:
    async def test_live_xrp_form(self) -> None:
        item = live_items("premiumIndex XRP (live)", MARKET_QUOTES)[0]
        handler, seen = _serve(item, "/openApi/swap/v2/quote/premiumIndex")
        result = await make_client(handler).get_premium_index("XRP-USDT")
        assert result.symbol == "XRP-USDT"
        assert result.mark_price == D("1.5245") and result.index_price == D("1.5247")
        assert result.last_funding_rate == D("0.00010000")
        assert result.next_funding_time == datetime.fromtimestamp(1790928000, tz=UTC)
        assert result.funding_interval_hours == 8
        assert "symbol=XRP-USDT" in seen[0]

    @pytest.mark.parametrize("host", ["live", "demo"])
    async def test_live_btc_form_from_29_09(self, host: str) -> None:
        item = live_items(f"premiumIndex BTC ({host})", PUBLIC)[0]
        handler, _ = _serve(item, "premiumIndex")
        result = await make_client(handler).get_premium_index("BTC-USDT")
        assert result.mark_price == D(item["markPrice"])
        assert result.last_funding_rate == D(item["lastFundingRate"])

    @pytest.mark.parametrize(
        "missing", ["markPrice", "indexPrice", "lastFundingRate", "nextFundingTime"]
    )
    async def test_missing_required_field_is_error_not_zero(self, missing: str) -> None:
        item = dict(live_items("premiumIndex XRP (live)", MARKET_QUOTES)[0])
        item[missing] = ""
        handler, _ = _serve(item, "premiumIndex")
        with pytest.raises(ExchangeResponseError, match=missing):
            await make_client(handler).get_premium_index("XRP-USDT")

    async def test_missing_interval_is_none(self) -> None:
        item = dict(live_items("premiumIndex XRP (live)", MARKET_QUOTES)[0])
        del item["fundingIntervalHours"]
        handler, _ = _serve(item, "premiumIndex")
        result = await make_client(handler).get_premium_index("XRP-USDT")
        assert result.funding_interval_hours is None

    async def test_other_symbol_is_error(self) -> None:
        item = live_items("premiumIndex XRP (live)", MARKET_QUOTES)[0]
        handler, _ = _serve(item, "premiumIndex")
        with pytest.raises(ExchangeResponseError, match="нет записи BTC-USDT"):
            await make_client(handler).get_premium_index("BTC-USDT")


class TestOpenInterest:
    @pytest.mark.parametrize(
        ("label", "symbol"), [("openInterest XRP (live)", "XRP-USDT"),
                              ("openInterest DOGE (demo)", "DOGE-USDT")],
    )
    async def test_live_forms(self, label: str, symbol: str) -> None:
        item = live_items(label, MARKET_QUOTES)[0]
        handler, seen = _serve(item, "/openApi/swap/v2/quote/openInterest")
        result = await make_client(handler).get_open_interest(symbol)
        assert result.symbol == symbol
        assert result.value_usdt == D(item["openInterest"])
        assert result.time == datetime.fromtimestamp(item["time"] / 1000, tz=UTC)
        assert f"symbol={symbol}" in seen[0]

    @pytest.mark.parametrize("missing", ["openInterest", "time"])
    async def test_missing_field_is_error(self, missing: str) -> None:
        item = dict(live_items("openInterest XRP (live)", MARKET_QUOTES)[0])
        item[missing] = ""
        handler, _ = _serve(item, "openInterest")
        with pytest.raises(ExchangeResponseError, match=missing):
            await make_client(handler).get_open_interest("XRP-USDT")

    async def test_other_symbol_is_error(self) -> None:
        item = live_items("openInterest XRP (live)", MARKET_QUOTES)[0]
        handler, _ = _serve(item, "openInterest")
        with pytest.raises(ExchangeResponseError, match="нет записи"):
            await make_client(handler).get_open_interest("BTC-USDT")
