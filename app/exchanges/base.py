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

from app.trading.enums import TradeSide

# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------


class ExchangeError(RuntimeError):
    """Базовая ошибка работы с биржей."""


class ExchangeAuthError(ExchangeError):
    """Неверные ключи, недостаточно прав, или ключ не привязан к этому IP."""


class ExchangeRateLimitError(ExchangeError):
    """Превышен лимит запросов. Содержит рекомендуемую паузу."""

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ExchangeUnavailableError(ExchangeError):
    """Биржа недоступна: таймаут, обрыв связи, технические работы."""


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
    max_leverage: int


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

    # --- Публичные данные (ключи не требуются) ----------------------------

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        end_time: datetime | None = None,
    ) -> list[Kline]: ...

    @abstractmethod
    async def get_symbols(self) -> list[SymbolInfo]: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Decimal | None: ...

    # --- Приватные данные (нужны ключи) -----------------------------------

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_fills(
        self, start_time: datetime, end_time: datetime, symbol: str | None = None
    ) -> list[Fill]: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> ExchangeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
