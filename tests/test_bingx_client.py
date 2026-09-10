"""Тесты клиента BingX.

Сеть не используется: httpx.AsyncClient подменяется транспортом,
возвращающим заранее заготовленные ответы. Это позволяет проверить
подпись, разбор данных и обработку ошибок без ключей и без биржи.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeUnavailableError,
)
from app.exchanges.bingx import BingXClient
from app.trading.enums import TradeSide

D = Decimal

# Ключи из публичного примера в документации BingX.
DOC_SECRET = "UuGuyEGt6ZEkpUObCYCmIfh0elYsZVh80jlYwpJuRZEw70t6vomMH7Sjmf94ztSI"


def make_client(handler, **kwargs) -> BingXClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport, base_url="https://open-api.bingx.com"
    )
    return BingXClient(
        api_key="test-key", api_secret=DOC_SECRET, client=http, **kwargs
    )


def ok(payload) -> httpx.Response:  # type: ignore[no-untyped-def]
    return httpx.Response(200, json={"code": 0, "msg": "", "data": payload})


class TestSignature:
    def test_matches_official_example(self) -> None:
        """Эталон из документации BingX.

        Совпадение здесь означает, что алгоритм подписи верен: это
        первое, что ломается при интеграции, и проверить иначе нельзя.
        """
        client = BingXClient(api_key="k", api_secret=DOC_SECRET)
        query = (
            "quoteOrderQty=20&side=BUY&symbol=ETHUSDT"
            "&timestamp=1649404670162&type=MARKET"
        )
        assert client._sign(query) == (
            "428a3c383bde514baff0d10d3c20e5adfaacaf799e324546dafe5ccc480dd827"
        )

    def test_signature_covers_exact_sent_query(self) -> None:
        """Подпись должна считаться по той же строке, что уходит на сервер."""
        client = BingXClient(api_key="k", api_secret=DOC_SECRET)
        signed = client._build_signed_query({"symbol": "BTC-USDT"})

        body, _, signature = signed.rpartition("&signature=")
        assert client._sign(body) == signature
        assert "timestamp=" in body
        assert "recvWindow=" in body

    def test_no_secret_raises_clear_error(self) -> None:
        client = BingXClient()
        with pytest.raises(ExchangeAuthError, match="ключи"):
            client._sign("anything")


class TestPublicData:
    async def test_ticker(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "symbol=BTC-USDT" in str(request.url)
            return ok({
                "symbol": "BTC-USDT",
                "lastPrice": "101234.5",
                "volume": "18000.25",
                "priceChangePercent": "2.31",
            })

        client = make_client(handler)
        ticker = await client.get_ticker("BTC-USDT")

        assert ticker.last_price == D("101234.5")
        assert ticker.price_change_percent == D("2.31")
        await client.close()

    async def test_klines_sorted_chronologically(self) -> None:
        """Биржа отдаёт свечи от новых к старым — анализ ждёт обратного."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {"time": 1700003600000, "open": "102", "high": "104",
                 "low": "101", "close": "103", "volume": "10"},
                {"time": 1700000000000, "open": "100", "high": "103",
                 "low": "99", "close": "102", "volume": "12"},
            ])

        client = make_client(handler)
        candles = await client.get_klines("BTC-USDT", "1h")

        assert len(candles) == 2
        assert candles[0].open_time < candles[1].open_time
        assert candles[0].open == D("100")
        await client.close()

    async def test_kline_metrics_for_methodology(self) -> None:
        """Показатели свечи, на которые опирается стратегия."""
        def handler(request: httpx.Request) -> httpx.Response:
            # Полнотелая бычья свеча: тело 8 из диапазона 10.
            return ok([{
                "time": 1700000000000, "open": "100", "high": "110",
                "low": "100", "close": "108", "volume": "5",
            }])

        client = make_client(handler)
        candle = (await client.get_klines("BTC-USDT", "4h"))[0]

        assert candle.is_bullish
        assert candle.body == D("8")
        assert candle.range == D("10")
        assert candle.body_ratio == D("0.8")   # проходит фильтр 0.6
        assert candle.upper_wick == D("2")
        assert candle.lower_wick == D("0")
        await client.close()

    async def test_unsupported_interval_rejected(self) -> None:
        client = make_client(lambda r: ok([]))
        with pytest.raises(ValueError, match="не поддерживается"):
            await client.get_klines("BTC-USDT", "3m")
        await client.close()


