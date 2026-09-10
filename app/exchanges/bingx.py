"""Клиент BingX (USDT-M перпетуалы).

Подпись: HMAC SHA256 по строке запроса, ключ в заголовке X-BX-APIKEY,
обязательные timestamp и recvWindow для защиты от повторного
воспроизведения запроса.

Важная деталь подписи: подписывается ровно та строка, которая уходит
на сервер. Если собрать подпись по одному порядку параметров, а
отправить в другом, биржа ответит ошибкой аутентификации — поэтому
строка формируется один раз и переиспользуется.

Пути эндпоинтов вынесены в константы: BingX меняет версии API, и при
расхождении достаточно поправить одно место, не трогая логику.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.logging import get_logger
from app.exchanges.base import (
    Balance,
    ExchangeAuthError,
    ExchangeClient,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    Fill,
    Kline,
    Position,
    SymbolInfo,
    Ticker,
)
from app.trading.enums import TradeSide

logger = get_logger(__name__)

# --- Эндпоинты -------------------------------------------------------------
QUOTE_CONTRACTS = "/openApi/swap/v2/quote/contracts"
QUOTE_TICKER = "/openApi/swap/v2/quote/ticker"
QUOTE_KLINES = "/openApi/swap/v3/quote/klines"
QUOTE_PREMIUM_INDEX = "/openApi/swap/v2/quote/premiumIndex"
USER_BALANCE = "/openApi/swap/v3/user/balance"
USER_POSITIONS = "/openApi/swap/v2/user/positions"
TRADE_FILL_HISTORY = "/openApi/swap/v2/trade/allFillOrders"

# Таймфреймы в обозначениях BingX.
INTERVALS = {
    "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

# Ошибки, при которых повтор запроса бессмыслен.
_FATAL_CODES = {100001, 100004, 100413, 100421}


def _to_decimal(value: Any, field: str) -> Decimal:
    """Числа биржи приходят строками — так и разбираем, минуя float."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExchangeResponseError(
            f"Не удалось разобрать поле {field}: {value!r}"
        ) from exc


