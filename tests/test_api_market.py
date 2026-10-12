"""API Mini App, подэтап 1.4: /api/symbols, /api/market/{symbol}, /candles (§3, §5а)."""

from __future__ import annotations

import os
import time
from decimal import Decimal

import pytest

from app.api import market as market_module
from app.exchanges.base import ExchangeUnavailableError
from tests.api_helpers import (  # noqa: F401 — api_fixture: фикстура «api»
    FakeClient,
    FakeFactory,
    FakePublic,
    api_fixture,
    auth,
    client_for,
)

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="Нужен PostgreSQL")

D = Decimal


async def test_symbols_search_and_cache(api) -> None:  # type: ignore[no-untyped-def]
    public = FakePublic()
    async with client_for(api.state(factory=FakeFactory(public=public))) as c:
        h = auth(api.tid)
        xr = (await c.get("/api/symbols", params={"q": "xr"}, headers=h)).json()
        exact = (await c.get("/api/symbols", params={"q": "btc"}, headers=h)).json()
        anon = await c.get("/api/symbols", params={"q": "xr"})
    assert [s["symbol"] for s in xr] == ["XRP-USDT", "XRPUP-USDT"]
    assert xr[0] == {"symbol": "XRP-USDT", "price_precision": 4, "quantity_precision": 0,
                     "min_quantity": "2", "min_notional": "2"}
    assert [s["symbol"] for s in exact] == ["BTC-USDT"]          # только USDT-M
    assert anon.status_code == 401
    assert public.calls["symbols"] == 1                           # кэш списка — 1 ч


async def test_market(api) -> None:  # type: ignore[no-untyped-def]
    public, client = FakePublic(), FakeClient()
    state = api.state(factory=FakeFactory(client, public=public))
    async with client_for(state) as c:
        r = await c.get("/api/market/XRP-USDT", params={"interval": "5m"}, headers=auth(api.tid))
        again = await c.get("/api/market/XRP-USDT", params={"interval": "5m"},
                            headers=auth(api.tid))
    assert r.status_code == 200 and again.json() == r.json()
    body = r.json()
    assert body["contract"]["symbol"] == "XRP-USDT" and body["interval"] == "5m"
    assert (body["last"], body["time"]) == ("1.4952", "2026-10-12T17:00:00Z")
    # last 1.4952 против open текущей (незакрытой) 5-минутки 1.4960
    assert body["candle_change"] == {"value": "-0.0008", "percent": "-0.05"}
    assert body["day_change"] == {"value": "0.0123", "percent": "0.83"}
    assert (body["quote_volume"], body["open_interest"]) == ("31820000.46", "21940000.10")
    assert body["funding_rate_percent"] == "0.0100"               # 0.0001 доли = 0.01 %
    assert body["next_funding_time"] == "2026-10-12T19:00:00Z"
    assert body["leverage"] == {"long": 10, "short": 5, "max_long": 75, "max_short": 50}
    assert body["leverage_error"] is None and body["details_error"] is None
    # второй запрос — из кэша: к бирже ничего, клиент не открывался
    assert public.calls == {"symbols": 1, "ticker": 1, "klines:5m": 1, "premium": 1, "oi": 1}
    assert client.leverage_calls == 1 and client.closed
    assert public.closed == 1
    cache = state.market_cache._data
    ttl = {k: round(e.expires_at - time.monotonic()) for k, e in cache.items()}
    assert ttl["api:ticker:XRP-USDT"] == market_module.TTL_PRICE == 2
    assert ttl["api:candles:XRP-USDT:5m"] == 30 and ttl["api:premium:XRP-USDT"] == 60
    assert ttl[f"api:leverage:{api.uid}:DEMO:XRP-USDT"] == 30


async def test_candles(api) -> None:  # type: ignore[no-untyped-def]
    public = FakePublic()
    state = api.state(factory=FakeFactory(public=public))
    async with client_for(state) as c:
        r = await c.get("/api/market/XRP-USDT/candles", params={"interval": "1m"},
                        headers=auth(api.tid))
    body = r.json()
    assert body["interval"] == "1m" and len(body["candles"]) == 2
    assert body["candles"][1] == {"time": 1791824400, "open": "1.4960", "high": "1.4971",
                                  "low": "1.4949", "close": "1.4952"}
    assert public.calls["klines:1m"] == 1
    left = state.market_cache._data["api:candles:XRP-USDT:1m"].expires_at - time.monotonic()
    assert 4 < left <= 5                                          # 1m — 5 с


@pytest.mark.parametrize(("path", "params", "status", "code"), [
    ("/api/market/ZZZ-USDT", {}, 404, "SYMBOL_NOT_FOUND"),
    ("/api/market/BTC-USDC", {}, 404, "SYMBOL_NOT_FOUND"),
    ("/api/market/XRP-USDT/candles", {}, 200, None),
    ("/api/market/XRP-USDT", {"interval": "3m"}, 422, "BAD_REQUEST"),
    ("/api/market/XRP-USDT/candles", {"interval": "2h"}, 422, "BAD_REQUEST"),
])
async def test_market_validation(api, path, params, status, code) -> None:  # type: ignore[no-untyped-def]
    async with client_for(api.state()) as c:
        r = await c.get(path, params=params, headers=auth(api.tid))
    assert r.status_code == status
    if code is not None:
        assert r.json()["code"] == code


async def test_market_exchange_failures(api) -> None:  # type: ignore[no-untyped-def]
    down = ExchangeUnavailableError("down")
    no_price = FakePublic()
    no_price.fail["ticker"] = down
    no_details = FakePublic()
    no_details.fail["premium"] = down
    async with client_for(api.state(factory=FakeFactory(public=no_price))) as c:
        r1 = await c.get("/api/market/XRP-USDT", headers=auth(api.tid))
    async with client_for(api.state(factory=FakeFactory(public=no_details))) as c:
        r2 = await c.get("/api/market/XRP-USDT", headers=auth(api.tid))
    assert (r1.status_code, r1.json()["code"]) == (503, "EXCHANGE_UNAVAILABLE")
    assert no_price.closed == 1                                   # клиент закрыт и при сбое
    body = r2.json()
    assert r2.status_code == 200 and body["last"] == "1.4952"
    assert body["funding_rate_percent"] is None and body["details_error"]


async def test_market_leverage_unavailable(api) -> None:  # type: ignore[no-untyped-def]
    broken = FakeClient(fail=ExchangeUnavailableError("down"))
    async with client_for(api.state(factory=FakeFactory(has_key=False))) as c:
        no_key = (await c.get("/api/market/XRP-USDT", headers=auth(api.tid))).json()
    async with client_for(api.state(factory=FakeFactory(broken))) as c:
        failed = (await c.get("/api/market/XRP-USDT", headers=auth(api.tid))).json()
    assert no_key["leverage"] is None and "Ключ BingX" in no_key["leverage_error"]
    assert failed["leverage"] is None and "ExchangeUnavailableError" in failed["leverage_error"]
    assert broken.closed
