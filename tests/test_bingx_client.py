"""Тесты клиента BingX.

Сеть не используется: httpx.AsyncClient подменяется транспортом,
возвращающим заранее заготовленные ответы. Это позволяет проверить
подпись, разбор данных и обработку ошибок без ключей и без биржи.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
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
    OrderNotFoundError,
    ReadbackIncomplete,
    TpSlSpec,
    UnsupportedPositionMode,
)
from app.exchanges.bingx import QUOTE_TICKER, BingXClient
from app.trading.enums import ExchangeKeyMode, OrderSide, TradeSide
from tests.bingx_fixtures import PUBLIC, live_call, live_items

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


_HOSTS = ["live", "demo"]


class TestPublicLiveForms:
    """Замки на живых формах публичных ручек (фикстура PUBLIC, 29.09): разбор
    живого ответа не меняется. На старом коде зелёные — это не регрессии."""

    @pytest.mark.parametrize("host", _HOSTS)
    async def test_lock_live_ticker(self, host: str) -> None:
        item = live_items(f"ticker BTC ({host})", PUBLIC)[0]
        client = make_client(lambda r: ok(item))
        ticker = await client.get_ticker("BTC-USDT")
        assert ticker.symbol == "BTC-USDT"
        assert ticker.last_price == D(item["lastPrice"])
        await client.close()

    @pytest.mark.parametrize("host", _HOSTS)
    async def test_lock_live_klines_have_no_close_time(self, host: str) -> None:
        """klines v3 живьём — объект без closeTime: закрытие считается как
        открытие + длительность таймфрейма, а не берётся из ответа."""
        items = live_items(f"klines BTC 1h ({host})", PUBLIC)
        assert all("closeTime" not in item for item in items)
        client = make_client(lambda r: ok(items))
        candles = await client.get_klines("BTC-USDT", "1h")
        assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
        newest = max(items, key=lambda i: i["time"])
        assert candles[-1].low == D(newest["low"])
        assert candles[-1].close_time - candles[-1].open_time == timedelta(hours=1)
        await client.close()

    @pytest.mark.parametrize("host", _HOSTS)
    async def test_lock_live_mark_price(self, host: str) -> None:
        item = live_items(f"premiumIndex BTC ({host})", PUBLIC)[0]
        client = make_client(lambda r: ok(item))
        assert await client.get_mark_price("BTC-USDT") == D(item["markPrice"])
        await client.close()

    @pytest.mark.parametrize("host", _HOSTS)
    async def test_lock_live_contracts(self, host: str) -> None:
        """Во всём живом списке (1185-1239 записей) точности и минимумы есть
        у каждой записи, status — только 1/25; tradeMinQuantity — JSON-float."""
        call = live_call(f"contracts ({host})", PUBLIC)
        assert all(
            not stats["missing"] and not stats["empty"]
            for stats in call["numeric_stats"].values()
        )
        assert call["status_values"] == ["1", "25"]
        items = live_items(f"contracts ({host})", PUBLIC)
        client = make_client(lambda r: ok(items))
        btc = {s.symbol: s for s in await client.get_symbols()}["BTC-USDT"]
        assert (btc.price_precision, btc.quantity_precision) == (1, 4)
        assert btc.min_quantity == D("0.0001")
        assert btc.min_notional == D("2")
        await client.close()


class TestPrivateData:
    async def test_balance(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-BX-APIKEY"] == "test-key"
            assert "signature=" in str(request.url)
            return ok([{
                "asset": "USDT", "balance": "10000.5",
                "equity": "10250.75", "unrealizedProfit": "250.25",
                "usedMargin": "2010", "availableMargin": "7990.5",
            }])

        client = make_client(handler)
        balance = await client.get_balance()

        assert balance.equity == D("10250.75")
        assert balance.available == D("7990.5")
        assert balance.used_margin == D("2010")
        await client.close()

    async def test_balance_nested_object_form_is_error(self) -> None:
        """Форма {"balance": {...}} живьём не встречалась (боевой ключ и демо —
        список по активам): не разбираем, а asset не подставляем."""
        client = make_client(lambda r: ok({"balance": {
            "asset": "USDT", "balance": "10000.5", "equity": "10250.75",
            "unrealizedProfit": "250.25", "usedMargin": "2010", "availableMargin": "7990.5",
        }}))
        with pytest.raises(ExchangeResponseError, match="не список"):
            await client.get_balance()
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
        DEMO.

        СИНТЕТИКА: числа VST не помечены живыми ни в тесте, ни в коммите
        13d3c7a (11.09), где фикстура появилась. Семантику полей
        balance/availableMargin по ней не выводить — живой снимок с открытой
        позицией снимается на ближайшем демо-входе (handoff)."""
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

    @pytest.mark.parametrize(
        "available_margin",
        [
            pytest.param(None, id="поля нет"),
            pytest.param("", id="пустая строка"),
        ],
    )
    async def test_balance_without_available_margin_is_none_not_wallet(
        self, available_margin: str | None
    ) -> None:
        """Хвост 26.09: нет availableMargin — available None, а не поле
        "balance" (старый код брал его молча через `or`) и не 0 (так
        _to_decimal читает None/""). Синтетика из живого: набор полей —
        живой дамп из test_balance_list_of_assets_shape, availableMargin
        убран или пуст, суммы ненулевые, чтобы подмена была видна."""
        entry = {
            "userId": "1314404133518147588", "asset": "USDT",
            "balance": "500.0000", "equity": "512.5000",
            "unrealizedProfit": "12.5000", "realizedProfit": "0",
            "usedMargin": "0.0000", "frozenMargin": "0.0000",
            "shortUid": "21792211",
        }
        if available_margin is not None:
            entry["availableMargin"] = available_margin

        def handler(request: httpx.Request) -> httpx.Response:
            return ok([entry])

        client = make_client(handler)
        balance = await client.get_balance()

        assert balance.available is None
        assert balance.equity == D("512.5000")
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


