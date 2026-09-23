"""Тесты клиента BingX.

Сеть не используется: httpx.AsyncClient подменяется транспортом,
возвращающим заранее заготовленные ответы. Это позволяет проверить
подпись, разбор данных и обработку ошибок без ключей и без биржи.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from app.exchanges.base import (
    CONDITIONAL_WORKING_TYPE,
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    ReadbackIncomplete,
    TpSlSpec,
)
from app.exchanges.bingx import QUOTE_TICKER, BingXClient
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


def ok(payload, headers=None) -> httpx.Response:  # type: ignore[no-untyped-def]
    return httpx.Response(
        200, json={"code": 0, "msg": "", "data": payload}, headers=headers
    )


def rate_limit_headers(remaining: int, expire_ms: int) -> dict:  # type: ignore[no-untyped-def]
    """Заголовки остатка лимита BingX — см. app/exchanges/bingx.py,
    _HEADER_RATE_LIMIT_REMAIN/_EXPIRE (снято живым запросом)."""
    return {
        "X-RateLimit-Requests-Remain": str(remaining),
        "X-RateLimit-Requests-Expire": str(expire_ms),
    }


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


def _old_build_signed_query(params: dict, recv_window: int, secret: str, ts_ms: int) -> str:
    """Алгоритм _build_signed_query() ДО правки signature mismatch:
    кодирование урл-строки целиком (urlencode) перед подписью. Эталон для
    сравнения "ручка не изменилась" — встроен как есть, а не импортирован
    из продакшн-кода, чтобы тест сравнивал именно со старым поведением,
    а не с самим собой."""
    import hashlib
    import hmac as hmac_
    from urllib.parse import urlencode as urlencode_

    payload = dict(params)
    payload["timestamp"] = ts_ms
    payload["recvWindow"] = recv_window
    query = urlencode_(payload)
    signature = hmac_.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


class TestSignedQueryEncodingOrder:
    """Раздел 8 ТЗ / разведка signature mismatch (docs/execution-stage-15.md,
    раздел 16): BingX подписывает СЫРУЮ строку параметров, URL-кодирование
    значений — отдельным шагом, уже после подписи (официальная документация,
    раздел "Signature Description" и детальный пример "Place multiple
    orders" — см. докстринг app/exchanges/bingx.py:_build_signed_query).
    До правки код кодировал строку целиком (urlencode()) и подписывал уже
    закодированный результат — расхождение было незаметно для простых
    значений (encode() их не меняет), но ломало любой JSON-значение
    параметр (takeProfit/stopLoss)."""

    async def test_place_market_order_with_tp_sl_signs_raw_value_not_encoded(self) -> None:
        """Раньше сигнатура для этого запроса считалась по URL-кодированной
        строке (спецсимволы JSON — %7B, %22, %3A, %2C, %7D), а не по сырой —
        падало бы здесь до правки."""
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return ok({"order": {"symbol": "BTC-USDT", "status": "FILLED"}})

        client = make_client(handler)

        signed_over: dict[str, str] = {}
        original_sign = client._sign

        def spying_sign(query: str) -> str:
            signed_over["query"] = query
            return original_sign(query)

        client._sign = spying_sign  # type: ignore[method-assign]

        await client.place_market_order(
            symbol="BTC-USDT",
            side=OrderSide.BUY,
            position_side="LONG",
            quantity=D("0.014"),
            client_order_id="probe-1",
            take_profit=TpSlSpec(trigger_price=D("65100")),
            stop_loss=TpSlSpec(trigger_price=D("62400")),
        )

        raw_signed = signed_over["query"]
        sent_query = str(captured["request"].url.query, "ascii")

        # 1. Строка, по которой считалась подпись, содержит СЫРОЙ JSON —
        # буквальные {, ", :, , — не %7B/%22/%3A/%2C.
        assert (
            'takeProfit={"type":"TAKE_PROFIT_MARKET","stopPrice":65100,'
            '"workingType":"MARK_PRICE"}' in raw_signed
        )
        assert '"stopPrice":62400' in raw_signed  # stopLoss тем же образом
        assert "%7B" not in raw_signed and "%22" not in raw_signed

        # 2. А реально отправленный URL содержит УЖЕ закодированные значения.
        assert (
            "takeProfit=%7B%22type%22%3A%22TAKE_PROFIT_MARKET%22%2C"
            "%22stopPrice%22%3A65100%2C%22workingType%22%3A%22MARK_PRICE%22%7D"
            in sent_query
        )
        assert '{"type"' not in sent_query  # сырого JSON в URL быть не должно

        # 3. Подпись в отправленном URL — это действительно HMAC от сырой
        # (не закодированной) строки, а не от того, что реально ушло в URL.
        sent_signature = sent_query.rpartition("&signature=")[2]
        assert client._sign(raw_signed) == sent_signature
        await client.close()

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({}, id="get_balance/get_positions/get_api_restrictions"),
            pytest.param({"symbol": "BTC-USDT"}, id="get_open_orders(symbol=...)"),
            pytest.param(
                {"symbol": "BTC-USDT", "leverage": 10, "side": "LONG"}, id="set_leverage"
            ),
        ],
    )
    def test_simple_signed_endpoints_unchanged_by_fix(self, monkeypatch, params) -> None:  # type: ignore[no-untyped-def]
        """Для параметров без спецсимволов (как у get_balance, get_positions,
        get_open_orders, get_api_restrictions, set_leverage) urlencode()
        целиком и "сырая строка + кодирование значений по одному" дают
        байт-в-байт одинаковый результат — правка эти ручки не трогает.
        Должен проходить и на старом, и на новом коде: сравнивает текущую
        _build_signed_query() с эталонной реализацией "как было", встроенной
        в тест (_old_build_signed_query), а не наоборот."""
        import time as time_module

        fixed_ts = 1700000000000
        monkeypatch.setattr(time_module, "time", lambda: fixed_ts / 1000)

        client = BingXClient(api_key="k", api_secret=DOC_SECRET, recv_window=5000)

        actual = client._build_signed_query(dict(params))
        expected = _old_build_signed_query(
            params, recv_window=5000, secret=DOC_SECRET, ts_ms=fixed_ts
        )

        assert actual == expected


class TestBuildTpSl:
    """docs/execution-stage-15.md, раздел 16: значение takeProfit/stopLoss —
    JSON, упакованный в строку параметра, подписывается СЫРЫМ (см.
    TestSignedQueryEncodingOrder выше) — лишний пробел внутри изменил бы
    подписываемую строку. _build_tp_sl собирает JSON вручную, без пробелов
    (эквивалент separators=(",", ":")), но не через json.dumps — фиксируем
    инвариант тестом, чтобы будущий рефакторинг на json.dumps() по
    умолчанию (который пробелы как раз вставляет) не вернул баг."""

    def test_no_whitespace_in_tp_sl_json(self) -> None:
        from app.exchanges.bingx import _build_tp_sl
        from app.trading.enums import OrderType

        rendered = _build_tp_sl(
            OrderType.TAKE_PROFIT_MARKET,
            TpSlSpec(trigger_price=D("65100"), price=D("65050")),
        )
        assert " " not in rendered
        assert rendered == (
            '{"type":"TAKE_PROFIT_MARKET","stopPrice":65100,"price":65050,'
            '"workingType":"MARK_PRICE"}'
        )


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


class TestGetPositionMode:
    """Раздел 16 ТЗ, шаг 15.5.1: путь v1 (не v2 — тот отвечает code 100404,
    проверено дважды живым запросом на демо-хосте), значение под data."""

    async def test_parses_dual_side_position_from_data(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "/openApi/swap/v1/positionSide/dual" in str(request.url)
            return ok({"dualSidePosition": True})

        client = make_client(handler)
        assert await client.get_position_mode() is True
        await client.close()

    async def test_false_value_parsed_correctly(self) -> None:
        """bool(data["dualSidePosition"]) не должен молча стать True для
        любого непустого значения — сам JSON true/false уже bool, но
        проверяем явно, раз от этого зависит positionSide."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"dualSidePosition": False})

        client = make_client(handler)
        assert await client.get_position_mode() is False
        await client.close()

    async def test_missing_field_raises_explicit_error_not_default(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({})

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError, match="dualSidePosition"):
            await client.get_position_mode()
        await client.close()

    async def test_max_retries_override_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_position_mode(max_retries=1)
        assert calls["n"] == 1
        await client.close()


class TestGetLeverage:
    """Раздел 16 ТЗ, шаг 15.5.1: GET на тот же путь, что и set_leverage
    ниже — читает текущее и максимальное плечо по символу, раздельно
    по long/short (значения из живого запроса на демо-хосте, разведка
    раздела 16)."""

    async def test_parses_current_and_max_leverage_by_side(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "symbol=BTC-USDT" in str(request.url)
            return ok({
                "symbol": "BTC-USDT",
                "longLeverage": 20, "shortLeverage": 20,
                "maxLongLeverage": 150, "maxShortLeverage": 150,
                "availableLongVal": 1762415.2, "availableLongVol": 20.4655,
                "availableShortVal": 1762415.2, "availableShortVol": 20.4655,
                "maxPositionLongVal": 100000000.0, "maxPositionShortVal": 100000000.0,
            })

        client = make_client(handler)
        info = await client.get_leverage("BTC-USDT")

        assert info.symbol == "BTC-USDT"
        assert info.long_leverage == 20
        assert info.short_leverage == 20
        assert info.max_long_leverage == 150
        assert info.max_short_leverage == 150
        await client.close()

    async def test_missing_field_raises_explicit_error_not_default(self) -> None:
        """Раздел 16 ТЗ: живой /quote/contracts не отдаёт максимум плеча
        вовсе — SymbolInfo.max_leverage убрали из-за молчаливого дефолта
        20 именно по этой причине (см. коммит рефакторинга). get_leverage()
        не должен повторить тот же баг для своего собственного ответа:
        отсутствие поля — явная ошибка, не подставленное число."""
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "longLeverage": 20, "shortLeverage": 20})

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError, match="maxLongLeverage"):
            await client.get_leverage("BTC-USDT")
        await client.close()

    async def test_max_retries_override_does_not_retry(self) -> None:
        """Путь подтверждения («Да») держит Redis-лок — как и у остальных
        чтений (get_ticker/get_balance/get_symbol_info), max_retries=1
        не должен ретраить."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_leverage("BTC-USDT", max_retries=1)
        assert calls["n"] == 1
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

    async def test_network_failure_does_not_retry(self) -> None:
        """Раздел 8 ТЗ, докстринг bingx.py:692-699: торговые вызовы,
        меняющие состояние перед входом, не ретраятся автоматически — до
        этой правки set_leverage не передавал max_retries и молча ретраился
        3 раза, расходясь с собственным докстрингом файла."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.set_leverage("BTC-USDT", 5, position_side="LONG")
        assert calls["n"] == 1
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


