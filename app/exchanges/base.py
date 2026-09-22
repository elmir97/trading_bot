"""Абстракция биржи.

Бизнес-логика работает только с этими типами и интерфейсом, а не с
конкретной биржей. Благодаря этому добавление Bybit или Binance не
потребует правок в журнале, статистике или анализе — достаточно новой
реализации ExchangeClient.

Все числовые значения — Decimal. Биржи отдают числа строками именно
затем, чтобы не терять точность, и превращать их во float по дороге
означало бы обесценить эту предосторожность.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.trading.enums import OrderSide, TradeSide

# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------


class ExchangeError(RuntimeError):
    """Базовая ошибка работы с биржей.

    code/payload — раздел 16 ТЗ, шаг 15.5.2: числовой код и сырой JSON
    ответа биржи, когда он реально пришёл (BingXClient._parse() отдаёт их
    при code != 0). None у обоих — ответа не было вовсе (транспортный сбой)
    или код был 0, но что-то не разобралось уже после него (см.
    BingXClient._parse_order()/_to_decimal). Это различие и есть граница
    между REJECTED (биржа явно отказала — code не None и не 0) и UNKNOWN
    (мы не знаем, что произошло на её стороне) на пути отправки ордера —
    см. app/execution/service.py, ExecutionService.submit_entry_order()."""

    def __init__(
        self, message: str, *, code: int | None = None, payload: dict | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class ExchangeAuthError(ExchangeError):
    """Неверные ключи, недостаточно прав, или ключ не привязан к этому IP."""


class ExchangeRateLimitError(ExchangeError):
    """Превышен лимит запросов. Содержит рекомендуемую паузу."""

    def __init__(
        self,
        message: str,
        retry_after: float = 1.0,
        *,
        code: int | None = None,
        payload: dict | None = None,
    ) -> None:
        super().__init__(message, code=code, payload=payload)
        self.retry_after = retry_after


class ExchangeUnavailableError(ExchangeError):
    """Биржа недоступна: таймаут, обрыв связи, технические работы.

    code/payload здесь всегда None — по определению: этот класс как раз
    и означает, что ответа не было (см. докстринг ExchangeError выше)."""


class ExchangeResponseError(ExchangeError):
    """Ответ получен, но не соответствует ожидаемому формату."""


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: Decimal
    volume_24h: Decimal
    price_change_percent: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class Kline:
    """Одна свеча OHLCV.

    open_time — время открытия свечи. Незакрытую свечу анализировать
    нельзя: методология требует торговать по закрытию, и is_closed
    позволяет отфильтровать её на уровне данных, а не в стратегии.
    """

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: datetime

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def body_ratio(self) -> Decimal:
        """Доля тела в диапазоне свечи.

        Используется для определения полнотелой свечи при пробое:
        методология требует уверенного закрытия за уровнем, а не
        свечи-волчка с длинными тенями.
        """
        if self.range == 0:
            return Decimal(0)
        return self.body / self.range

    @property
    def upper_wick(self) -> Decimal:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> Decimal:
        return min(self.open, self.close) - self.low

    def is_closed(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.close_time


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    available: Decimal       # свободно для новых позиций
    used_margin: Decimal     # заблокировано открытыми позициями
    unrealized_pnl: Decimal
    equity: Decimal          # общая стоимость счёта


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    side: TradeSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: int
    unrealized_pnl: Decimal
    margin: Decimal
    liquidation_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Исполнение с биржи — сырьё для импорта в журнал.

    external_id обязателен: по нему выполняется дедупликация при
    повторном импорте пересекающихся временных окон.
    """

    external_id: str
    symbol: str
    side: TradeSide           # направление позиции, а не ордера
    is_entry: bool            # увеличивает позицию или сокращает
    price: Decimal
    quantity: Decimal
    fee: Decimal
    realized_pnl: Decimal
    executed_at: datetime
    position_id: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """Торговые ограничения инструмента.

    Нужны, чтобы рассчитанный объём можно было реально выставить:
    биржа откажет, если объём не кратен шагу лота.
    """

    symbol: str
    price_precision: int
    quantity_precision: int
    min_quantity: Decimal
    # Минимальный нотионал (tradeMinUSDT у BingX). По умолчанию 0 — сохраняет
    # обратную совместимость с позиционным конструктором, которым эту
    # dataclass уже создают в тестах.
    min_notional: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class TpSlSpec:
    """Условный ордер (стоп или тейк), вложенный в маркет-вход.

    BingX принимает такой ордер не отдельным вызовом, а JSON-строкой
    внутри параметров takeProfit/stopLoss запроса на вход — см.
    BingXClient._build_tp_sl. price опционален: для *_MARKET условных
    ордеров исполнение идёт по рынку, цена нужна только справочно.
    """

    trigger_price: Decimal
    price: Decimal | None = None
    working_type: str = "MARK_PRICE"


