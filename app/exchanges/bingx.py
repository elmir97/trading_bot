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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.core.logging import get_logger
from app.exchanges.base import (
    CONDITIONAL_WORKING_TYPE,
    ApiRestrictions,
    AttachedTpSl,
    Balance,
    ExchangeAuthError,
    ExchangeClient,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeUnavailableError,
    Fill,
    Kline,
    LeverageInfo,
    OpenOrder,
    OrderFill,
    OrderResult,
    Position,
    ReadbackIncomplete,
    SymbolInfo,
    Ticker,
    TpSlSpec,
)
from app.trading.enums import ExchangeKeyMode, OrderSide, OrderType, TradeSide

logger = get_logger(__name__)

# --- Эндпоинты -------------------------------------------------------------
QUOTE_CONTRACTS = "/openApi/swap/v2/quote/contracts"
QUOTE_TICKER = "/openApi/swap/v2/quote/ticker"
QUOTE_KLINES = "/openApi/swap/v3/quote/klines"
QUOTE_PREMIUM_INDEX = "/openApi/swap/v2/quote/premiumIndex"
USER_BALANCE = "/openApi/swap/v3/user/balance"
USER_POSITIONS = "/openApi/swap/v2/user/positions"
# Раздел 8 ТЗ: права ключа. Проверено живым запросом (не по документации —
# страница bingx-api.github.io отдаёт только SPA-шелл): поля лежат на
# ВЕРХНЕМ уровне JSON, рядом с code/msg, а не под data, как у остальных
# приватных методов ниже.
API_RESTRICTIONS = "/openApi/v1/account/apiRestrictions"
TRADE_FILL_HISTORY = "/openApi/swap/v2/trade/allFillOrders"
# Один и тот же путь: GET читает текущее/максимальное плечо по символу
# (раздел 16 ТЗ, шаг 15.5.1, проверено живым запросом на демо-хосте),
# POST выставляет.
TRADE_LEVERAGE = "/openApi/swap/v2/trade/leverage"
# Один и тот же путь: POST размещает ордер, GET — запрашивает его статус.
TRADE_ORDER = "/openApi/swap/v2/trade/order"
TRADE_OPEN_ORDERS = "/openApi/swap/v2/trade/openOrders"
# Раздел 16 ТЗ, шаг 15.5.1: путь v1, НЕ v2 — /openApi/swap/v2/trade/
# positionSide/dual отвечает code 100404 "this api is not exist"
# (проверено дважды живым запросом на демо-хосте, не по документации:
# страница bingx-api.github.io отдаёт только SPA-шелл). Версия отдельно
# от остальных путей этого файла, не переиспользуем v2 по аналогии.
POSITION_SIDE_DUAL = "/openApi/swap/v1/positionSide/dual"

# Валюта маржи зависит от контура: LIVE торгует настоящими USDT, DEMO —
# виртуальными VST (см. app/bot/handlers/settings.py:626). get_balance()
# должен искать в ответе биржи именно этот актив, а не всегда "USDT" —
# иначе на DEMO фильтр промахивается при каждом запросе.
_QUOTE_ASSET_BY_MODE = {
    ExchangeKeyMode.LIVE: "USDT",
    ExchangeKeyMode.DEMO: "VST",
}

