"""Клиент BingX для открытия сделки из бота — живые ответы разведки Р1–Р8
(демо, XRP-USDT, 05.10.2026 15:44–15:50 UTC, docs/handoff.md). Сеть не
используется: ответы — копии живых, сокращённые до полей, которые читает код."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.exchanges.base import (
    ExchangeResponseError,
    ExchangeUnavailableError,
    MarginType,
    OrderNotFoundError,
    TpSlSpec,
)
from app.exchanges.bingx import BingXClient
from app.trading.enums import OrderSide
from tests.test_bingx_client import make_client, ok

D = Decimal


async def _noop() -> None:
    return None


def _params(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.url.query))


# Р3: GET входа по clientOrderId — маркет FILLED (сокращено до читаемых полей).
R3_ENTRY_FILLED = {
    "symbol": "XRP-USDT", "orderId": 2107135113934413824, "side": "BUY",
    "positionSide": "LONG", "type": "MARKET", "origQty": "10", "price": "1.4952",
    "executedQty": "10", "avgPrice": "1.4952", "cumQuote": "15", "stopPrice": "",
    "profit": "0.0000", "commission": "-0.007476", "status": "FILLED",
    "time": 1791215134000, "updateTime": 1791215134503,
    "clientOrderId": "rcnr3x1791215134", "leverage": "20X",
    "positionID": 2107135113963773954, "workingType": "MARK_PRICE",
    "reduceOnly": False, "triggerOrderId": 0, "closePosition": "false",
}

# Р3: вложенный стоп в openOrders — отдельный ордер, clientOrderId пустой,
# связь со входом — positionID.
R3_ATTACHED_STOP = {
    "symbol": "XRP-USDT", "orderId": 2107135114386956289, "side": "SELL",
    "positionSide": "LONG", "type": "STOP_MARKET", "origQty": "10", "price": "0.0000",
    "executedQty": "0", "avgPrice": "1.4952", "cumQuote": "0", "stopPrice": "1.4501",
    "profit": "", "commission": "", "status": "NEW", "time": 1791215134604,
    "updateTime": 1791215134604, "clientOrderId": "", "leverage": "20X",
    "takeProfit": {"type": "", "quantity": 0, "stopPrice": 0, "price": 0, "workingType": ""},
    "stopLoss": {"type": "", "quantity": 0, "stopPrice": 0, "price": 0, "workingType": ""},
    "positionID": 2107135113963773954, "workingType": "MARK_PRICE",
    "reduceOnly": True, "triggerOrderId": 0, "closePosition": "false",
}

# Р5: лимит до исполнения в openOrders — PENDING, positionID 0, SL/TP внутри.
R5_LIMIT_PENDING = {
    "symbol": "XRP-USDT", "orderId": 2107135436539305984, "side": "BUY",
    "positionSide": "LONG", "type": "LIMIT", "origQty": "10", "price": "1.4193",
    "executedQty": "0", "avgPrice": "0.0000", "cumQuote": "0", "stopPrice": "",
    "profit": "0.0", "commission": "0.0", "status": "PENDING",
    "time": 1791215211000, "updateTime": 1791215211419,
    "clientOrderId": "rcnr5x1791215211", "leverage": "20X",
    "takeProfit": {"type": "TAKE_PROFIT_MARKET", "quantity": 0, "stopPrice": 1.539,
                   "price": 0, "workingType": ""},
    "stopLoss": {"type": "STOP_MARKET", "quantity": 0, "stopPrice": 1.3767,
                 "price": 0, "workingType": ""},
    "positionID": 0, "workingType": "CONTRACT_PRICE", "reduceOnly": False,
    "triggerOrderId": 0, "closePosition": "false",
}


class TestSetMarginType:
    async def test_post_and_live_answer(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/openApi/swap/v2/trade/marginType"
            params = _params(request)
            assert params["symbol"] == "XRP-USDT"
            assert params["marginType"] == "CROSSED"
            return ok({"symbol": "XRP-USDT", "marginType": "CROSSED"})

        client = make_client(handler)
        assert await client.set_margin_type("XRP-USDT", MarginType.CROSSED) is MarginType.CROSSED
        await client.close()

    async def test_open_position_refusal_keeps_code(self) -> None:
        """Р3а: при открытой позиции — 104103, режим не меняется."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 104103,
                "msg": "Please close open positions or cancel pending orders first.",
                "data": {},
            })

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError) as caught:
            await client.set_margin_type("XRP-USDT", MarginType.CROSSED)
        assert caught.value.code == 104103
        await client.close()

    async def test_unknown_answer_is_error(self) -> None:
        client = make_client(lambda r: ok({"symbol": "XRP-USDT"}))
        with pytest.raises(ExchangeResponseError):
            await client.set_margin_type("XRP-USDT", MarginType.ISOLATED)
        await client.close()

    async def test_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.set_margin_type("XRP-USDT", MarginType.ISOLATED)
        assert calls["n"] == 1
        await client.close()