class TestPrivateData:
    async def test_balance(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-BX-APIKEY"] == "test-key"
            assert "signature=" in str(request.url)
            return ok({"balance": {
                "asset": "USDT", "balance": "10000.5",
                "equity": "10250.75", "unrealizedProfit": "250.25",
                "usedMargin": "2010", "availableMargin": "7990.5",
            }})

        client = make_client(handler)
        balance = await client.get_balance()

        assert balance.equity == D("10250.75")
        assert balance.available == D("7990.5")
        assert balance.used_margin == D("2010")
        await client.close()

    async def test_positions_skip_closed(self) -> None:
        """Биржа возвращает и закрытые позиции с нулевым объёмом."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {"symbol": "BTC-USDT", "positionSide": "LONG",
                 "positionAmt": "0.2", "avgPrice": "100500",
                 "markPrice": "101000", "leverage": "10",
                 "unrealizedProfit": "100", "initialMargin": "2010"},
                {"symbol": "ETH-USDT", "positionSide": "SHORT",
                 "positionAmt": "0", "avgPrice": "0"},
            ])

        client = make_client(handler)
        positions = await client.get_positions()

        assert len(positions) == 1
        assert positions[0].symbol == "BTC-USDT"
        assert positions[0].side is TradeSide.LONG
        assert positions[0].quantity == D("0.2")
        await client.close()

    async def test_fill_direction_derived_from_position_side(self) -> None:
        """BUY — это вход для лонга, но выход для шорта."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"fill_orders": [
                {"tradeId": "1", "symbol": "BTC-USDT", "positionSide": "LONG",
                 "side": "BUY", "avgPrice": "100000", "executedQty": "0.1",
                 "commission": "-4", "profit": "0", "filledTime": 1700000000000},
                {"tradeId": "2", "symbol": "ETH-USDT", "positionSide": "SHORT",
                 "side": "BUY", "avgPrice": "3000", "executedQty": "1",
                 "commission": "-1.2", "profit": "50",
                 "filledTime": 1700003600000},
            ]})

        client = make_client(handler)
        fills = await client.get_fills(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 8, tzinfo=UTC)
        )

        assert fills[0].is_entry is True    # BUY в лонг — вход
        assert fills[1].is_entry is False   # BUY в шорт — выход
        # Комиссия приходит отрицательной, храним модуль.
        assert fills[0].fee == D("4")
        await client.close()


class TestErrorHandling:
    async def test_auth_error_is_explicit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"code": 100001, "msg": "bad key"})

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExchangeAuthError, match="IP"):
            await client.get_balance()
        await client.close()

    async def test_business_error_code_in_200_response(self) -> None:
        """BingX отдаёт ошибки с HTTP 200 и кодом в теле."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": 100001, "msg": "signature verification failed"}
            )

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExchangeAuthError, match="signature"):
            await client.get_balance()
        await client.close()

    async def test_rate_limit_retries_then_raises(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeRateLimitError):
            await client.get_ticker("BTC-USDT")
        assert calls["n"] == 3
        await client.close()

    async def test_recovers_after_transient_failure(self) -> None:
        """Разовый сбой не должен ронять запрос: повтор обязан помочь."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502)
            return ok({"symbol": "BTC-USDT", "lastPrice": "100000",
                       "volume": "1", "priceChangePercent": "0"})

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        ticker = await client.get_ticker("BTC-USDT")
        assert ticker.last_price == D("100000")
        assert calls["n"] == 2
        await client.close()

    async def test_malformed_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="не json")

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExchangeResponseError, match="JSON"):
            await client.get_ticker("BTC-USDT")
        await client.close()

    async def test_timeout_becomes_readable_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=2)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError, match="вовремя"):
            await client.get_ticker("BTC-USDT")
        await client.close()


async def _noop() -> None:
    return None