# Таймфреймы в обозначениях BingX.
INTERVALS = {
    "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

# Ошибки, при которых повтор запроса бессмыслен.
_FATAL_CODES = {100001, 100004, 100413, 100421}

# Заголовки остатка лимита запросов. Проверено живым запросом на трёх
# приватных ручках и двух публичных (раздел "троттлинг сканера"): лимит
# отдельный на каждый ПУТЬ, не общий на ключ/IP — contracts сбрасывается
# в полный остаток сразу после того, как ticker его подъел, а у balance/
# positions/openOrders с одним и тем же ключом одновременно три разных
# потолка (40/10/5). httpx.Headers регистронезависим, точный кейс заголовка
# роли не играет. Заголовков может не быть вовсе (сторонний прокси, обрыв
# соединения) — это не значит "остаток 0", значит "неизвестно".
_HEADER_RATE_LIMIT_REMAIN = "X-RateLimit-Requests-Remain"
_HEADER_RATE_LIMIT_EXPIRE = "X-RateLimit-Requests-Expire"


@dataclass(slots=True)
class _RateLimitState:
    """Остаток лимита по ОДНОМУ пути на момент последнего ответа.

    expires_at — не время из заголовка, а расчётная точка на monotonic():
    заголовок отдаёт "сколько ещё мс до сброса окна", а не абсолютное
    время, поэтому пересчитываем сразу после получения ответа."""

    remaining: int
    expires_at: float


def _to_decimal(value: Any, field: str) -> Decimal:
    """Числа биржи приходят строками — так и разбираем, минуя float.

    "" — тоже "нет значения": проверено живым запросом на openOrders,
    так приходит stopPrice у ордера без стопа (не null, не 0, а пустая
    строка)."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExchangeResponseError(
            f"Не удалось разобрать поле {field}: {value!r}"
        ) from exc


def _ms_to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _optional_ms_to_dt(value: Any) -> datetime | None:
    """Мягкий разбор времени (шаг 15.5.4): нет или не число → None, а не
    ошибка и не 1970-01-01."""
    try:
        return _ms_to_dt(value) if value not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


def _decimal_literal(value: Decimal) -> str:
    """Decimal как литерал числа в JSON — без экспоненциальной записи."""
    return format(value, "f")


def _parse_order_leverage(value: Any) -> int:
    """leverage в openOrders — строка вида "20X" (не int, как в get_positions())."""
    text = str(value or "").rstrip("Xx")
    return int(text) if text.isdigit() else 0


def _str_bool(value: Any) -> bool:
    """closePosition приходит строкой "false"/"true", reduceOnly — bool.
    Разбираем оба одним правилом, проверено живым запросом."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _build_tp_sl(order_type: OrderType, spec: TpSlSpec) -> str:
    """Собирает takeProfit/stopLoss в формате, который ждёт BingX.

    Не вложенный объект, а JSON, упакованный в строку — так это принимает
    /openApi/swap/v2/trade/order (проверено по живой документации BingX,
    см. docs/execution-stage-15.md, раздел 16).

    Строка собирается вручную (не json.dumps), без пробелов после ":" и
    ",": это уже эквивалент separators=(",", ":") — важно, потому что
    сервер пересчитывает подпись по сырой строке параметров (см.
    _build_signed_query), и лишний пробел внутри значения обязан совпасть
    у нас и у BingX посимвольно, а не только после URL-кодирования."""
    fields = [
        f'"type":"{order_type.value}"',
        f'"stopPrice":{_decimal_literal(spec.trigger_price)}',
    ]
    if spec.price is not None:
        fields.append(f'"price":{_decimal_literal(spec.price)}')
    fields.append(f'"workingType":"{spec.working_type}"')
    return "{" + ",".join(fields) + "}"


def bingx_position_side(side: TradeSide, dual_side_position: bool) -> str:
    """Переводит сторону позиции в значение параметра positionSide BingX
    (раздел 16 ТЗ, шаг 15.5.2) — для set_leverage и place_market_order.

    dual_side_position=True — хедж, LONG и SHORT раздельные позиции,
    positionSide обязан называть сторону явно. dual_side_position=False —
    односторонний режим счёта, BingX ждёт "BOTH" (см. докстринг
    ExchangeClient.set_leverage): нет двух раздельных позиций, называть
    сторону нечем и не нужно."""
    if not dual_side_position:
        return "BOTH"
    return side.value


class BingXClient(ExchangeClient):
    name = "bingx"
    # Подтверждено живым запросом: тот же ключ аутентифицируется и на
    # open-api.bingx.com, и на open-api-vst.bingx.com (get_balance/
    # get_api_restrictions отработали одинаково на обоих хостах одним и
    # тем же ключом). Отдельная пара для DEMO не обязательна.
    shares_keys_across_modes = True

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
        mode: ExchangeKeyMode = ExchangeKeyMode.LIVE,
        rate_limit_threshold: int = 20,
        rate_limit_throttle_enabled: bool = True,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._recv_window = recv_window
        self._max_retries = max_retries
        self._rate_limit_threshold = rate_limit_threshold
        self._rate_limit_throttle_enabled = rate_limit_throttle_enabled
        # По пути, не общий на клиент — см. комментарий у _RateLimitState.
        # Живёт, пока живёт сам клиент: смысл только у клиента, который
        # переживает больше одного вызова (сканер — см. app/workers/scanner.py),
        # у одноразовых клиентов хендлеров это просто пустой словарь.
        self._rate_limits: dict[str, _RateLimitState] = {}
        # Фактически отправленные запросы (считая повторы) — используется
        # для замера цикла сканера (app/workers/scanner.py), не для самого
        # троттлинга.
        self.request_count = 0
        # Не угадывается по base_url (второй источник истины разошёлся бы с
        # Settings.bingx_demo_base_url) — вызывающий код (ExchangeFactory.
        # for_user(), тот же самый mode) и так знает режим, достаточно его
        # передать. public_client() данными приватного баланса не пользуется
        # (там нет ключей), LIVE-дефолт там ничего не решает.
        self._mode = mode
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

        Раздел "Signature Description" официальной документации BingX
        (https://bingx-api.github.io/docs/#/en-us/swapV2/authentication.html
        #Signature%20Description; детальный пример со значением-JSON —
        раздел "Place multiple orders", .../trade-api.html#Bulk%20order):
        подпись считается по СЫРОЙ, ещё не закодированной строке параметров;
        URL-кодирование (только значений, не ключей и не строки целиком)
        применяется отдельно, уже после подписи, только к тому, что уходит
        в запрос. До этой правки код кодировал строку целиком (urlencode())
        и подписывал уже закодированный результат — для простых значений
        (BTC-USDT, BUY, числа) кодирование ничего не меняет, поэтому
        расхождения не было видно нигде, кроме takeProfit/stopLoss: их
        значение — JSON-строка со спецсимволами {"":,, которые кодирование
        меняет, и получавшаяся подпись переставала совпадать с тем, что
        пересчитывает сервер по сырой строке (раздел 16 ТЗ, docs/
        execution-stage-15.md).

        Порядок параметров фиксируется здесь и больше не меняется — важно
        не для подписи как таковой (BingX подписывает конкатенацию как
        есть, без сортировки), а чтобы raw- и encoded-версии ниже были
        построены по одним и тем же парам."""
        payload = {k: v for k, v in params.items() if v is not None}
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = self._recv_window

        pairs = [(k, str(v)) for k, v in payload.items()]
        raw_query = "&".join(f"{k}={v}" for k, v in pairs)
        signature = self._sign(raw_query)

        # quote(), не quote_plus(): документация BingX кодирует только
        # значения (не ключи, не строку целиком) — quote_plus дополнительно
        # превращал бы пробел в "+", а не "%20", как в примерах документации.
        # safe="" — без исключений: экранируем всё, что не входит в
        # "always safe" по RFC 3986 (буквы/цифры/"_.-~" quote не трогает
        # в любом случае), включая "/", если он когда-нибудь появится
        # в значении.
        encoded_query = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)
        return f"{encoded_query}&signature={signature}"

    # --- Транспорт ---------------------------------------------------------

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        method: str = "GET",
        max_retries: int | None = None,
    ) -> Any:
        params = params or {}

        if signed:
            if not self._api_key:
                raise ExchangeAuthError(
                    "Не заданы ключи BingX. Подключи их в настройках бота."
                )
            # BingX и для POST ждёт параметры строкой запроса, не JSON-телом —
            # подписанная строка формируется одинаково для любого метода.
            url = f"{path}?{self._build_signed_query(params)}"
            headers = {"X-BX-APIKEY": self._api_key}
        else:
            url = f"{path}?{urlencode(params)}" if params else path
            headers = {}

        send = self._client.get if method == "GET" else self._client.post
        retries = max_retries if max_retries is not None else self._max_retries

        await self._maybe_throttle(path)

        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                response = await send(url, headers=headers)
                self.request_count += 1
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
                self._update_rate_limit(path, response)
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

            if attempt < retries:
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

    async def _maybe_throttle(self, path: str) -> None:
        """Притормаживает ПЕРЕД запросом, если по этому пути остаток мал
        (раздел "троттлинг сканера"). Постфактум-реакция на 429 (Retry-After
        в _parse) остаётся отдельно и не отменяется этим — это подстраховка
        на случай, если порог не сработал (например, самый первый запрос
        после старта процесса, когда state ещё пуст).

        Если состояния по этому пути нет (ещё не отвечали, или ответ не
        принёс заголовков) — не тормозим: отсутствие не значит "остаток 0",
        значит "неизвестно" (см. комментарий у _HEADER_RATE_LIMIT_REMAIN)."""
        if not self._rate_limit_throttle_enabled:
            return
        state = self._rate_limits.get(path)
        if state is None:
            return
        remaining_seconds = state.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            # Окно по нашим расчётам уже истекло — реальный остаток на
            # стороне биржи наверняка сброшен, наше устаревшее число не
            # повод ждать.
            return
        if state.remaining > self._rate_limit_threshold:
            return
        logger.info(
            "Троттлинг BingX: мало остатка лимита, ждём конца окна",
            extra={
                "path": path,
                "remaining": state.remaining,
                "wait_seconds": round(remaining_seconds, 3),
            },
        )
        await self._sleep(remaining_seconds)

    def _update_rate_limit(self, path: str, response: httpx.Response) -> None:
        """Разбирает заголовки остатка лимита (см. _HEADER_RATE_LIMIT_REMAIN).

        Их может не быть, или значение может быть неожиданным (сторонний
        прокси между нами и BingX, например) — тогда просто не обновляем
        state для этого пути и идём дальше, не падаем."""
        remain = response.headers.get(_HEADER_RATE_LIMIT_REMAIN)
        expire = response.headers.get(_HEADER_RATE_LIMIT_EXPIRE)
        if remain is None or expire is None:
            return
        try:
            remaining = int(remain)
            expires_in_seconds = int(expire) / 1000
        except (TypeError, ValueError):
            return
        self._rate_limits[path] = _RateLimitState(
            remaining=remaining,
            expires_at=time.monotonic() + expires_in_seconds,
        )

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
            # None, а не 0, если код не разобрался в число — раздел 16 ТЗ,
            # шаг 15.5.2: 0 у ExchangeError.code теперь означает не "код не
            # взялся", а "биржа его не назвала совсем", что здесь не так —
            # code присутствовал, просто не int. Смешивать эти два случая
            # значило бы ронять REJECTED-классификацию на пути отправки
            # ордера (см. ExecutionService.submit_entry_order()).
            code_int = int(code) if str(code).lstrip("-").isdigit() else None
            if code_int in _FATAL_CODES:
                raise ExchangeAuthError(
                    f"BingX: {message} (код {code})", code=code_int, payload=payload
                )
            if code_int == 100410:
                raise ExchangeRateLimitError(
                    f"BingX: {message}", 2.0, code=code_int, payload=payload
                )
            raise ExchangeResponseError(
                f"BingX: {message} (код {code})", code=code_int, payload=payload
            )

        return payload.get("data", payload)

    async def close(self) -> None:
        await self._client.aclose()

    # --- Публичные данные --------------------------------------------------

    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        data = await self._request(QUOTE_TICKER, {"symbol": symbol}, max_retries=max_retries)
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

    async def get_symbols(self, *, max_retries: int | None = None) -> list[SymbolInfo]:
        data = await self._request(QUOTE_CONTRACTS, max_retries=max_retries)
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
                    min_notional=_to_decimal(
                        item.get("tradeMinUSDT"), "tradeMinUSDT"
                    ),
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

    async def get_balance(self, *, max_retries: int | None = None) -> Balance:
        expected_asset = _QUOTE_ASSET_BY_MODE[self._mode]
        data = await self._request(USER_BALANCE, signed=True, max_retries=max_retries)
        if isinstance(data, list):
            # Боевая форма ответа (проверено по логам, не по документации,
            # раздел 16 ТЗ): список записей по активам, у каждой уже плоский
            # набор полей — "balance" тут строка-сумма, а не вложенный
            # объект. Берём запись нужного актива явно: порядок активов в
            # списке не гарантирован (в живом ответе после USDT шёл USDC),
            # а сам актив зависит от контура — USDT на LIVE, VST на DEMO
            # (_QUOTE_ASSET_BY_MODE). Если ожидаемого актива нет вовсе —
            # не берём первый попавшийся молча, а падаем с понятной
            # ошибкой: тихая подмена валюты маржи хуже явного отказа.
            match = next(
                (item for item in data if item.get("asset") == expected_asset), None
            )
            if match is None:
                found = [item.get("asset") for item in data]
                raise ExchangeResponseError(
                    f"В ответе BingX нет актива {expected_asset} "
                    f"(режим {self._mode.value}). Получены активы: {found or 'пусто'}."
                )
            data = match
        if isinstance(data, dict) and isinstance(data.get("balance"), dict):
            # Другая форма ответа: {"balance": {...вложенный объект...}}.
            # Разворачиваем, только если "balance" действительно объект —
            # в форме выше это просто строка, и разворачивать там нечего.
            data = data["balance"]

        equity = _to_decimal(data.get("equity"), "equity")
        return Balance(
            asset=data.get("asset", expected_asset),
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

    async def get_api_restrictions(self) -> ApiRestrictions:
        # ВАЖНО: в отличие от get_balance/get_positions выше, здесь поля
        # лежат на верхнем уровне ответа, не под "data" — _request()/_parse()
        # в этом случае возвращают payload целиком (payload.get("data",
        # payload) откатывается на payload при отсутствии ключа "data"),
        # так что fallback уже отрабатывает сам собой, без доп. кода тут.
        data = await self._request(API_RESTRICTIONS, signed=True)
        create_time = data.get("createTime")
        if create_time is None:
            raise ExchangeResponseError("В ответе apiRestrictions нет createTime")
        return ApiRestrictions(
            ip_restrict=bool(data.get("ipRestrict", False)),
            create_time=_ms_to_dt(create_time),
            permits_universal_transfer=bool(data.get("permitsUniversalTransfer", False)),
            enable_reading=bool(data.get("enableReading", False)),
            enable_futures=bool(data.get("enableFutures", False)),
            enable_spot_and_margin_trading=bool(
                data.get("enableSpotAndMarginTrading", False)
            ),
        )

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
            order_id=str(item.get("orderId")) if item.get("orderId") else None,
        )

    # --- Торговые методы (этап 15.2) ----------------------------------------
    #
    # Всё, что размещает или меняет реальные ордера, идёт с retries=1:
    # автоматический повтор на таймауте здесь недопустим — раздел 8 ТЗ
    # прямо запрещает повторную отправку без предварительной сверки по
    # client_order_id, а транспортный retry этой сверки не делает. Чтение
    # (get_order, set_leverage — идемпотентная по своей природе операция)
    # обычный retry сохраняет.

    async def get_leverage(
        self, symbol: str, *, max_retries: int | None = None
    ) -> LeverageInfo:
        """Раздел 16 ТЗ, шаг 15.5.1: GET на тот же путь, что и set_leverage
        ниже — чтение, не изменение состояния, поэтому обычный retry
        (max_retries=None по умолчанию), а не max_retries=1, как у POST-
        методов этого блока. Поля без дефолтов: если maxLongLeverage/
        maxShortLeverage/longLeverage/shortLeverage нет в ответе —
        ExchangeResponseError, не подставленное число (раздел 16: живой
        ответ /quote/contracts вообще не отдаёт максимум плеча — молчаливый
        дефолт по этой же причине убран у SymbolInfo)."""
        data = await self._request(
            TRADE_LEVERAGE, {"symbol": symbol}, signed=True, max_retries=max_retries
        )
        if not isinstance(data, dict):
            raise ExchangeResponseError("Ожидался объект с данными о плече")
        required = ("longLeverage", "shortLeverage", "maxLongLeverage", "maxShortLeverage")
        missing = [field for field in required if data.get(field) is None]
        if missing:
            raise ExchangeResponseError(
                f"В ответе {TRADE_LEVERAGE} нет полей: {', '.join(missing)}"
            )
        return LeverageInfo(
            symbol=data.get("symbol", symbol),
            long_leverage=int(data["longLeverage"]),
            short_leverage=int(data["shortLeverage"]),
            max_long_leverage=int(data["maxLongLeverage"]),
            max_short_leverage=int(data["maxShortLeverage"]),
        )

    async def get_position_mode(self, *, max_retries: int | None = None) -> bool:
        """Раздел 16 ТЗ, шаг 15.5.1: GET POSITION_SIDE_DUAL (v1 — см.
        комментарий у константы). Чтение, обычный retry по умолчанию.
        dualSidePosition — под data (проверено живым запросом), значит
        _request()/_parse() уже развернули её сюда; отсутствие поля —
        явная ошибка, не молчаливый дефолт (тот же принцип, что и в
        get_leverage выше)."""
        data = await self._request(POSITION_SIDE_DUAL, signed=True, max_retries=max_retries)
        if not isinstance(data, dict) or "dualSidePosition" not in data:
            raise ExchangeResponseError(
                f"В ответе {POSITION_SIDE_DUAL} нет поля dualSidePosition"
            )
        return bool(data["dualSidePosition"])

    async def set_leverage(
        self, symbol: str, leverage: int, *, position_side: str | None = None
    ) -> int:
        params: dict[str, Any] = {
            "symbol": symbol,
            "leverage": leverage,
            "side": position_side or "BOTH",
        }
        # retries=1: это торговый вызов, меняющий состояние позиции перед
        # входом — та же логика, что и у place_market_order ниже (раздел 8
        # ТЗ, см. докстринг блока "Торговые методы" выше). До этой правки
        # параметр не передавался, и вызов молча ретраился 3 раза — расходясь
        # с собственным докстрингом файла.
        data = await self._request(
            TRADE_LEVERAGE, params, signed=True, method="POST", max_retries=1
        )
        return int(data.get("leverage", leverage))

    async def place_market_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        position_side: str,
        quantity: Decimal,
        client_order_id: str,
        take_profit: TpSlSpec | None = None,
        stop_loss: TpSlSpec | None = None,
    ) -> OrderResult:
        if not 1 <= len(client_order_id) <= 40:
            raise ValueError(
                "clientOrderID у BingX — 1-40 символов, получено "
                f"{len(client_order_id)}: {client_order_id!r}"
            )

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "positionSide": position_side,
            "type": OrderType.MARKET.value,
            "quantity": _decimal_literal(quantity),
            "clientOrderID": client_order_id,
        }
        if take_profit is not None:
            params["takeProfit"] = _build_tp_sl(OrderType.TAKE_PROFIT_MARKET, take_profit)
        if stop_loss is not None:
            params["stopLoss"] = _build_tp_sl(OrderType.STOP_MARKET, stop_loss)

        data = await self._request(
            TRADE_ORDER, params, signed=True, method="POST", max_retries=1
        )
        order = data.get("order", data) if isinstance(data, dict) else {}
        return self._parse_order(order)

    async def place_conditional_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        position_side: str,
        order_type: str,
        stop_price: Decimal,
        client_order_id: str,
    ) -> OrderResult:
        """Шаг 15.5.3: см. ExchangeClient.place_conditional_order. Тот же
        путь /trade/order, что и у входа, но type — условный, stopPrice
        вместо quantity и closePosition=true. max_retries=1 — торговый
        вызов, повтор после обрыва мог бы выставить второй ордер (раздел 8
        ТЗ, как у place_market_order). Форма параметров — по документации
        BingX, живьём не снята (раздел 16, 15.5.5)."""
        if order_type not in (OrderType.STOP_MARKET.value, OrderType.TAKE_PROFIT_MARKET.value):
            raise ValueError(f"Не условный тип ордера: {order_type!r}")
        if not 1 <= len(client_order_id) <= 40:
            raise ValueError(
                "clientOrderID у BingX — 1-40 символов, получено "
                f"{len(client_order_id)}: {client_order_id!r}"
            )
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "positionSide": position_side,
            "type": order_type,
            "stopPrice": _decimal_literal(stop_price),
            "closePosition": "true",
            "workingType": CONDITIONAL_WORKING_TYPE,
            "clientOrderID": client_order_id,
        }
        data = await self._request(
            TRADE_ORDER, params, signed=True, method="POST", max_retries=1
        )
        order = data.get("order", data) if isinstance(data, dict) else {}
        return self._parse_order(order)

    async def get_order(
        self, symbol: str, client_order_id: str, *, max_retries: int | None = None
    ) -> OrderResult:
        params = {"symbol": symbol, "clientOrderID": client_order_id}
        data = await self._request(TRADE_ORDER, params, signed=True, max_retries=max_retries)
        order = data.get("order", data) if isinstance(data, dict) else {}
        return self._parse_order(order)

    async def get_order_fill(
        self, symbol: str, client_order_id: str, *, max_retries: int | None = None
    ) -> OrderFill:
        """Шаг 15.5.3: тот же GET, что у get_order, но строгий разбор
        (_parse_order_fill). Фикстуры тестов — синтетика до 15.5.5."""
        params = {"symbol": symbol, "clientOrderID": client_order_id}
        data = await self._request(TRADE_ORDER, params, signed=True, max_retries=max_retries)
        order = data.get("order", data) if isinstance(data, dict) else None
        if not isinstance(order, dict):
            raise ExchangeResponseError("Ожидался объект ордера в data.order")
        return self._parse_order_fill(order)

    @staticmethod
    def _parse_order_fill(item: dict[str, Any]) -> OrderFill:
        """Строгий разбор для read-back: обязательное поле отсутствует или
        пустое → ReadbackIncomplete с именем поля, а не Decimal(0) (в
        отличие от _parse_order/_to_decimal — там "" и None дают ноль).
        Написание orderId/orderID и clientOrderId/clientOrderID — оба
        варианта, как в _parse_order: документация BingX расходится."""

        def required(*names: str) -> Any:
            for name in names:
                value = item.get(name)
                if value is not None and value != "":
                    return value
            raise ReadbackIncomplete(names[0], payload=item)

        def number(name: str) -> Decimal:
            return _to_decimal(required(name), name)

        return OrderFill(
            order_id=str(required("orderId", "orderID")),
            client_order_id=str(
                item.get("clientOrderId") or item.get("clientOrderID") or ""
            ),
            status=str(required("status")),
            avg_price=number("avgPrice"),
            orig_qty=number("origQty"),
            executed_qty=number("executedQty"),
            fee=abs(number("commission")),
            raw=item,
            filled_at=_optional_ms_to_dt(item.get("updateTime") or item.get("time")),
        )

    @staticmethod
    def _parse_order(item: dict[str, Any]) -> OrderResult:
        # Документация BingX сама расходится в написании этих двух полей
        # (orderId/orderID, clientOrderId/clientOrderID) — берём оба варианта.
        return OrderResult(
            order_id=str(item.get("orderId") or item.get("orderID") or ""),
            client_order_id=str(
                item.get("clientOrderId") or item.get("clientOrderID") or ""
            ),
            symbol=item.get("symbol", ""),
            side=str(item.get("side", "")),
            position_side=str(item.get("positionSide", "")),
            order_type=str(item.get("type", "")),
            status=str(item.get("status", "")),
            price=_to_decimal(item.get("price"), "price"),
            avg_price=_to_decimal(item.get("avgPrice"), "avgPrice"),
            quantity=_to_decimal(
                item.get("origQty") or item.get("quantity"), "quantity"
            ),
            executed_qty=_to_decimal(item.get("executedQty"), "executedQty"),
            fee=abs(_to_decimal(item.get("commission"), "commission")),
            raw=item,
        )

    async def get_open_orders(
        self, symbol: str | None = None, *, max_retries: int | None = None
    ) -> list[OpenOrder]:
        params = {"symbol": symbol} if symbol else {}
        data = await self._request(
            TRADE_OPEN_ORDERS, params, signed=True, max_retries=max_retries
        )
        orders = data.get("orders", []) if isinstance(data, dict) else data
        if not isinstance(orders, list):
            raise ExchangeResponseError("Ожидался список ордеров в data.orders")
        return [self._parse_open_order(item) for item in orders if item]

    @staticmethod
    def _parse_attached_tp_sl(item: Any) -> AttachedTpSl | None:
        if not isinstance(item, dict):
            return None
        stop_price = _to_decimal(item.get("stopPrice"), "stopPrice")
        if stop_price == 0:
            # Не задан: BingX всегда кладёт объект-заглушку, а не опускает
            # поле, — price/quantity нулевые и у реально прикреплённого
            # условника (проверено живым запросом), поэтому решает только
            # stopPrice.
            return None
        return AttachedTpSl(
            trigger_price=stop_price,
            price=_to_decimal(item.get("price"), "price"),
            quantity=_to_decimal(item.get("quantity"), "quantity"),
            working_type=str(item.get("workingType", "")),
        )

    @staticmethod
    def _parse_open_order(item: dict[str, Any]) -> OpenOrder:
        return OpenOrder(
            order_id=str(item.get("orderId") or item.get("orderID") or ""),
            client_order_id=str(
                item.get("clientOrderId") or item.get("clientOrderID") or ""
            ),
            symbol=item.get("symbol", ""),
            side=str(item.get("side", "")),
            position_side=str(item.get("positionSide", "")),
            order_type=str(item.get("type", "")),
            quantity=_to_decimal(item.get("origQty"), "origQty"),
            executed_qty=_to_decimal(item.get("executedQty"), "executedQty"),
            price=_to_decimal(item.get("price"), "price"),
            stop_price=_to_decimal(item.get("stopPrice"), "stopPrice"),
            status=str(item.get("status", "")),
            leverage=_parse_order_leverage(item.get("leverage")),
            reduce_only=_str_bool(item.get("reduceOnly")),
            close_position=_str_bool(item.get("closePosition")),
            working_type=str(item.get("workingType", "")),
            created_at=_ms_to_dt(item.get("time") or 0),
            updated_at=_ms_to_dt(item.get("updateTime") or 0),
            take_profit=BingXClient._parse_attached_tp_sl(item.get("takeProfit")),
            stop_loss=BingXClient._parse_attached_tp_sl(item.get("stopLoss")),
        )