class TestPlaceLimitOrder:
    async def test_params_and_pending_answer(self) -> None:
        """Р5: лимит GTC с вложенными SL/TP; ответ POST — status PENDING."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            params = _params(request)
            assert params["type"] == "LIMIT"
            assert params["price"] == "1.4193"
            assert params["quantity"] == "10"
            assert params["timeInForce"] == "GTC"
            assert params["side"] == "BUY"
            assert params["positionSide"] == "LONG"
            assert params["clientOrderID"] == "to7u1e"
            assert params["stopLoss"] == (
                '{"type":"STOP_MARKET","stopPrice":1.3767,"workingType":"MARK_PRICE"}'
            )
            assert params["takeProfit"] == (
                '{"type":"TAKE_PROFIT_MARKET","stopPrice":1.5390,"workingType":"MARK_PRICE"}'
            )
            return ok({"order": {
                "orderId": 2107135436539305984, "orderID": "2107135436539305984",
                "symbol": "XRP-USDT", "positionSide": "LONG", "side": "BUY", "type": "LIMIT",
                "price": 1.4193, "quantity": 10, "clientOrderID": "to7u1e", "clientOrderId": "",
                "timeInForce": "GTC", "status": "PENDING", "avgPrice": "0.0000",
                "executedQty": "0",
            }})

        client = make_client(handler)
        result = await client.place_limit_order(
            symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
            quantity=D("10"), price=D("1.4193"), client_order_id="to7u1e",
            take_profit=TpSlSpec(trigger_price=D("1.5390")),
            stop_loss=TpSlSpec(trigger_price=D("1.3767")),
        )
        assert result.order_id == "2107135436539305984"
        assert result.status == "PENDING"
        assert result.order_type == "LIMIT"
        await client.close()

    async def test_without_take_profit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = _params(request)
            assert "takeProfit" not in params
            assert "stopLoss" in params
            return ok({"order": {"orderId": 1, "status": "PENDING", "type": "LIMIT"}})

        client = make_client(handler)
        await client.place_limit_order(
            symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
            quantity=D("10"), price=D("1.4"), client_order_id="to8u1e",
            stop_loss=TpSlSpec(trigger_price=D("1.3")),
        )
        await client.close()

    async def test_wrong_side_stop_rejected_whole(self) -> None:
        """Р4: неверный вложенный стоп — отказ всего ордера кодом 101400."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 101400, "msg": "SL Price must be lower than Last Price", "data": {},
            })

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError) as caught:
            await client.place_limit_order(
                symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
                quantity=D("10"), price=D("1.5"), client_order_id="to9u1e",
                stop_loss=TpSlSpec(trigger_price=D("1.6")),
            )
        assert caught.value.code == 101400
        await client.close()

    @pytest.mark.parametrize(
        ("cid", "price"), [("", D("1")), ("x" * 41, D("1")), ("ok", D("0")), ("ok", D("-1"))]
    )
    async def test_validation_before_http(self, cid: str, price: Decimal) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("запрос не должен уйти")

        client = make_client(handler)
        with pytest.raises(ValueError):
            await client.place_limit_order(
                symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
                quantity=D("10"), price=price, client_order_id=cid,
            )
        await client.close()

    async def test_does_not_retry(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        client = make_client(handler, max_retries=3)
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        with pytest.raises(ExchangeUnavailableError):
            await client.place_limit_order(
                symbol="XRP-USDT", side=OrderSide.BUY, position_side="LONG",
                quantity=D("10"), price=D("1.4"), client_order_id="to10u1e",
            )
        assert calls["n"] == 1
        await client.close()


class TestCancelByClientId:
    async def test_live_form(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            params = _params(request)
            assert params == {**params, "symbol": "XRP-USDT", "clientOrderId": "rcnr5x1791215211"}
            assert "orderId" not in params
            return ok({"order": {
                "symbol": "XRP-USDT", "orderId": 2107135436539305984, "side": "BUY",
                "positionSide": "LONG", "type": "LIMIT", "origQty": "10", "price": "1.4193",
                "executedQty": "0", "status": "CANCELLED",
                "clientOrderId": "rcnr5x1791215211",
            }})

        client = make_client(handler)
        result = await client.cancel_order_by_client_id("XRP-USDT", "rcnr5x1791215211")
        assert result.order_id == "2107135436539305984"
        assert result.status == "CANCELLED"
        await client.close()

    async def test_not_exist(self) -> None:
        client = make_client(lambda r: httpx.Response(
            200, json={"code": 109400, "msg": "order not exist", "data": {}}
        ))
        with pytest.raises(OrderNotFoundError):
            await client.cancel_order_by_client_id("XRP-USDT", "to1u1e")
        await client.close()

    async def test_cancel_by_order_id_unchanged(self) -> None:
        """Рефакторинг общего _cancel не меняет отмену по orderId."""

        def handler(request: httpx.Request) -> httpx.Response:
            params = _params(request)
            assert params["orderId"] == "42"
            assert "clientOrderId" not in params
            return ok({"order": {"status": "CANCELLED"}})

        client = make_client(handler)
        result = await client.cancel_order("XRP-USDT", "42")
        assert result.order_id == "42"
        await client.close()


class TestCommissionRate:
    async def test_live_form(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/openApi/swap/v2/user/commissionRate"
            return ok({"commission": {
                "takerCommissionRate": 0.0005, "makerCommissionRate": 0.0002,
            }})

        client = make_client(handler)
        rate = await client.get_commission_rate()
        assert rate.taker == D("0.0005")
        assert rate.maker == D("0.0002")
        await client.close()

    @pytest.mark.parametrize(
        "payload",
        [{}, {"commission": None}, {"commission": {"makerCommissionRate": 0.0002}}],
    )
    async def test_missing_is_error(self, payload: dict[str, object]) -> None:
        client = make_client(lambda r: ok(payload))
        with pytest.raises(ExchangeResponseError):
            await client.get_commission_rate()
        await client.close()


class TestPositionIdParsing:
    def test_entry_fill_carries_position_id(self) -> None:
        fill = BingXClient._parse_order_fill(R3_ENTRY_FILLED)
        assert fill.status == "FILLED"
        assert fill.avg_price == D("1.4952")
        assert fill.executed_qty == D("10")
        assert fill.fee == D("0.007476")
        assert fill.position_id == "2107135113963773954"   # > 2^53 — строкой без потерь
        assert fill.client_order_id == "rcnr3x1791215134"

    def test_attached_stop_links_by_position_id(self) -> None:
        order = BingXClient._parse_open_order(R3_ATTACHED_STOP)
        assert order.order_type == "STOP_MARKET"
        assert order.client_order_id == ""
        assert order.position_id == "2107135113963773954"
        assert order.stop_price == D("1.4501")
        assert order.quantity == D("10")
        assert order.reduce_only is True
        assert order.close_position is False

    def test_pending_limit_has_no_position(self) -> None:
        order = BingXClient._parse_open_order(R5_LIMIT_PENDING)
        assert order.status == "PENDING"
        assert order.position_id is None
        assert order.stop_loss is not None and order.stop_loss.trigger_price == D("1.3767")
        assert order.take_profit is not None and order.take_profit.trigger_price == D("1.539")

    def test_pending_limit_fill_read_is_soft(self) -> None:
        """GET лимита до исполнения (Р5): avgPrice 0.0000, commission "0.000000"
        — не FILLED, нули законны, positionID 0 → None."""
        fill = BingXClient._parse_order_fill({
            **R5_LIMIT_PENDING, "commission": "0.000000",
        })
        assert fill.status == "PENDING"
        assert fill.executed_qty == 0
        assert fill.position_id is None