def _ms_to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class BingXClient(ExchangeClient):
    name = "bingx"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = "https://open-api.bingx.com",
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._recv_window = recv_window
        self._max_retries = max_retries
        # Клиент можно передать снаружи — это точка подмены в тестах,
        # позволяющая проверить разбор ответов без обращения к сети.
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": "trading-journal-bot/1.0"},
        )

    # --- Подпись -----------------------------------------------------------

    def _sign(self, query: str) -> str:
        if not self._api_secret:
            raise ExchangeAuthError(
                "Не заданы ключи BingX. Подключи их в настройках бота."
            )
        return hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    def _build_signed_query(self, params: dict[str, Any]) -> str:
        """Собирает подписанную строку запроса.

        Порядок параметров фиксируется здесь и больше не меняется:
        подпись считается по той же строке, что уходит на сервер.
        """
        payload = {k: v for k, v in params.items() if v is not None}
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = self._recv_window

        query = urlencode(payload)
        return f"{query}&signature={self._sign(query)}"

    # --- Транспорт ---------------------------------------------------------

    async def _request(
        self, path: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> Any:
        params = params or {}

        if signed:
            if not self._api_key:
                raise ExchangeAuthError(
                    "Не заданы ключи BingX. Подключи их в настройках бота."
                )
            url = f"{path}?{self._build_signed_query(params)}"
            headers = {"X-BX-APIKEY": self._api_key}
        else:
            url = f"{path}?{urlencode(params)}" if params else path
            headers = {}

        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = ExchangeUnavailableError(
                    "BingX не ответил вовремя"
                )
                logger.warning(
                    "Таймаут запроса к BingX",
                    extra={"path": path, "attempt": attempt},
                )
            except httpx.HTTPError as exc:
                last_error = ExchangeUnavailableError(
                    f"Не удалось связаться с BingX: {type(exc).__name__}"
                )
                logger.warning(
                    "Ошибка соединения с BingX",
                    extra={"path": path, "attempt": attempt},
                )
            else:
                try:
                    return self._parse(response, path)
                except ExchangeRateLimitError as exc:
                    last_error = exc
                    logger.warning(
                        "Лимит запросов BingX",
                        extra={"path": path, "retry_after": exc.retry_after},
                    )
                except ExchangeUnavailableError as exc:
                    last_error = exc
                    logger.warning(
                        "BingX временно недоступен", extra={"path": path}
                    )

            if attempt < self._max_retries:
                # Экспоненциальная пауза: при перегрузке биржи частые
                # повторы только усугубляют ситуацию.
                delay = self._backoff(last_error, attempt)
                await self._sleep(delay)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _backoff(error: Exception | None, attempt: int) -> float:
        if isinstance(error, ExchangeRateLimitError):
            return max(error.retry_after, 2.0**attempt)
        return min(2.0**attempt, 8.0)

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def _parse(self, response: httpx.Response, path: str) -> Any:
        """Разбирает ответ, приводя ошибки биржи к понятным исключениям."""
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 1))
            raise ExchangeRateLimitError("Превышен лимит запросов", retry_after)
        if response.status_code in (401, 403):
            raise ExchangeAuthError(
                "BingX отклонил ключи. Проверь, что ключ активен и "
                "привязан к IP этого сервера."
            )
        if response.status_code >= 500:
            raise ExchangeUnavailableError(
                f"BingX вернул {response.status_code}"
            )
        if response.status_code != 200:
            raise ExchangeResponseError(
                f"BingX вернул {response.status_code} на {path}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeResponseError("Ответ BingX не является JSON") from exc

        # У BingX код 0 означает успех; ошибки приходят с HTTP 200.
        code = payload.get("code", 0)
        if code not in (0, "0", None):
            message = payload.get("msg") or "без описания"
            code_int = int(code) if str(code).lstrip("-").isdigit() else 0
            if code_int in _FATAL_CODES:
                raise ExchangeAuthError(f"BingX: {message} (код {code})")
            if code_int == 100410:
                raise ExchangeRateLimitError(f"BingX: {message}", 2.0)
            raise ExchangeResponseError(f"BingX: {message} (код {code})")

        return payload.get("data", payload)

    async def close(self) -> None:
        await self._client.aclose()

    # --- Публичные данные --------------------------------------------------

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._request(QUOTE_TICKER, {"symbol": symbol})
        if isinstance(data, list):
            data = data[0] if data else {}

        return Ticker(
            symbol=data.get("symbol", symbol),
            last_price=_to_decimal(data.get("lastPrice"), "lastPrice"),
            volume_24h=_to_decimal(data.get("volume"), "volume"),
            price_change_percent=_to_decimal(
                data.get("priceChangePercent"), "priceChangePercent"
            ),
            timestamp=datetime.now(UTC),
        )

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        end_time: datetime | None = None,
    ) -> list[Kline]:
        if interval not in INTERVALS:
            raise ValueError(
                f"Таймфрейм {interval} не поддерживается. "
                f"Доступны: {', '.join(INTERVALS)}"
            )

        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": INTERVALS[interval],
            "limit": min(limit, 1000),
        }
        if end_time is not None:
            params["endTime"] = int(end_time.timestamp() * 1000)

        data = await self._request(QUOTE_KLINES, params)
        if not isinstance(data, list):
            raise ExchangeResponseError("Ожидался список свечей")

        candles = [self._parse_kline(item, interval) for item in data]
        # Биржа отдаёт свечи в обратном порядке; анализ ждёт хронологию.
        candles.sort(key=lambda c: c.open_time)
        return candles

    @staticmethod
    def _parse_kline(item: Any, interval: str) -> Kline:
        minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        duration_ms = minutes[interval] * 60_000

        if isinstance(item, dict):
            open_ms = int(item.get("time") or item.get("openTime") or 0)
            values = (
                item.get("open"), item.get("high"),
                item.get("low"), item.get("close"), item.get("volume"),
            )
            close_ms = int(item.get("closeTime") or open_ms + duration_ms)
        else:  # массив [time, open, high, low, close, volume]
            open_ms = int(item[0])
            values = (item[1], item[2], item[3], item[4], item[5])
            close_ms = open_ms + duration_ms

        return Kline(
            open_time=_ms_to_dt(open_ms),
            open=_to_decimal(values[0], "open"),
            high=_to_decimal(values[1], "high"),
            low=_to_decimal(values[2], "low"),
            close=_to_decimal(values[3], "close"),
            volume=_to_decimal(values[4], "volume"),
            close_time=_ms_to_dt(close_ms),
        )

    async def get_symbols(self) -> list[SymbolInfo]:
        data = await self._request(QUOTE_CONTRACTS)
        if not isinstance(data, list):
            raise ExchangeResponseError("Ожидался список контрактов")

        result = []
        for item in data:
            if item.get("status") not in (1, "1", None):
                continue  # неактивный контракт
            result.append(
                SymbolInfo(
                    symbol=item.get("symbol", ""),
                    price_precision=int(item.get("pricePrecision", 2)),
                    quantity_precision=int(item.get("quantityPrecision", 4)),
                    min_quantity=_to_decimal(
                        item.get("tradeMinQuantity"), "tradeMinQuantity"
                    ),
                    max_leverage=int(item.get("maxLongLeverage", 20) or 20),
                )
            )
        return result

    async def get_funding_rate(self, symbol: str) -> Decimal | None:
        data = await self._request(QUOTE_PREMIUM_INDEX, {"symbol": symbol})
        if isinstance(data, list):
            data = data[0] if data else {}
        rate = data.get("lastFundingRate")
        return _to_decimal(rate, "lastFundingRate") if rate is not None else None

    # --- Приватные данные --------------------------------------------------

    async def get_balance(self) -> Balance:
        data = await self._request(USER_BALANCE, signed=True)
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict) and "balance" in data:
            data = data["balance"]

        equity = _to_decimal(data.get("equity"), "equity")
        return Balance(
            asset=data.get("asset", "USDT"),
            available=_to_decimal(
                data.get("availableMargin") or data.get("balance"), "available"
            ),
            used_margin=_to_decimal(data.get("usedMargin"), "usedMargin"),
            unrealized_pnl=_to_decimal(
                data.get("unrealizedProfit"), "unrealizedProfit"
            ),
            equity=equity,
        )

    async def get_positions(self) -> list[Position]:
        data = await self._request(USER_POSITIONS, signed=True)
        if not isinstance(data, list):
            return []

        positions = []
        for item in data:
            quantity = _to_decimal(
                item.get("positionAmt") or item.get("availableAmt"), "positionAmt"
            )
            if quantity == 0:
                continue  # закрытые позиции биржа тоже возвращает

            positions.append(
                Position(
                    symbol=item.get("symbol", ""),
                    side=(
                        TradeSide.LONG
                        if str(item.get("positionSide", "LONG")).upper() == "LONG"
                        else TradeSide.SHORT
                    ),
                    quantity=abs(quantity),
                    entry_price=_to_decimal(item.get("avgPrice"), "avgPrice"),
                    mark_price=_to_decimal(item.get("markPrice"), "markPrice"),
                    leverage=int(item.get("leverage", 1) or 1),
                    unrealized_pnl=_to_decimal(
                        item.get("unrealizedProfit"), "unrealizedProfit"
                    ),
                    margin=_to_decimal(item.get("initialMargin"), "initialMargin"),
                    liquidation_price=(
                        _to_decimal(item.get("liquidationPrice"), "liquidationPrice")
                        if item.get("liquidationPrice")
                        else None
                    ),
                )
            )
        return positions

    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]:
        """Исполнения за период.

        Окно запроса у BingX ограничено, поэтому длинные периоды режутся
        на отрезки в вызывающем коде (см. HistoryImporter). Здесь —
        один запрос за переданный интервал.
        """
        params: dict[str, Any] = {
            "startTs": int(start_time.timestamp() * 1000),
            "endTs": int(end_time.timestamp() * 1000),
        }
        if symbol:
            params["symbol"] = symbol

        data = await self._request(TRADE_FILL_HISTORY, params, signed=True)
        if isinstance(data, dict):
            data = data.get("fill_orders") or data.get("orders") or []
        if not isinstance(data, list):
            return []

        return [self._parse_fill(item) for item in data if item]

    @staticmethod
    def _parse_fill(item: dict[str, Any]) -> Fill:
        position_side = str(item.get("positionSide", "LONG")).upper()
        side = TradeSide.LONG if position_side == "LONG" else TradeSide.SHORT

        # Направление ордера относительно позиции: BUY увеличивает лонг и
        # сокращает шорт. Именно это отличает вход от выхода, а не сам
        # по себе BUY/SELL.
        order_side = str(item.get("side", "BUY")).upper()
        is_entry = (side is TradeSide.LONG) == (order_side == "BUY")

        executed_at = item.get("filledTime") or item.get("time") or 0

        return Fill(
            external_id=str(
                item.get("tradeId") or item.get("orderId") or item.get("id") or ""
            ),
            symbol=item.get("symbol", ""),
            side=side,
            is_entry=is_entry,
            price=_to_decimal(
                item.get("avgPrice") or item.get("price"), "price"
            ),
            quantity=_to_decimal(
                item.get("executedQty") or item.get("qty"), "quantity"
            ),
            fee=abs(_to_decimal(item.get("commission") or item.get("fee"), "fee")),
            realized_pnl=_to_decimal(item.get("profit"), "profit"),
            executed_at=_ms_to_dt(executed_at) if executed_at else datetime.now(UTC),
            position_id=str(item.get("positionId")) if item.get("positionId") else None,
        )