# Шаг 15.5.4а: форма /openApi/swap/v2/user/positions снята живьём на демо
# (хедж), прогоны 25-26.09.2026: (a) нет позиций, (b) LONG BTC-USDT открыт,
# (c) закрыт, (d) SHORT BTC-USDT открыт, (e) закрыт. Живые — список ключей
# записи и symbol/positionSide/positionAmt/availableAmt с типами. Значения
# остальных полей не печатались (ответ полями по списку, не repr()) —
# в фикстуре они синтетика.
_LIVE_POSITION_KEYS = [
    "availableAmt", "avgPrice", "createTime", "currency", "initialMargin",
    "isolated", "leverage", "liquidationPrice", "margin", "markPrice",
    "maxMarginReduction", "minIncreaseMargin", "onlyOnePosition", "pnlRatio",
    "positionAmt", "positionId", "positionSide", "positionValue",
    "realisedProfit", "riskRate", "symbol", "unrealizedProfit", "updateTime",
]


def _live_position(position_side: str, amount: str) -> dict[str, object]:
    item: dict[str, object] = {
        # Синтетика — см. комментарий выше.
        "avgPrice": "100000", "createTime": 1790000000000, "currency": "VST",
        "initialMargin": "100", "isolated": False, "leverage": 10,
        "liquidationPrice": 0, "margin": "100", "markPrice": "100100",
        "maxMarginReduction": "0", "minIncreaseMargin": "0", "onlyOnePosition": False,
        "pnlRatio": "0", "positionId": "1", "positionValue": "84000",
        "realisedProfit": "0", "riskRate": "0", "unrealizedProfit": "0",
        "updateTime": 1790000000000,
        # Живые значения и типы.
        "symbol": "BTC-USDT",
        "positionSide": position_side,
        "positionAmt": amount,
        "availableAmt": amount,
    }
    assert sorted(item) == _LIVE_POSITION_KEYS
    return item