class TestGetOpenOrders:
    """Фикстуры — реальные снимки GET /openApi/swap/v2/trade/openOrders
    (DEMO), не по документации: один ордер без TP/SL, один с заполненными
    takeProfit/stopLoss (см. docs/execution-stage-15.md, раздел 16)."""

    # BTC-USDT, выставлен без TP/SL — оба вложенных объекта присутствуют,
    # но это заглушки (stopPrice=0), а не заданный условник.
    NO_TPSL_ORDER = {
        "symbol": "BTC-USDT", "orderId": 2099406229323390976,
        "side": "BUY", "positionSide": "LONG", "type": "LIMIT",
        "origQty": "0.2517", "price": "70000.0", "executedQty": "0.0000",
        "avgPrice": "0.0", "status": "PENDING", "stopPrice": "",
        "workingType": "CONTRACT_PRICE", "clientOrderId": "",
        "time": 1789372424000, "updateTime": 1789372424814,
        "leverage": "20X", "reduceOnly": False, "closePosition": "false",
        "takeProfit": {
            "type": "TAKE_PROFIT", "quantity": 0, "stopPrice": 0,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
        "stopLoss": {
            "type": "STOP", "quantity": 0, "stopPrice": 0,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
    }

    # SOL-USDT, выставлен с заполненными TP/SL — price и quantity внутри
    # всё равно нулевые (исполнение по рынку), заданность видна только
    # по stopPrice.
    WITH_TPSL_ORDER = {
        "symbol": "SOL-USDT", "orderId": 2099409652890476544,
        "side": "BUY", "positionSide": "LONG", "type": "LIMIT",
        "origQty": "205.27", "price": "85.000", "executedQty": "0.00",
        "avgPrice": "0.000", "status": "PENDING", "stopPrice": "",
        "workingType": "CONTRACT_PRICE", "clientOrderId": "",
        "time": 1789373241000, "updateTime": 1789373241072,
        "leverage": "20X", "reduceOnly": False, "closePosition": "false",
        "takeProfit": {
            "type": "TAKE_PROFIT_MARKET", "quantity": 0, "stopPrice": 89.25,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
        "stopLoss": {
            "type": "STOP_MARKET", "quantity": 0, "stopPrice": 82.45,
            "price": 0, "workingType": "", "stopGuaranteed": "false",
        },
    }

    async def test_parses_known_fields_without_tpsl(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return ok({"orders": [self.NO_TPSL_ORDER]})

        client = make_client(handler)
        orders = await client.get_open_orders()

        assert len(orders) == 1
        order = orders[0]
        assert order.order_id == "2099406229323390976"
        assert order.client_order_id == ""
        assert order.symbol == "BTC-USDT"
        assert order.side == "BUY"
        assert order.position_side == "LONG"
        assert order.order_type == "LIMIT"
        assert order.quantity == D("0.2517")
        assert order.executed_qty == D("0.0000")
        assert order.price == D("70000.0")
        assert order.stop_price == D(0)  # "" в ответе, не 0 и не null
        assert order.status == "PENDING"
        assert order.leverage == 20
        assert order.reduce_only is False
        assert order.close_position is False
        assert order.working_type == "CONTRACT_PRICE"
        assert order.created_at == datetime.fromtimestamp(
            1789372424000 / 1000, tz=UTC
        )
        assert order.take_profit is None
        assert order.stop_loss is None
        await client.close()

    async def test_attached_tp_sl_parsed_when_stop_price_nonzero(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"orders": [self.WITH_TPSL_ORDER]})

        client = make_client(handler)
        orders = await client.get_open_orders()

        order = orders[0]
        assert order.take_profit is not None
        assert order.take_profit.trigger_price == D("89.25")
        assert order.take_profit.price == D(0)
        assert order.take_profit.quantity == D(0)
        assert order.stop_loss is not None
        assert order.stop_loss.trigger_price == D("82.45")
        await client.close()

    async def test_symbol_filter_passed_when_given(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "symbol=SOL-USDT" in str(request.url)
            return ok({"orders": []})

        client = make_client(handler)
        await client.get_open_orders(symbol="SOL-USDT")
        await client.close()

    async def test_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"orders": []})

        client = make_client(handler)
        orders = await client.get_open_orders()

        assert orders == []
        await client.close()

    async def test_unexpected_shape_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"orders": "не список"})

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError):
            await client.get_open_orders()
        await client.close()


