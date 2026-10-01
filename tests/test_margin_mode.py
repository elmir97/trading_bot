"""Режим маржи символа (28.09): разбор ответа BingX. Без БД.

Кэш и гвард изолированной маржи удалены вместе с входом по сигналу (02.10);
метод клиента остаётся."""

from __future__ import annotations

import httpx
import pytest

from app.exchanges.base import (
    ExchangeResponseError,
    MarginType,
)
from tests.test_bingx_client import make_client, ok


class TestBingXGetMarginType:
    """Живая форма ответа (28.09, демо): data = {"marginType", "symbol"}."""

    @pytest.mark.parametrize("value", ["ISOLATED", "CROSSED"])
    async def test_parses_value_from_data(self, value: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "/openApi/swap/v2/trade/marginType" in str(request.url)
            assert request.url.params["symbol"] == "LINK-USDT"
            return ok({"marginType": value, "symbol": "LINK-USDT"})

        client = make_client(handler)
        assert await client.get_margin_type("LINK-USDT") is MarginType(value)
        await client.close()

    @pytest.mark.parametrize(
        "payload", [{}, {"marginType": "isolated"}, {"marginType": None}, []]
    )
    async def test_unknown_or_missing_raises_not_default(self, payload: object) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok(payload)

        client = make_client(handler)
        with pytest.raises(ExchangeResponseError, match="marginType"):
            await client.get_margin_type("LINK-USDT")
        await client.close()