class TestGetPositions:
    """Шаг 15.5.4а: строгий разбор — по нему гвард EXCHANGE_POSITION_EXISTS."""

    async def _positions(self, payload):  # type: ignore[no-untyped-def]
        client = make_client(lambda request: ok(payload))
        try:
            return await client.get_positions()
        finally:
            await client.close()

    async def test_no_positions_is_empty_list(self) -> None:
        """Прогоны (a), (c), (e): без позиций и после закрытия — пустой
        список; закрытая позиция с нулём в списке не остаётся."""
        assert await self._positions([]) == []

    async def test_live_long(self) -> None:
        """Прогон (b): positionAmt — строка, разбирается в Decimal."""
        [position] = await self._positions([_live_position("LONG", "0.8397")])
        assert position.symbol == "BTC-USDT"
        assert position.side is TradeSide.LONG
        assert position.quantity == D("0.8397")

    async def test_live_short_amount_is_positive(self) -> None:
        """Прогон (d): у SHORT в хедже positionAmt положительный — сторона
        только из positionSide."""
        [position] = await self._positions([_live_position("SHORT", "1.2592")])
        assert position.side is TradeSide.SHORT
        assert position.quantity == D("1.2592")

    async def test_one_way_both_blocks_with_named_reason(self) -> None:
        with pytest.raises(UnsupportedPositionMode) as exc_info:
            await self._positions([_live_position("BOTH", "0.5")])
        assert isinstance(exc_info.value, ExchangeResponseError)
        assert str(exc_info.value) == (
            "BTC-USDT: позиция в режиме one-way (BOTH) — форма ответа не проверена, "
            "вход заблокирован"
        )

    @pytest.mark.parametrize("side", [None, "", "long", "HEDGE"])
    async def test_unknown_position_side_fails(self, side: object) -> None:
        item = _live_position("LONG", "0.8397")
        if side is None:
            del item["positionSide"]
        else:
            item["positionSide"] = side
        with pytest.raises(ExchangeResponseError, match="positionSide"):
            await self._positions([item])

    @pytest.mark.parametrize("amount", [None, "", "abc"])
    async def test_unparseable_amount_fails(self, amount: object) -> None:
        item = _live_position("LONG", "0.8397")
        if amount is None:
            del item["positionAmt"]
        else:
            item["positionAmt"] = amount
        with pytest.raises(ExchangeResponseError, match="positionAmt"):
            await self._positions([item])

    async def test_available_amt_is_not_a_fallback(self) -> None:
        item = _live_position("LONG", "0.8397")
        del item["positionAmt"]
        with pytest.raises(ExchangeResponseError, match="positionAmt"):
            await self._positions([item])

    async def test_negative_amount_fails(self) -> None:
        """В хедже минуса не видели — не берём abs() наугад."""
        with pytest.raises(ExchangeResponseError, match="отрицательный"):
            await self._positions([_live_position("SHORT", "-1.2592")])

    async def test_zero_amount_is_skipped(self) -> None:
        """Живьём не наблюдалось, но ноль — не живая позиция."""
        assert await self._positions([_live_position("LONG", "0")]) == []

    @pytest.mark.parametrize("leverage", ["10X", "10.0", {"x": 1}])
    async def test_unparseable_leverage_is_exchange_error(self, leverage: object) -> None:
        """Не ValueError/TypeError: потребители ловят только ExchangeError."""
        item = _live_position("LONG", "0.8397")
        item["leverage"] = leverage
        with pytest.raises(ExchangeResponseError, match="leverage"):
            await self._positions([item])

    async def test_missing_symbol_fails(self) -> None:
        item = _live_position("LONG", "0.8397")
        del item["symbol"]
        with pytest.raises(ExchangeResponseError, match="symbol"):
            await self._positions([item])

    @pytest.mark.parametrize("payload", [{}, {"positions": []}, None])
    async def test_not_a_list_fails(self, payload: object) -> None:
        with pytest.raises(ExchangeResponseError, match="не список"):
            await self._positions(payload)

    async def test_max_retries_one_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.get_positions(max_retries=1)
        assert calls["n"] == 1
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
        assert result.order_id == "123456"
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
        # "" в ответе (не 0 и не null) — у ордера нет триггера: None, не 0.
        assert order.stop_price is None
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

        state = client._rate_limits[("GET", QUOTE_TICKER)]  # noqa: SLF001
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

        assert ("GET", QUOTE_TICKER) not in client._rate_limits  # noqa: SLF001
        assert slept == []
        await client.close()

    async def test_get_low_remaining_does_not_throttle_post_on_same_path(self) -> None:
        """28.09: лимит BingX — по (метод, путь). GET trade/leverage с малым
        остатком не должен усыплять следующий POST того же пути (27.09 вход
        LINK/SOL спал 1.0 с между get_leverage и set_leverage)."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                data = {"longLeverage": 5, "shortLeverage": 5, "maxLongLeverage": 50,
                        "maxShortLeverage": 50}
                return ok(data, rate_limit_headers(1, 10000))
            return ok({"leverage": 7, "symbol": "BTC-USDT"}, rate_limit_headers(4, 10000))

        client = make_client(handler, rate_limit_threshold=20)
        slept: list[float] = []
        client._sleep = lambda seconds: slept.append(seconds) or _noop()  # type: ignore[assignment]

        await client.get_leverage("BTC-USDT")
        await client.set_leverage("BTC-USDT", 7, position_side="LONG")

        assert calls == ["GET", "POST"]
        assert slept == []
        await client.close()

    async def test_post_keeps_own_low_remaining_across_get_on_same_path(self) -> None:
        """POST с малым остатком, между ними GET того же пути с большим
        остатком — второй POST всё равно ждёт: GET не перезатирает
        состояние POST (раньше ключ был один на путь)."""

        events: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            events.append(request.method)
            if request.method == "GET":
                data = {"longLeverage": 5, "shortLeverage": 5, "maxLongLeverage": 50,
                        "maxShortLeverage": 50}
                return ok(data, rate_limit_headers(29, 10000))
            return ok({"leverage": 7, "symbol": "BTC-USDT"}, rate_limit_headers(4, 10000))

        client = make_client(handler, rate_limit_threshold=20)
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            events.append("sleep")
            slept.append(seconds)

        client._sleep = fake_sleep  # type: ignore[assignment]

        await client.set_leverage("BTC-USDT", 7, position_side="LONG")
        await client.get_leverage("BTC-USDT")
        await client.set_leverage("BTC-USDT", 7, position_side="LONG")

        # GET не ждёт (у него своё состояние), второй POST — ждёт.
        assert events == ["POST", "GET", "sleep", "POST"]
        assert 0 < slept[0] <= 10.0
        await client.close()

    async def test_post_logs_remaining_and_window_at_info(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """Лимиты POST-ручек пути входа копятся фактом с каждого входа —
        INFO (на проде уровень INFO), без отдельных запросов."""

        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"leverage": 7, "symbol": "BTC-USDT"}, rate_limit_headers(4, 1000))

        client = make_client(handler)
        with caplog.at_level(logging.INFO, logger="app.exchanges.bingx"):
            await client.set_leverage("BTC-USDT", 7, position_side="LONG")

        records = [r for r in caplog.records if r.getMessage() == "Лимит BingX после POST"]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert records[0].remaining == 4  # type: ignore[attr-defined]
        assert records[0].expire_ms == 1000  # type: ignore[attr-defined]
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
# Живой ответ GET /trade/order по исполненному маркет-входу LINK #3 (демо
# 27.09, execution_orders #37): orderId и positionID — int, числа — строки,
# commission отрицательная, clientOrderId биржа отдаёт строчными (tj209u1e
# при tj209u1E в БД). Ответ POST условного ордера живьём не снят — ниже
# синтетика с пометкой.
LINK_ENTRY_CID = "tj209u1E"


def _live_entry() -> dict[str, object]:
    from tests.bingx_fixtures import live_items

    [raw] = live_items("order #37 LINK-USDT ENTRY")
    return raw


class TestGetOrderFill:
    async def test_parses_live_filled_entry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert request.method == "GET"
            assert f"clientOrderID={LINK_ENTRY_CID}" in url
            return ok({"order": _live_entry()})

        client = make_client(handler)
        fill = await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID, max_retries=1)

        assert fill.order_id == "2104122757776154624"
        assert fill.client_order_id == "tj209u1e"  # Р1: эхо биржи строчными
        assert fill.status == "FILLED"
        assert fill.avg_price == D("14.400")
        assert fill.orig_qty == D("2037.8")
        assert fill.executed_qty == D("2037.8")
        assert fill.fee == D("14.672461")
        assert fill.filled_at == datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC)
        assert fill.raw["positionID"] == 2104122757805514754
        await client.close()

    @pytest.mark.parametrize(
        "field", ["avgPrice", "executedQty", "origQty", "commission", "status", "orderId"]
    )
    async def test_missing_field_raises_not_zero(self, field: str) -> None:
        """«Поля нет» ≠ «поле = 0»: отсутствие обязательного поля — явная
        ошибка с его именем, а не молчаливый Decimal(0) (как в _parse_order).
        СИНТЕТИКА ИЗ ЖИВОГО #37, убрано поле."""
        order = {k: v for k, v in _live_entry().items() if k != field}
        client = make_client(lambda r: ok({"order": order}))

        with pytest.raises(ReadbackIncomplete) as info:
            await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID)
        assert info.value.field == field
        await client.close()

    async def test_empty_avg_price_raises(self) -> None:
        """СИНТЕТИКА ИЗ ЖИВОГО #37, заменено: avgPrice."""
        order = {**_live_entry(), "avgPrice": ""}
        client = make_client(lambda r: ok({"order": order}))
        with pytest.raises(ReadbackIncomplete, match="avgPrice"):
            await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID)
        await client.close()

    async def test_explicit_zero_is_a_value(self) -> None:
        """Поле есть и равно 0 — это значение, не ошибка. СИНТЕТИКА ИЗ
        ЖИВОГО #37, заменены: commission, executedQty."""
        order = {**_live_entry(), "commission": "0", "executedQty": "0"}
        client = make_client(lambda r: ok({"order": order}))
        fill = await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID)
        assert fill.fee == D("0")
        assert fill.executed_qty == D("0")
        await client.close()

    async def test_order_id_alternative_spelling(self) -> None:
        """В GET живьём только orderId; orderID — в ответе POST (разведка
        27.09). СИНТЕТИКА ИЗ ЖИВОГО #37, orderId → orderID."""
        order = {k: v for k, v in _live_entry().items() if k != "orderId"}
        order["orderID"] = "777"
        client = make_client(lambda r: ok({"order": order}))
        fill = await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID)
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
            await client.get_order_fill("LINK-USDT", LINK_ENTRY_CID, max_retries=1)
        assert calls["n"] == 1
        await client.close()