class TestRateLimitThrottle:
    """См. app/core/config.py bingx_rate_limit_threshold/-_throttle_enabled
    и app/exchanges/bingx.py _maybe_throttle/_update_rate_limit."""

    async def test_headers_are_parsed_into_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"}, rate_limit_headers(499, 10000))

        client = make_client(handler)
        await client.get_ticker("BTC-USDT")

        state = client._rate_limits[QUOTE_TICKER]  # noqa: SLF001
        assert state.remaining == 499
        await client.close()

    async def test_throttles_when_remaining_at_or_below_threshold(self) -> None:
        calls: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            # Первый ответ сразу говорит "остаток на пороге" — второй
            # вызов должен притормозить ПЕРЕД отправкой, не постфактум.
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"}, rate_limit_headers(5, 4000))

        client = make_client(handler, rate_limit_threshold=20)
        slept = {"seconds": None}

        async def fake_sleep(seconds: float) -> None:
            slept["seconds"] = seconds

        client._sleep = fake_sleep  # type: ignore[assignment]

        await client.get_ticker("BTC-USDT")
        assert slept["seconds"] is None  # состояния ещё не было — не тормозим

        await client.get_ticker("BTC-USDT")
        assert slept["seconds"] is not None
        assert 0 < slept["seconds"] <= 4.0
        await client.close()

    async def test_does_not_throttle_when_remaining_above_threshold(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"}, rate_limit_headers(499, 10000))

        client = make_client(handler, rate_limit_threshold=20)
        slept = []
        client._sleep = lambda seconds: slept.append(seconds) or _noop()  # type: ignore[assignment]

        await client.get_ticker("BTC-USDT")  # заполняет state (remaining=499)
        await client.get_ticker("BTC-USDT")  # 499 > порога 20 — не тормозим

        assert slept == []
        await client.close()

    async def test_does_not_throttle_when_window_already_expired(self) -> None:
        """Наш state устарел (окно по расчётам давно кончилось) — не ждём
        по стухшему числу, оно уже не отражает реальный остаток биржи."""

        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"}, rate_limit_headers(1, 50))

        client = make_client(handler, rate_limit_threshold=20)
        slept = []
        client._sleep = lambda seconds: slept.append(seconds) or _noop()  # type: ignore[assignment]

        await client.get_ticker("BTC-USDT")  # remaining=1, окно всего 50мс
        await asyncio.sleep(0.1)  # ждём дольше окна — оно "истекло"
        await client.get_ticker("BTC-USDT")

        assert slept == []
        await client.close()

    async def test_disabled_by_setting(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"}, rate_limit_headers(1, 10000))

        client = make_client(
            handler, rate_limit_threshold=20, rate_limit_throttle_enabled=False
        )
        slept = []
        client._sleep = lambda seconds: slept.append(seconds) or _noop()  # type: ignore[assignment]

        await client.get_ticker("BTC-USDT")
        await client.get_ticker("BTC-USDT")

        assert slept == []
        await client.close()

    async def test_missing_headers_do_not_break_request_or_count_as_zero(self) -> None:
        """Ответ без заголовков лимита (сторонний прокси, обрыв и т.п.) —
        запрос не должен падать, и второй вызов не должен неожиданно
        тормозить, как если бы остаток был 0."""

        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"})  # без headers

        client = make_client(handler, rate_limit_threshold=20)
        slept = []
        client._sleep = lambda seconds: slept.append(seconds) or _noop()  # type: ignore[assignment]

        await client.get_ticker("BTC-USDT")
        await client.get_ticker("BTC-USDT")

        assert QUOTE_TICKER not in client._rate_limits  # noqa: SLF001
        assert slept == []
        await client.close()

    async def test_request_count_increments_per_sent_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"symbol": "BTC-USDT", "lastPrice": "1"})

        client = make_client(handler)
        assert client.request_count == 0
        await client.get_ticker("BTC-USDT")
        await client.get_ticker("ETH-USDT")
        assert client.request_count == 2
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

    async def test_business_error_carries_code_and_payload(self) -> None:
        """Раздел 16 ТЗ, шаг 15.5.2: путь отправки ордера различает REJECTED
        (биржа явно отказала) и UNKNOWN (мы не знаем) по exc.code — без
        структурных code/payload на исключении пришлось бы парсить текст
        сообщения. См. ExecutionService.submit_entry_order()."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": 80001, "msg": "insufficient margin"}
            )

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExchangeResponseError) as excinfo:
            await client.get_balance()
        assert excinfo.value.code == 80001
        assert excinfo.value.payload == {"code": 80001, "msg": "insufficient margin"}
        await client.close()

    async def test_unparseable_code_is_none_not_zero(self) -> None:
        """code_int раньше по умолчанию был 0 при нечисловом code — после
        этой правки 0 означает "биржа вернула ровно 0", а не "не смогли
        разобрать", иначе REJECTED/UNKNOWN спутали бы неразобранный код
        с успехом-но-не-совсем."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "oops", "msg": "weird"})

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExchangeResponseError) as excinfo:
            await client.get_balance()
        assert excinfo.value.code is None
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