@dataclass(frozen=True, slots=True)
class AttachedTpSl:
    """Условный ордер, вложенный в ОТКРЫТЫЙ ордер — GET .../trade/openOrders.

    Не путать с TpSlSpec: та описывает намерение при отправке входа, эта —
    то, что реально вернула биржа по уже существующему ордеру. Форма другая
    (есть quantity, нет опционального price) и признак "не задан" другой:
    BingX всегда кладёт объект (даже когда TP/SL не выставлен), а не
    опускает поле — пустой отличается от заполненного тем, что stopPrice
    в нём 0. price и quantity нулевые и у реально прикреплённого условника
    (проверено живым запросом на TAKE_PROFIT_MARKET/STOP_MARKET — там
    исполнение по рынку, cена не нужна), поэтому решает только stopPrice.
    """

    trigger_price: Decimal
    price: Decimal
    quantity: Decimal
    working_type: str


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """Выставленный, но не исполненный ордер — GET .../trade/openOrders.

    order_type — поле "type" ответа. Поле "orderType" рядом с ним в живом
    ответе было пустым и назначения не имеет (не документировано, не
    подтверждено живым запросом) — сюда не берём.
    """

    order_id: str
    client_order_id: str
    symbol: str
    side: str            # BUY | SELL
    position_side: str   # LONG | SHORT | BOTH
    order_type: str
    quantity: Decimal
    executed_qty: Decimal
    price: Decimal
    stop_price: Decimal
    status: str
    leverage: int
    reduce_only: bool
    close_position: bool
    working_type: str
    created_at: datetime
    updated_at: datetime
    take_profit: AttachedTpSl | None
    stop_loss: AttachedTpSl | None


@dataclass(frozen=True, slots=True)
class ApiRestrictions:
    """Права API-ключа (раздел 8 ТЗ) — GET /openApi/v1/account/apiRestrictions.

    enable_futures — единственное поле, которое приложение реально
    использует (is_read_only = not enable_futures, см.
    app/services/permissions.py); остальные — справочные, для показа
    пользователю и на будущее.
    """

    ip_restrict: bool
    create_time: datetime
    permits_universal_transfer: bool
    enable_reading: bool
    enable_futures: bool
    enable_spot_and_margin_trading: bool