class TestOrderFillNotFilledLiveForm:
    """Р2, 29.09: ордер в статусе NEW живьём — commission и profit пустые
    строки, avgPrice "0.000" (GET #38, условник LINK до срабатывания, демо
    27.09). Разбор сначала смотрит status: не FILLED — «не исполнен», пустая
    комиссия не ошибка; FILLED с пустой комиссией — по-прежнему ошибка."""

    def test_live_new_order_is_not_filled_not_incomplete(self) -> None:
        from tests.bingx_fixtures import live_items

        [raw] = live_items("order #38 LINK-USDT STOP_LOSS")
        assert (raw["status"], raw["commission"]) == ("NEW", "")

        fill = BingXClient._parse_order_fill(raw)

        assert fill.status == "NEW"
        assert fill.order_id == "2104122758140616705"
        assert fill.executed_qty == D("0")
        assert fill.orig_qty == D("2037.8")

    def test_filled_with_empty_commission_is_still_incomplete(self) -> None:
        from tests.bingx_fixtures import live_items

        [raw] = live_items("order #37 LINK-USDT ENTRY")
        with pytest.raises(ReadbackIncomplete) as info:
            BingXClient._parse_order_fill({**raw, "commission": ""})
        assert info.value.field == "commission"

    def test_not_filled_without_status_is_incomplete(self) -> None:
        from tests.bingx_fixtures import live_items

        [raw] = live_items("order #38 LINK-USDT STOP_LOSS")
        with pytest.raises(ReadbackIncomplete) as info:
            BingXClient._parse_order_fill({k: v for k, v in raw.items() if k != "status"})
        assert info.value.field == "status"


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


class TestOpenOrdersLiveConditionals:
    """Живой openOrders LINK (демо 27.09): TP/SL, вложенные во вход, на бирже —
    отдельные условники. origQty — объём позиции, reduceOnly true,
    closePosition "false", avgPrice — цена входа позиции, positionID — id
    позиции, clientOrderId пустой, свои вложенные takeProfit/stopLoss —
    заглушки (type "", stopPrice 0). Тот же ордер в GET по orderId —
    reduceOnly false, closePosition "", positionID 0 (#38/#39)."""

    async def test_parses_live_stop_and_take(self) -> None:
        from tests.bingx_fixtures import live_items

        client = make_client(lambda r: ok({"orders": live_items("openOrders LINK")}))
        stop, take = await client.get_open_orders("LINK-USDT")

        assert (stop.order_id, stop.order_type, stop.stop_price) == (
            "2104122758140616705", "STOP_MARKET", D("13.526")
        )
        assert (take.order_id, take.order_type, take.stop_price) == (
            "2104122758140616704", "TAKE_PROFIT_MARKET", D("16.263")
        )
        for order in (stop, take):
            assert (order.side, order.position_side, order.status) == ("SELL", "LONG", "NEW")
            assert order.client_order_id == ""
            assert order.quantity == D("2037.8")
            assert (order.reduce_only, order.close_position) == (True, False)
            assert order.working_type == "MARK_PRICE"
            assert (order.take_profit, order.stop_loss) == (None, None)
            assert order.created_at == datetime(2026, 9, 27, 8, 15, 32, 908000, tzinfo=UTC)
        await client.close()