class TestPerCallRetryOverride:
    """Раздел 8 ТЗ: путь подтверждения («Да») держит Redis-лок — обычный
    повтор клиента (по умолчанию max_retries=3) там только удлиняет
    удержание лока. get_ticker/get_balance/get_symbols принимают
    max_retries за вызов, не трогая дефолт клиента для остальных
    потребителей (сканер и т.п.), которые его не передают."""

    async def test_get_ticker_max_retries_override_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_ticker("BTC-USDT", max_retries=1)
        assert calls["n"] == 1
        await client.close()

    async def test_get_balance_max_retries_override_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_balance(max_retries=1)
        assert calls["n"] == 1
        await client.close()

    async def test_get_symbols_max_retries_override_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_symbols(max_retries=1)
        assert calls["n"] == 1
        await client.close()

    async def test_get_ticker_without_override_keeps_client_default(self) -> None:
        """Без max_retries — старое поведение не меняется (остальные
        потребители клиента, например сканер, ничего не передают)."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]

        with pytest.raises(ExchangeUnavailableError):
            await client.get_ticker("BTC-USDT")
        assert calls["n"] == 3
        await client.close()


async def _noop() -> None:
    return None


# --- Шаг 15.5.3: строгий read-back и условный ордер -------------------------
#
# СИНТЕТИКА ДО 15.5.5, заменить живым снимком: форма ответа GET
# /trade/order по исполненному маркет-ордеру и POST условного ордера
# живьём не сняты (раздел 16). Имена полей — те же, что уже разбирает
# _parse_order (orderId, status, avgPrice, origQty, executedQty,
# commission).
SYNTHETIC_FILLED_ORDER = {
    "symbol": "BTC-USDT", "orderId": 2100000000000000001,
    "clientOrderId": "tj105u1E", "side": "BUY", "positionSide": "LONG",
    "type": "MARKET", "status": "FILLED", "origQty": "0.0100",
    "executedQty": "0.0100", "avgPrice": "86012.4", "commission": "-0.4301",
}


class TestGetOrderFill:
    async def test_parses_required_fields(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert request.method == "GET"
            assert "clientOrderID=tj105u1E" in url
            return ok({"order": SYNTHETIC_FILLED_ORDER})

        client = make_client(handler)
        fill = await client.get_order_fill("BTC-USDT", "tj105u1E", max_retries=1)

        assert fill.order_id == "2100000000000000001"
        assert fill.status == "FILLED"
        assert fill.avg_price == D("86012.4")
        assert fill.orig_qty == D("0.0100")
        assert fill.executed_qty == D("0.0100")
        assert fill.fee == D("0.4301")
        await client.close()

    @pytest.mark.parametrize(
        "field", ["avgPrice", "executedQty", "origQty", "commission", "status", "orderId"]
    )
    async def test_missing_field_raises_not_zero(self, field: str) -> None:
        """«Поля нет» ≠ «поле = 0»: отсутствие обязательного поля — явная
        ошибка с его именем, а не молчаливый Decimal(0) (как в _parse_order)."""
        order = {k: v for k, v in SYNTHETIC_FILLED_ORDER.items() if k != field}
        client = make_client(lambda r: ok({"order": order}))

        with pytest.raises(ReadbackIncomplete) as info:
            await client.get_order_fill("BTC-USDT", "tj105u1E")
        assert info.value.field == field
        await client.close()

    async def test_empty_avg_price_raises(self) -> None:
        order = {**SYNTHETIC_FILLED_ORDER, "avgPrice": ""}
        client = make_client(lambda r: ok({"order": order}))
        with pytest.raises(ReadbackIncomplete, match="avgPrice"):
            await client.get_order_fill("BTC-USDT", "tj105u1E")
        await client.close()

    async def test_explicit_zero_is_a_value(self) -> None:
        """Поле есть и равно 0 — это значение, не ошибка."""
        order = {**SYNTHETIC_FILLED_ORDER, "commission": "0", "executedQty": "0"}
        client = make_client(lambda r: ok({"order": order}))
        fill = await client.get_order_fill("BTC-USDT", "tj105u1E")
        assert fill.fee == D("0")
        assert fill.executed_qty == D("0")
        await client.close()

    async def test_order_id_alternative_spelling(self) -> None:
        order = {k: v for k, v in SYNTHETIC_FILLED_ORDER.items() if k != "orderId"}
        order["orderID"] = "777"
        client = make_client(lambda r: ok({"order": order}))
        fill = await client.get_order_fill("BTC-USDT", "tj105u1E")
        assert fill.order_id == "777"
        await client.close()

    async def test_max_retries_one_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.get_order_fill("BTC-USDT", "tj105u1E", max_retries=1)
        assert calls["n"] == 1
        await client.close()


class TestGetOpenOrdersMaxRetries:
    async def test_max_retries_one_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.get_open_orders("BTC-USDT", max_retries=1)
        assert calls["n"] == 1
        await client.close()


class TestPlaceConditionalOrder:
    async def test_stop_market_close_position_params(self) -> None:
        """Спасение стопа LONG: закрывающая сторона SELL, positionSide LONG,
        closePosition=true, без quantity, тот же workingType, что у входа."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            params = dict(httpx.QueryParams(request.url.query))
            assert params["type"] == "STOP_MARKET"
            assert params["side"] == "SELL"
            assert params["positionSide"] == "LONG"
            assert params["stopPrice"] == "84300.1"
            assert params["closePosition"] == "true"
            assert params["workingType"] == CONDITIONAL_WORKING_TYPE
            assert params["clientOrderID"] == "tj105u1S"
            assert "quantity" not in params
            return ok({"order": {"orderId": 2100000000000000009, "status": "NEW"}})

        client = make_client(handler)
        result = await client.place_conditional_order(
            symbol="BTC-USDT", side=OrderSide.SELL, position_side="LONG",
            order_type="STOP_MARKET", stop_price=D("84300.1"),
            client_order_id="tj105u1S",
        )
        assert result.order_id == "2100000000000000009"
        await client.close()

    async def test_rejects_non_conditional_type(self) -> None:
        client = make_client(lambda r: ok({}))
        with pytest.raises(ValueError, match="Не условный"):
            await client.place_conditional_order(
                symbol="BTC-USDT", side=OrderSide.SELL, position_side="LONG",
                order_type="MARKET", stop_price=D("1"), client_order_id="tj1u1S",
            )
        await client.close()

    async def test_network_failure_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.place_conditional_order(
                symbol="BTC-USDT", side=OrderSide.BUY, position_side="SHORT",
                order_type="TAKE_PROFIT_MARKET", stop_price=D("80000"),
                client_order_id="tj105u1T",
            )
        assert calls["n"] == 1
        await client.close()


