"""Тесты клиента BingX.

Сеть не используется: httpx.AsyncClient подменяется транспортом,
возвращающим заранее заготовленные ответы. Это позволяет проверить
подпись, разбор данных и обработку ошибок без ключей и без биржи.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    TpSlSpec,
)
from app.exchanges.bingx import BingXClient
from app.trading.enums import ExchangeKeyMode, OrderSide, TradeSide

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

    async def test_symbols_include_min_notional(self) -> None:
        """tradeMinUSDT нужен sizing.py (этап 15.3) для отказа SIZE_TOO_SMALL."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([{
                "symbol": "BTC-USDT", "status": 1, "pricePrecision": 1,
                "quantityPrecision": 4, "tradeMinQuantity": "0.0001",
                "tradeMinUSDT": "2",
            }])

        client = make_client(handler)
        symbols = await client.get_symbols()

        assert symbols[0].min_notional == D("2")
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

    async def test_balance_list_of_assets_shape(self) -> None:
        """Регрессия: реальный боевой ключ вернул баланс не вложенным
        объектом (test_balance выше), а списком записей по активам, где
        "balance" — строка-сумма, а не объект. Прежний код разворачивал
        её как вложенный объект и падал с AttributeError на data.get()
        у строки. Форма ответа — дословно то, что залогировано при
        первом воспроизведении бага (raw dump из живого аккаунта),
        не придумана по документации."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {
                    "userId": "1314404133518147588", "asset": "USDT",
                    "balance": "0.0000", "equity": "0.0000",
                    "unrealizedProfit": "0.0000", "realizedProfit": "0",
                    "availableMargin": "0.0000", "usedMargin": "0.0000",
                    "frozenMargin": "0.0000", "shortUid": "21792211",
                },
                {
                    "userId": "1314404133518147588", "asset": "USDC",
                    "balance": "0.0000", "equity": "0.0000",
                    "unrealizedProfit": "0.0000", "realizedProfit": "0",
                    "availableMargin": "0.0000", "usedMargin": "0.0000",
                    "frozenMargin": "0.0000", "shortUid": "21792211",
                },
            ])

        client = make_client(handler)
        balance = await client.get_balance()

        assert balance.asset == "USDT"
        assert balance.equity == D("0.0000")
        assert balance.available == D("0.0000")
        await client.close()

    async def test_balance_list_picks_usdt_not_first_entry(self) -> None:
        """Порядок активов в списке не гарантирован — берём запись USDT
        по полю asset, а не первую попавшуюся (data[0])."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {"asset": "USDC", "balance": "1.0", "equity": "1.0",
                 "unrealizedProfit": "0", "usedMargin": "0", "availableMargin": "1.0"},
                {"asset": "USDT", "balance": "500.0", "equity": "512.5",
                 "unrealizedProfit": "12.5", "usedMargin": "0", "availableMargin": "500.0"},
            ])

        client = make_client(handler)
        balance = await client.get_balance()

        assert balance.asset == "USDT"
        assert balance.equity == D("512.5")
        await client.close()

    async def test_balance_demo_mode_picks_vst_not_usdt(self) -> None:
        """DEMO торгует виртуальными VST, не USDT (см. _QUOTE_ASSET_BY_MODE
        в bingx.py) — список из нескольких активов, включая настоящий USDT
        вперемешку, должен выбрать именно VST, раз клиент создан в режиме
        DEMO."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {"asset": "USDT", "balance": "0.0", "equity": "0.0",
                 "unrealizedProfit": "0", "usedMargin": "0", "availableMargin": "0.0"},
                {"asset": "VST", "balance": "88329.9129", "equity": "88980.4276",
                 "unrealizedProfit": "-1138.5134", "usedMargin": "1789.0281",
                 "availableMargin": "88329.9129"},
            ])

        client = make_client(handler, mode=ExchangeKeyMode.DEMO)
        balance = await client.get_balance()

        assert balance.asset == "VST"
        assert balance.equity == D("88980.4276")
        await client.close()

    async def test_balance_missing_expected_asset_raises_clear_error(self) -> None:
        """Ожидаемого актива в ответе нет вовсе (например, DEMO-ключ
        случайно дёрнули с mode=LIVE) — не подставлять первый попавшийся
        актив молча, а поднять понятную ошибку с перечислением того, что
        реально вернула биржа."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok([
                {"asset": "VST", "balance": "100.0", "equity": "100.0",
                 "unrealizedProfit": "0", "usedMargin": "0", "availableMargin": "100.0"},
            ])

        client = make_client(handler, mode=ExchangeKeyMode.LIVE)
        with pytest.raises(ExchangeResponseError, match=r"USDT.*VST"):
            await client.get_balance()
        await client.close()

    async def test_api_restrictions_fields_on_top_level(self) -> None:
        """apiRestrictions — единственный приватный метод, где поля лежат
        на верхнем уровне ответа, рядом с code/msg, а не под "data" (в
        отличие от get_balance/get_positions выше). Значения — дословно
        то, что вернула биржа при живой разведке (не придуманы)."""
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-BX-APIKEY"] == "test-key"
            return httpx.Response(200, json={
                "code": 0, "msg": "",
                "ipRestrict": True,
                "createTime": 1789058346655,
                "permitsUniversalTransfer": False,
                "enableReading": True,
                "enableFutures": True,
                "enableSpotAndMarginTrading": False,
            })

        client = make_client(handler)
        restrictions = await client.get_api_restrictions()

        assert restrictions.ip_restrict is True
        assert restrictions.permits_universal_transfer is False
        assert restrictions.enable_reading is True
        assert restrictions.enable_futures is True
        assert restrictions.enable_spot_and_margin_trading is False
        assert restrictions.create_time == datetime.fromtimestamp(
            1789058346655 / 1000, tz=UTC
        )
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