class TestPlaceConditionalOrder:
    """СИНТЕТИКА: ответ POST условного ордера (спасение стопа) живьём не снят —
    ни одного спасения на демо не было."""

    async def test_stop_market_close_position_params(self) -> None:
        """Стоп LONG на всю позицию: SELL, positionSide LONG, closePosition=true
        И quantity (без него BingX — 109400, разведка 02.10), MARK_PRICE, без
        reduceOnly (хедж-режим)."""

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
            assert params["quantity"] == "40"
            assert "reduceOnly" not in params
            return ok({"order": {"orderId": 2100000000000000009, "status": "NEW"}})

        client = make_client(handler)
        result = await client.place_conditional_order(
            symbol="BTC-USDT", side=OrderSide.SELL, position_side="LONG",
            order_type="STOP_MARKET", stop_price=D("84300.1"), quantity=D("40"),
            client_order_id="tj105u1S",
        )
        assert result.order_id == "2100000000000000009"
        await client.close()

    async def test_bridge_has_quantity_without_close_position(self) -> None:
        """Мост переноса стопа (этап 4, разведка A 02.10): ордер на объём —
        quantity есть, closePosition НЕ отправляется; второй closePosition-стоп
        BingX отклоняет 110406, стоп с quantity рядом принимает."""

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.url.query))
            assert "closePosition" not in params
            assert params["quantity"] == "40" and params["stopPrice"] == "1.49"
            assert params["workingType"] == CONDITIONAL_WORKING_TYPE
            assert params["clientOrderID"] == "tm12u1SB"
            assert "reduceOnly" not in params
            return ok({"order": {"orderId": 2106013507857768448, "status": "NEW",
                                 "closePosition": "", "reduceOnly": False}})

        client = make_client(handler)
        result = await client.place_conditional_order(
            symbol="XRP-USDT", side=OrderSide.SELL, position_side="LONG",
            order_type="STOP_MARKET", stop_price=D("1.49"), quantity=D("40"),
            client_order_id="tm12u1SB", close_position=False,
        )
        assert result.order_id == "2106013507857768448"
        await client.close()

    async def test_rejects_non_conditional_type(self) -> None:
        client = make_client(lambda r: ok({}))
        with pytest.raises(ValueError, match="Не условный"):
            await client.place_conditional_order(
                symbol="BTC-USDT", side=OrderSide.SELL, position_side="LONG",
                order_type="MARKET", stop_price=D("1"), quantity=D("1"),
                client_order_id="tj1u1S",
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
                order_type="TAKE_PROFIT_MARKET", stop_price=D("80000"), quantity=D("0.5"),
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
    datetime, нет или мусор → None, без ReadbackIncomplete. Живьём (GET #37)
    updateTime — мс исполнения, time — целые секунды постановки."""

    def test_live_update_time_parsed(self) -> None:
        fill = BingXClient._parse_order_fill(_live_entry())
        assert fill.filled_at == datetime(2026, 9, 27, 8, 15, 32, 828000, tzinfo=UTC)

    def test_falls_back_to_time(self) -> None:
        """СИНТЕТИКА ИЗ ЖИВОГО #37, убран updateTime."""
        order = {k: v for k, v in _live_entry().items() if k != "updateTime"}
        assert BingXClient._parse_order_fill(order).filled_at == datetime(
            2026, 9, 27, 8, 15, 32, tzinfo=UTC
        )

    @pytest.mark.parametrize("value", [None, "", 0, "not-a-number"])
    def test_missing_or_garbage_is_none_not_error(self, value: object) -> None:
        """СИНТЕТИКА ИЗ ЖИВОГО #37: убран time, updateTime нет или мусор."""
        order = {k: v for k, v in _live_entry().items() if k not in ("time", "updateTime")}
        if value is not None:
            order["updateTime"] = value
        assert BingXClient._parse_order_fill(order).filled_at is None


def test_fill_carries_order_id_for_import_dedupe() -> None:
    """Шаг 15.5.4: orderId исполнения — по нему импорт узнаёт ордера бота.
    Живой allFillOrders SOL (демо 27.09): orderId — строка; у выхода по стопу
    это дочерний ордер, связь с условником — triggerOrderId. Исполнение без
    orderId — TestLiveFillsForm.test_fill_without_any_id_is_none_with_warning."""
    from tests.bingx_fixtures import live_items

    entry, stop_exit = (
        BingXClient._parse_fill(item) for item in live_items("allFillOrders SOL (get_fills)")
    )
    assert entry.order_id == "2104213344135159808"
    assert stop_exit.order_id == "2104219661398712320"


class TestLiveFillsForm:
    """Живая форма allFillOrders (демо, 27.09): время — ISO-строки
    (filledTm с Z, filledTime с +08:00), объём — volume, связь выхода по
    стопу с условником — triggerOrderId. Раньше разбор падал ValueError на
    ISO и давал объём 0 (ждал executedQty/qty)."""

    def _fills(self):  # type: ignore[no-untyped-def]
        from tests.bingx_fixtures import live_items

        return [
            BingXClient._parse_fill(item)
            for item in live_items("allFillOrders SOL (get_fills)")
        ]

    def test_entry_and_stop_exit_parsed(self) -> None:
        entry, stop_exit = self._fills()
        assert entry.executed_at == datetime(2026, 9, 27, 14, 15, 30, tzinfo=UTC)
        assert stop_exit.executed_at == datetime(2026, 9, 27, 14, 40, 36, tzinfo=UTC)
        assert (entry.is_entry, stop_exit.is_entry) == (True, False)
        assert (entry.price, stop_exit.price) == (D("123.021"), D("121.611"))
        # volume у биржи округлён (1362 при executedQty ордера 1362.07) —
        # поле биржи, точнее allFillOrders объём не отдаёт.
        assert entry.quantity == D("1362")
        assert (entry.fee, stop_exit.fee) == (D("83.781520"), D("82.821383"))
        assert entry.order_id == "2104213344135159808"
        assert stop_exit.order_id == "2104219661398712320"

    def test_trigger_order_id_links_exit_to_conditional(self) -> None:
        entry, stop_exit = self._fills()
        assert entry.trigger_order_id is None  # 0 у биржи — «не условный»
        assert stop_exit.trigger_order_id == "2104213344721920001"

    def test_live_external_id_is_order_id(self) -> None:
        """tradeId в живом allFillOrders нет — ключ дедупа импорта orderId."""
        entry, stop_exit = self._fills()
        assert entry.external_id == "2104213344135159808"
        assert stop_exit.external_id == "2104219661398712320"

    def test_fill_without_any_id_is_none_with_warning(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """п.7, 29.09, СИНТЕТИКА ИЗ ЖИВОГО (allFillOrders SOL, убран orderId):
        нет ни tradeId, ни orderId — external_id None и WARNING, а не "":
        пустая строка — не ключ дедупа."""
        from tests.bingx_fixtures import live_items

        raw = live_items("allFillOrders SOL (get_fills)")[0]
        raw = {k: v for k, v in raw.items() if k != "orderId"}
        with caplog.at_level(logging.WARNING, logger="app.exchanges.bingx"):
            fill = BingXClient._parse_fill(raw)
        assert fill.external_id is None
        assert fill.order_id is None
        assert "без идентификатора" in caplog.text

    def test_missing_time_is_error_not_now(self) -> None:
        from app.exchanges.base import ExchangeResponseError

        item = {"symbol": "SOL-USDT", "orderId": "1", "side": "BUY", "positionSide": "LONG",
                "price": "1", "volume": "1", "commission": "0"}
        with pytest.raises(ExchangeResponseError):
            BingXClient._parse_fill(item)


# ---------------------------------------------------------------------------
# Шаг 15.6: история ордеров и позиций — живые ответы демо 27.09 (фикстура).
# ---------------------------------------------------------------------------


class TestReconcilerHistoryEndpoints:
    async def test_all_orders_sol_shows_stop_child_and_cancelled_tp(self) -> None:
        from tests.bingx_fixtures import live_items

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/openApi/swap/v2/trade/allOrders"
            return ok({"orders": live_items("allOrders SOL")})

        client = make_client(handler)
        orders = await client.get_all_orders(
            "SOL-USDT", datetime(2026, 9, 26, tzinfo=UTC), datetime(2026, 9, 27, 18, tzinfo=UTC)
        )
        by_type = {o.order_type: o for o in orders}
        stop = by_type["STOP_MARKET"]
        assert stop.status == "FILLED"
        assert stop.order_id == "2104219661398712320"
        assert stop.trigger_order_id == "2104213344721920001"
        assert stop.reduce_only is True
        assert stop.avg_price == D("121.611")
        assert stop.executed_qty == D("1362.07")
        assert stop.fee == D("82.821383")
        assert stop.realized_pnl == D("-1920.2749")
        assert stop.position_id == "2104213344168714242"
        assert stop.updated_at == datetime(2026, 9, 27, 14, 40, 36, tzinfo=UTC)
        assert by_type["TAKE_PROFIT_MARKET"].status == "CANCELLED"
        entry = by_type["MARKET"]
        assert (entry.status, entry.reduce_only, entry.trigger_order_id) == ("FILLED", False, None)
        await client.close()

    async def test_positions_carry_position_id(self) -> None:
        from tests.bingx_fixtures import live_items

        client = make_client(lambda request: ok(live_items("positions all")))
        [link] = await client.get_positions()
        assert link.symbol == "LINK-USDT"
        assert link.position_id == "2104122757805514754"
        await client.close()

    async def test_history_window_over_7_days_refused_before_http(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("запроса быть не должно")

        client = make_client(handler)
        with pytest.raises(ValueError):
            await client.get_all_orders(
                "SOL-USDT", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            )
        await client.close()


# Разведка 29.09: None/"" в числе ответа — ошибка разбора, не 0. Синтетика из
# живого: живой элемент, одно поле убрано ("поля нет") или пусто ("").
_EMPTY_MODES = [pytest.param("del", id="поля нет"), pytest.param("", id="пустая строка")]
_SOL_WINDOW = (datetime(2026, 9, 26, tzinfo=UTC), datetime(2026, 9, 27, 18, tzinfo=UTC))


def _blank(item: dict[str, object], field: str, mode: str) -> dict[str, object]:
    if mode == "del":
        del item[field]
    else:
        item[field] = ""
    return item


class TestStrictHistoryOrders:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize("field", ["avgPrice", "executedQty", "commission", "profit"])
    async def test_filled_exit_without_number_is_error(self, field: str, mode: str) -> None:
        """Исполненный стоп-выход SOL #4: цифры выхода идут в журнал — пустая
        цифра не становится 0 (цена/комиссия/profit выхода)."""
        orders = live_items("allOrders SOL")
        [child] = [o for o in orders if str(o["orderId"]) == "2104219661398712320"]
        assert child["status"] == "FILLED"
        _blank(child, field, mode)
        client = make_client(lambda r: ok({"orders": orders}))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_all_orders("SOL-USDT", *_SOL_WINDOW)
        await client.close()

    async def test_lock_not_filled_empty_commission_and_profit_is_lawful(self) -> None:
        """Замок: у NEW commission и profit живьём "" (openOrders LINK 27.09 —
        та же форма ордера, что в allOrders). Законная пустота: цифрами
        выхода не становятся, сверка берёт только FILLED."""
        [stop, _take] = live_items("openOrders LINK")
        assert (stop["status"], stop["commission"], stop["profit"]) == ("NEW", "", "")
        client = make_client(lambda r: ok({"orders": [stop]}))
        [order] = await client.get_all_orders("LINK-USDT", *_SOL_WINDOW)
        assert (order.status, order.fee, order.realized_pnl) == ("NEW", D(0), D(0))
        await client.close()


class TestStrictOpenOrders:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize(
        "field",
        [
            "orderId", "symbol", "side", "positionSide", "type", "status",
            "origQty", "executedQty", "price", "leverage", "time", "updateTime",
        ],
    )
    async def test_field_is_required(self, field: str, mode: str) -> None:
        """По этим полям read-back ищет свой стоп, сверка — защиту позиции,
        экран ордеров показывает объём/цену/плечо. Раньше time → 1970,
        строки → "", числа → 0: свой стоп не находился, read-back ставил
        второй. Живой стоп …705 openOrders LINK."""
        stop, take = live_items("openOrders LINK")
        _blank(stop, field, mode)
        client = make_client(lambda r: ok({"orders": [stop, take]}))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_open_orders("LINK-USDT")
        await client.close()

    async def test_attached_stub_without_stop_price_is_error(self) -> None:
        """Заглушка takeProfit живьём всегда со stopPrice (число 0). Нет поля —
        ошибка, а не «условник не задан»."""
        stop, take = live_items("openOrders LINK")
        stub = stop["takeProfit"]
        assert isinstance(stub, dict) and stub["stopPrice"] == 0
        del stub["stopPrice"]
        client = make_client(lambda r: ok({"orders": [stop, take]}))
        with pytest.raises(ExchangeResponseError, match="stopPrice"):
            await client.get_open_orders("LINK-USDT")
        await client.close()


def _live_balance_entry() -> dict[str, object]:
    """Набор полей — живой дамп боевого ключа (test_balance_list_of_assets_shape),
    суммы ненулевые (синтетика), чтобы подмена нулём была видна."""
    return {
        "userId": "1314404133518147588", "asset": "USDT",
        "balance": "500.0000", "equity": "512.5000",
        "unrealizedProfit": "12.5000", "realizedProfit": "0",
        "availableMargin": "400.0000", "usedMargin": "100.0000",
        "frozenMargin": "0.0000", "shortUid": "21792211",
    }


class TestStrictBalance:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize("field", ["equity", "usedMargin", "unrealizedProfit"])
    async def test_number_is_required(self, field: str, mode: str) -> None:
        """equity — база риска, дневного лимита и % импорта; usedMargin —
        проверка свободной маржи. Раньше нет поля → 0: вход отказывал
        INVALID_LEVELS с неверной причиной, импорт писал баланс 0 в журнал."""
        entry = _blank(_live_balance_entry(), field, mode)
        client = make_client(lambda r: ok([entry]))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_balance()
        await client.close()


class TestStrictPositions:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize("field", ["avgPrice", "markPrice", "unrealizedProfit", "leverage"])
    async def test_field_is_required(self, field: str, mode: str) -> None:
        """Живая позиция LINK (positions all, 27.09). Раньше цена/PnL → 0,
        плечо → 1 на экране «Позиции на бирже»."""
        [item] = live_items("positions all")
        _blank(item, field, mode)
        client = make_client(lambda r: ok([item]))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_positions()
        await client.close()


class TestStrictFills:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize(
        "field", ["price", "volume", "commission", "positionSide", "side", "symbol"]
    )
    async def test_field_is_required(self, field: str, mode: str) -> None:
        """Живой allFillOrders SOL (27.09): цена, объём (живьём — volume),
        комиссия, стороны и символ идут в импортированную сделку. Раньше
        0 / LONG / BUY / "" — сделка в журнале с нулями или не той стороной."""
        entry, exit_fill = live_items("allFillOrders SOL (get_fills)")
        _blank(exit_fill, field, mode)
        client = make_client(lambda r: ok({"fill_orders": [entry, exit_fill]}))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_fills(*_SOL_WINDOW)
        await client.close()

    async def test_unknown_position_side_is_error(self) -> None:
        """Раньше всё, что не LONG, считалось SHORT."""
        entry, exit_fill = live_items("allFillOrders SOL (get_fills)")
        exit_fill["positionSide"] = "BOTH"
        client = make_client(lambda r: ok({"fill_orders": [entry, exit_fill]}))
        with pytest.raises(ExchangeResponseError, match="positionSide"):
            await client.get_fills(*_SOL_WINDOW)
        await client.close()


class TestStrictPublicData:
    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    async def test_ticker_last_price_is_required(self, mode: str) -> None:
        """Раньше цена 0: карточка отказывала дрейфом 100%, экран цен — 0."""
        item = _blank(live_items("ticker BTC (live)", PUBLIC)[0], "lastPrice", mode)
        client = make_client(lambda r: ok(item))
        with pytest.raises(ExchangeResponseError, match="lastPrice"):
            await client.get_ticker("BTC-USDT")
        await client.close()

    async def test_ticker_empty_list_is_error(self) -> None:
        client = make_client(lambda r: ok([]))
        with pytest.raises(ExchangeResponseError, match="ticker"):
            await client.get_ticker("BTC-USDT")
        await client.close()

    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume", "time"])
    async def test_kline_field_is_required(self, field: str, mode: str) -> None:
        """Живые klines v3. Раньше свеча с low=0 (ложные уровни, ATR, «стоп»
        в отчёте исходов) или временем 1970."""
        items = live_items("klines BTC 1h (live)", PUBLIC)
        _blank(items[1], field, mode)
        client = make_client(lambda r: ok(items))
        with pytest.raises(ExchangeResponseError, match=field):
            await client.get_klines("BTC-USDT", "1h")
        await client.close()

    @pytest.mark.parametrize("mode", _EMPTY_MODES)
    @pytest.mark.parametrize(
        "field",
        ["pricePrecision", "quantityPrecision", "tradeMinQuantity", "tradeMinUSDT",
         "status", "symbol"],
    )
    async def test_broken_contract_is_dropped_with_warning(
        self, field: str, mode: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Живые контракты. Раньше точности по умолчанию 2/4, минимумы 0
        (SIZE_TOO_SMALL выключен), status нет — «активен», symbol "".
        Теперь битый контракт выпадает с WARNING, остальные живут."""
        items = live_items("contracts (live)", PUBLIC)
        [btc] = [i for i in items if i["symbol"] == "BTC-USDT"]
        _blank(btc, field, mode)
        client = make_client(lambda r: ok(items))
        with caplog.at_level(logging.WARNING, logger="app.exchanges.bingx"):
            symbols = await client.get_symbols()
        assert {s.symbol for s in symbols} == {"ETH-USDT", "LINK-USDT", "SOL-USDT"}
        assert any("Контракты без обязательных полей" in r.message for r in caplog.records)
        await client.close()

    async def test_lock_inactive_contract_skipped_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Замок: status 25 (живьём есть в списке) — неактивный, пропуск без
        WARNING."""
        items = live_items("contracts (live)", PUBLIC)
        [btc] = [i for i in items if i["symbol"] == "BTC-USDT"]
        btc["status"] = 25
        client = make_client(lambda r: ok(items))
        with caplog.at_level(logging.WARNING, logger="app.exchanges.bingx"):
            symbols = await client.get_symbols()
        assert "BTC-USDT" not in {s.symbol for s in symbols}
        assert not caplog.records
        await client.close()



class TestCancelOrder:
    """DELETE /openApi/swap/v2/trade/order — живые ответы 02.10 (разведка 0в)."""

    async def test_cancel_closeposition_order_live_form(self) -> None:
        """Ответ на отмену closePosition-ордера живьём: type LIMIT, пустой
        stopPrice — разбирается, но доказательством не служит."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            params = dict(httpx.QueryParams(request.url.query))
            assert params["symbol"] == "XRP-USDT"
            assert params["orderId"] == "2105907857156825088"
            return ok({"order": {
                "orderId": 2105907857156825088, "symbol": "XRP-USDT", "side": "SELL",
                "positionSide": "LONG", "type": "LIMIT", "status": "CANCELLED",
                "stopPrice": "", "price": "0.0000", "origQty": "40", "executedQty": "0",
                "workingType": "", "closePosition": "", "reduceOnly": False,
                "clientOrderId": "recon0va",
            }})

        client = make_client(handler)
        result = await client.cancel_order("XRP-USDT", "2105907857156825088")
        assert result.order_id == "2105907857156825088"   # число > 2^53 — строкой без потерь
        assert result.status == "CANCELLED"
        await client.close()

    async def test_repeat_cancel_is_order_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 109400, "msg": "order not exist", "data": {}})

        client = make_client(handler)
        with pytest.raises(OrderNotFoundError):
            await client.cancel_order("XRP-USDT", "2105898709895700480")
        await client.close()

    async def test_same_code_other_text_is_plain_error(self) -> None:
        """109400 общий: «parameter quantity or stopPrice is must» — не «нет ордера»."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 109400, "msg": "parameter quantity or stopPrice is must", "data": {},
            })

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError) as caught:
            await client.cancel_order("XRP-USDT", "1")
        assert not isinstance(caught.value, OrderNotFoundError)
        assert caught.value.code == 109400
        await client.close()

    async def test_cancel_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.cancel_order("XRP-USDT", "1")
        assert calls["n"] == 1
        await client.close()


def _ticking_clock(step: float):  # type: ignore[no-untyped-def]
    """time.time, который на каждом вызове уходит вперёд на step секунд."""
    state = {"t": 1_791_000_000.0}

    def now() -> float:
        state["t"] += step
        return state["t"]

    return now


class TestRetrySigningAndNoOrderRepeat:
    """08.10.2026: подпись и timestamp — заново на каждую попытку (прод, 2×
    «109400 timestamp is invalid» после таймаута позиций); повторяет клиент
    только GET — POST/DELETE ровно один раз при любом max_retries."""

    @staticmethod
    def _signed_ok(client: BingXClient, request: httpx.Request) -> bool:
        query = unquote(request.url.query.decode())
        raw, _, signature = query.rpartition("&signature=")
        return client._sign(raw) == signature

    async def test_get_retry_after_timeout_is_resigned(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr("app.exchanges.bingx.time.time", _ticking_clock(12))
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) == 1:
                raise httpx.TimeoutException("timeout")
            return ok([])

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        assert await client.get_positions() == []
        assert len(seen) == 2
        stamps = [int(dict(httpx.QueryParams(r.url.query))["timestamp"]) for r in seen]
        assert stamps[1] - stamps[0] >= 12_000   # свежий timestamp, не старый
        sigs = [dict(httpx.QueryParams(r.url.query))["signature"] for r in seen]
        assert sigs[0] != sigs[1]
        assert all(self._signed_ok(client, r) for r in seen)
        await client.close()

    async def test_get_retry_after_5xx_is_resigned(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr("app.exchanges.bingx.time.time", _ticking_clock(2))
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(502) if len(seen) == 1 else ok({"orders": []})

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        assert await client.get_open_orders("XRP-USDT") == []
        stamps = [dict(httpx.QueryParams(r.url.query))["timestamp"] for r in seen]
        assert len(set(stamps)) == 2
        await client.close()

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("market", lambda c: c.place_market_order(
                symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
                quantity=D("10"), client_order_id="to1u1e",
                stop_loss=TpSlSpec(trigger_price=D("1.4")))),
            ("limit", lambda c: c.place_limit_order(
                symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
                quantity=D("10"), price=D("1.4"), client_order_id="to2u1e")),
            ("conditional", lambda c: c.place_conditional_order(
                symbol="XRP-USDT", side=OrderSide.SELL, position_side="LONG",
                order_type="STOP_MARKET", stop_price=D("1.4"), quantity=D("10"),
                client_order_id="to3u1s1")),
            ("close", lambda c: c.place_market_order(
                symbol="XRP-USDT", side=OrderSide.SELL, position_side="LONG",
                quantity=D("10"), client_order_id="to4u1c1")),
            ("cancel", lambda c: c.cancel_order_by_client_id("XRP-USDT", "to5u1e")),
            ("leverage", lambda c: c.set_leverage("XRP-USDT", 5, position_side="LONG")),
        ],
    )
    @pytest.mark.parametrize("failure", ["timeout", "502", "rate_limit"])
    async def test_order_post_is_never_repeated(self, name, call, failure) -> None:  # type: ignore[no-untyped-def]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if failure == "timeout":
                raise httpx.TimeoutException("timeout")
            if failure == "502":
                return httpx.Response(502)
            return httpx.Response(200, json={"code": 100410, "msg": "rate limit", "data": {}})

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises((ExchangeUnavailableError, ExchangeRateLimitError)):
            await call(client)
        assert calls["n"] == 1, name
        await client.close()

    async def test_post_ignores_explicit_max_retries(self) -> None:
        """Даже явный max_retries=3 в _request не повторяет POST — правило в
        транспорте, а не на совести каждого метода."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client._request(
                "/openApi/swap/v2/trade/order", {"symbol": "XRP-USDT"}, signed=True,
                method="POST", max_retries=3,
            )
        assert calls["n"] == 1
        await client.close()
