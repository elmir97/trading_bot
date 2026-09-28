"""Режим маржи символа (28.09): разбор ответа BingX, кэш
app/services/margin_mode.py, гвард check_margin_isolated. Без БД."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    MarginType,
)
from app.execution.guards import check_margin_isolated
from app.execution.models import ExecutionRefusalCode
from app.market.cache import TTLCache
from app.services.margin_mode import refresh_margin_type
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


class FakeClient:
    name = "fake"

    def __init__(self, value: MarginType, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.symbols: list[str] = []

    async def get_margin_type(
        self, symbol: str, *, max_retries: int | None = None
    ) -> MarginType:
        self.symbols.append(symbol)
        if self.error is not None:
            raise self.error
        return self.value


async def _refresh(cache: TTLCache, client: FakeClient, symbol: str):  # type: ignore[no-untyped-def]
    return await refresh_margin_type(cache, client, 1, symbol, ttl_seconds=300)  # type: ignore[arg-type]


class TestRefreshMarginType:
    async def test_success_and_cache_hit(self) -> None:
        cache, client = TTLCache(), FakeClient(MarginType.ISOLATED)
        first = await _refresh(cache, client, "SOL-USDT")
        second = await _refresh(cache, client, "SOL-USDT")
        assert first.trustworthy and first.margin_type is MarginType.ISOLATED
        assert second.margin_type is MarginType.ISOLATED
        assert client.symbols == ["SOL-USDT"]

    async def test_symbol_is_part_of_key(self) -> None:
        """Режим маржи у BingX — по символу: SOL не отвечает за LINK."""
        cache, client = TTLCache(), FakeClient(MarginType.ISOLATED)
        await _refresh(cache, client, "SOL-USDT")
        await _refresh(cache, client, "LINK-USDT")
        assert client.symbols == ["SOL-USDT", "LINK-USDT"]

    @pytest.mark.parametrize(
        "error", [ExchangeUnavailableError("нет ответа"), ExchangeAuthError("подпись")]
    )
    async def test_failure_is_untrustworthy_and_not_cached(self, error: Exception) -> None:
        cache, client = TTLCache(), FakeClient(MarginType.ISOLATED, error=error)
        outcome = await _refresh(cache, client, "SOL-USDT")
        assert (outcome.trustworthy, outcome.margin_type, outcome.error) == (False, None, error)
        client.error = None
        again = await _refresh(cache, client, "SOL-USDT")
        assert again.margin_type is MarginType.ISOLATED
        assert client.symbols == ["SOL-USDT", "SOL-USDT"]


class TestGuard:
    def test_isolated_passes(self) -> None:
        assert check_margin_isolated(symbol="SOL-USDT", margin_type=MarginType.ISOLATED) is None

    def test_crossed_refused_with_owner_text(self) -> None:
        refusal = check_margin_isolated(symbol="SOL-USDT", margin_type=MarginType.CROSSED)
        assert refusal is not None
        assert refusal.code is ExecutionRefusalCode.MARGIN_NOT_ISOLATED
        assert refusal.message == "Маржа по SOL-USDT кросс — переключи на изолированную в BingX"

    def test_unknown_refused_separately(self) -> None:
        refusal = check_margin_isolated(symbol="SOL-USDT", margin_type=None)
        assert refusal is not None
        assert refusal.code is ExecutionRefusalCode.MARGIN_MODE_UNKNOWN


def test_ttl_setting_default() -> None:
    assert Settings().exec_margin_type_ttl_seconds == 300  # type: ignore[call-arg]