@dataclass(frozen=True, slots=True)
class LeverageInfo:
    """Текущее и максимальное плечо по символу — GET /openApi/swap/v2/
    trade/leverage (раздел 16 ТЗ, шаг 15.5.1).

    Не то же самое, что SymbolInfo: контрактная ручка (/quote/contracts)
    максимум плеча не отдаёт вовсе (проверено живым запросом на демо-
    хосте) — единственный источник и текущего, и максимального плеча
    для аккаунта — эта ручка. Текущее и максимум идут раздельно по
    сторонам (long/short): в хедж-режиме плечо LONG и SHORT независимо,
    сравнивать значение "не по той стороне" — значит сравнивать не то,
    что реально спросят при входе (см. app/execution/leverage.py)."""

    symbol: str
    long_leverage: int
    short_leverage: int
    max_long_leverage: int
    max_short_leverage: int


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Ответ биржи на размещение или запрос ордера.

    status — оригинальная строка биржи (NEW/PENDING/FILLED/...), не наш
    внутренний OrderStatus из app.trading.enums: перевод одного в другой —
    ответственность execution/service.py, а не биржевого клиента.
    """

    order_id: str
    client_order_id: str
    symbol: str
    side: str            # BUY | SELL — сторона ордера, не позиции
    position_side: str   # LONG | SHORT | BOTH
    order_type: str
    status: str
    price: Decimal
    avg_price: Decimal
    quantity: Decimal
    executed_qty: Decimal
    # Комиссия. У BingX не приходит в ответе на размещение маркет-ордера
    # (только avg_price) — появляется лишь при последующем запросе ордера
    # (get_order) или в get_fills. Здесь 0, если источник её не вернул.
    fee: Decimal
    raw: dict[str, object]


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------


class ExchangeClient(ABC):
    """Контракт биржевого клиента.

    Разделение на публичные и приватные методы намеренное: рыночные
    данные доступны без ключей, и анализ рынка должен работать даже
    у пользователя, который ключи не подключал.
    """

    name: str

    # Свойство биржи, не аккаунта: у BingX один ключ аутентифицируется и на
    # боевом, и на демо-хосте (подтверждено живым запросом) — отдельная
    # пара для DEMO не нужна, хотя завести её всё равно можно. У других
    # бирж демо обычно отдельный аккаунт с собственными ключами — там
    # дефолт False должен остаться False. Решает ExchangeFactory.get_credentials():
    # нет строки под запрошенный режим — берёт строку другого режима.
    shares_keys_across_modes: bool = False

    # --- Публичные данные (ключи не требуются) ----------------------------

    @abstractmethod
    async def get_ticker(self, symbol: str, *, max_retries: int | None = None) -> Ticker:
        """max_retries=None — обычное поведение (self._max_retries клиента,
        см. BingXClient). Явное значение переопределяет его для этого
        вызова, не трогая остальных потребителей клиента — нужно пути
        подтверждения «Да» (раздел 8 ТЗ): там повтор транспортного слоя
        только удлиняет удержание лока, решение "повторять или нет"
        обязан принимать вызывающий код, не клиент."""
        ...

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        end_time: datetime | None = None,
    ) -> list[Kline]: ...

    @abstractmethod
    async def get_symbols(self, *, max_retries: int | None = None) -> list[SymbolInfo]:
        """max_retries — см. get_ticker."""
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Decimal | None: ...

    # --- Приватные данные (нужны ключи) -----------------------------------

    @abstractmethod
    async def get_balance(self, *, max_retries: int | None = None) -> Balance:
        """max_retries — см. get_ticker."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_api_restrictions(self) -> ApiRestrictions:
        """Права ключа (раздел 8 ТЗ) — не путать с get_balance/get_positions:
        читает саму учётку, а не торговые данные, но требует подписи так же."""
        ...

    @abstractmethod
    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OpenOrder]: ...

    @abstractmethod
    async def get_leverage(
        self, symbol: str, *, max_retries: int | None = None
    ) -> LeverageInfo:
        """Текущее и максимальное плечо по символу (раздел 16 ТЗ, шаг
        15.5.1) — GET, чтение, а не следующий за ним, отдельный set_leverage
        (POST) ниже. max_retries — см. get_ticker: путь подтверждения
        читает с max_retries=1, путь карточки — обычным ретраем."""
        ...

    @abstractmethod
    async def get_position_mode(self, *, max_retries: int | None = None) -> bool:
        """Режим позиций аккаунта: True — хедж (LONG/SHORT раздельно),
        False — односторонний (BOTH). Раздел 16 ТЗ, шаг 15.5.1 — рабочий
        путь v1 (не v2, тот отвечает code 100404), значение живёт под
        data, не на верхнем уровне (проверено живым запросом на демо-
        хосте). Возвращает bool напрямую, без обёртки-датакласса — как
        get_funding_rate выше, для единственного скалярного значения."""
        ...

    # --- Торговые методы (нужен ключ с правом Perpetual Futures Trading) ---
    #
    # Здесь и только здесь проходит граница с биржей: методы отправляют
    # запрос и возвращают то, что ответила биржа, ничего не решая сами
    # (например, в каком режиме позиций аккаунт и что поэтому положить в
    # position_side — это знание execution/service.py, не биржевого
    # клиента).

    @abstractmethod
    async def set_leverage(
        self, symbol: str, leverage: int, *, position_side: str | None = None
    ) -> int:
        """Выставляет плечо. position_side=None — односторонний режим счёта."""
        ...

    @abstractmethod
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
    ) -> OrderResult: ...

    @abstractmethod
    async def get_order(self, symbol: str, client_order_id: str) -> OrderResult: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> ExchangeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