def test_entry_and_rescue_share_working_type() -> None:
    """Одна константа для вложенных TP/SL входа и для спасения — тип
    триггера не может разойтись между двумя местами."""
    assert TpSlSpec(trigger_price=D("1")).working_type == CONDITIONAL_WORKING_TYPE
    assert CONDITIONAL_WORKING_TYPE == "MARK_PRICE"



class TestOrderFillFilledAt:
    """Шаг 15.5.4: время исполнения — мягко (не цифра сделки): есть →
    datetime, нет или мусор → None, без ReadbackIncomplete. СИНТЕТИКА ДО
    15.5.5 — имя поля (updateTime/time)."""

    def test_update_time_parsed(self) -> None:
        fill = BingXClient._parse_order_fill(
            {**SYNTHETIC_FILLED_ORDER, "updateTime": 1789372424814}
        )
        assert fill.filled_at == datetime(2026, 9, 14, 7, 53, 44, 814000, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "", 0, "not-a-number"])
    def test_missing_or_garbage_is_none_not_error(self, value: object) -> None:
        order = {**SYNTHETIC_FILLED_ORDER}
        if value is not None:
            order["updateTime"] = value
        assert BingXClient._parse_order_fill(order).filled_at is None



def test_fill_carries_order_id_for_import_dedupe() -> None:
    """Шаг 15.5.4: orderId исполнения — по нему импорт узнаёт ордера бота
    (СИНТЕТИКА ДО 15.5.5 — имя поля в allFillOrders)."""
    base = {
        "symbol": "BTC-USDT", "tradeId": "t1", "side": "BUY", "positionSide": "LONG",
        "price": "100", "qty": "0.1", "commission": "-0.01", "time": 1789372424000,
    }
    assert BingXClient._parse_fill({**base, "orderId": "555"}).order_id == "555"
    assert BingXClient._parse_fill(base).order_id is None