class TestSetLeverage:
    async def test_sends_symbol_side_and_leverage_as_post(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            url = str(request.url)
            assert "symbol=BTC-USDT" in url
            assert "side=LONG" in url
            assert "leverage=5" in url
            assert "signature=" in url
            return ok({"leverage": 5, "symbol": "BTC-USDT"})

        client = make_client(handler)
        leverage = await client.set_leverage("BTC-USDT", 5, position_side="LONG")

        assert leverage == 5
        await client.close()

    async def test_one_way_mode_defaults_to_both(self) -> None:
        """Без position_side — односторонний режим счёта, BingX ждёт BOTH."""
        def handler(request: httpx.Request) -> httpx.Response:
            assert "side=BOTH" in str(request.url)
            return ok({"leverage": 10, "symbol": "ETH-USDT"})

        client = make_client(handler)
        await client.set_leverage("ETH-USDT", 10)
        await client.close()


class TestPlaceMarketOrder:
    async def test_entry_with_take_profit_and_stop_loss(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            url = unquote(str(request.url))
            assert "type=MARKET" in url
            assert "side=BUY" in url
            assert "positionSide=LONG" in url
            assert "clientOrderID=tj1-42-entry" in url
            # takeProfit/stopLoss — JSON, упакованный в строку параметра
            # (раздел 16 ТЗ — формат проверен по живой документации BingX).
            assert (
                '{"type":"TAKE_PROFIT_MARKET","stopPrice":65100,'
                '"workingType":"MARK_PRICE"}' in url
            )
            assert (
                '{"type":"STOP_MARKET","stopPrice":62400,'
                '"workingType":"MARK_PRICE"}' in url
            )
            return ok({"order": {
                "symbol": "BTC-USDT", "orderId": 123456, "side": "BUY",
                "positionSide": "LONG", "type": "MARKET", "status": "FILLED",
                "avgPrice": "63245.5", "executedQty": "0.014",
                "clientOrderId": "tj1-42-entry",
            }})

        client = make_client(handler)
        result = await client.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            position_side="LONG",
            quantity=D("0.014"),
            client_order_id="tj1-42-entry",
            take_profit=TpSlSpec(trigger_price=D("65100")),
            stop_loss=TpSlSpec(trigger_price=D("62400")),
        )

        assert result.status == "FILLED"
        assert result.avg_price == D("63245.5")
        assert result.executed_qty == D("0.014")
        assert result.client_order_id == "tj1-42-entry"
        await client.close()

    async def test_optional_price_included_in_tp_sl_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = unquote(str(request.url))
            assert (
                '{"type":"STOP_MARKET","stopPrice":62400,"price":62350,'
                '"workingType":"MARK_PRICE"}' in url
            )
            return ok({"order": {"symbol": "BTC-USDT", "status": "FILLED"}})

        client = make_client(handler)
        await client.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.SELL,
            position_side="SHORT",
            quantity=D("1"),
            client_order_id="tj-price-test",
            stop_loss=TpSlSpec(trigger_price=D("62400"), price=D("62350")),
        )
        await client.close()

    async def test_rejects_client_order_id_out_of_range(self) -> None:
        """BingX принимает clientOrderID только 1-40 символов (раздел 16 ТЗ)."""
        client = make_client(lambda r: ok({}))
        with pytest.raises(ValueError, match="1-40"):
            await client.place_market_order(
                symbol="BTC-USDT",
                side=OrderSide.BUY,
                position_side="LONG",
                quantity=D("1"),
                client_order_id="x" * 41,
            )
        await client.close()

    async def test_network_failure_does_not_retry(self) -> None:
        """Раздел 8 ТЗ: повторная отправка ордера после обрыва запрещена —
        клиент не должен маскировать обрыв автоматическим повтором, иначе
        решение "повторять или сверяться по client_order_id" примет не
        execution/service.py, а транспортный слой."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.place_market_order(
                symbol="BTC-USDT",
                side=OrderSide.BUY,
                position_side="LONG",
                quantity=D("1"),
                client_order_id="tj-timeout-test",
            )
        assert calls["n"] == 1
        await client.close()


class TestGetOrder:
    async def test_query_by_client_order_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert request.method == "GET"
            assert "symbol=BTC-USDT" in url
            assert "clientOrderID=tj1-42-entry" in url
            return ok({"order": {
                "symbol": "BTC-USDT", "orderId": 123456,
                "clientOrderId": "tj1-42-entry", "status": "FILLED",
                "avgPrice": "63245.5", "executedQty": "0.014",
                "commission": "-2.53", "side": "BUY", "positionSide": "LONG",
                "type": "MARKET",
            }})

        client = make_client(handler)
        result = await client.get_order("BTC-USDT", "tj1-42-entry")

        assert result.status == "FILLED"
        assert result.avg_price == D("63245.5")
        # Комиссия приходит отрицательной — храним модуль, как и в get_fills.
        assert result.fee == D("2.53")
        assert result.order_id == "123456"
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
