"""Ответы API. Деньги, цены, проценты — Decimal: в JSON уходят строками
(pydantic v2), уже округлёнными для показа — JS только форматирует (§5)."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

CENT = Decimal("0.01")


def money(value: Decimal | None) -> Decimal | None:
    """Деньги и проценты для показа — два знака, ROUND_HALF_UP (как formatting.py)."""
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


def price(value: Decimal | None, precision: int) -> Decimal | None:
    """Цена — знаков по pricePrecision контракта (как fmt_price)."""
    if value is None:
        return None
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


class ErrorBody(BaseModel):
    code: str
    message: str


class Health(BaseModel):
    ok: bool


class Plan(BaseModel):
    risk_percent: Decimal | None      # риск по умолчанию = максимум плана
    max_leverage: int | None
    min_rr: Decimal | None
    allowed_symbols: list[str]


class Flags(BaseModel):
    dry_run: bool                     # EXEC_OPEN_DRY_RUN — на биржу не уйдёт
    live_allowed: bool                # EXEC_OPEN_ALLOW_LIVE


class Me(BaseModel):
    account_mode: str                 # DEMO | LIVE
    equity: Decimal | None
    available: Decimal | None
    balance_error: str | None         # баланс не прочитан — почему (экран: «—»)
    margin_type: str                  # ISOLATED | CROSSED — для новых сделок
    plan: Plan
    flags: Flags
    limit_expiry_minutes: list[int]
    limit_expiry_default: int
    card_ttl_seconds: int


class Contract(BaseModel):
    symbol: str
    price_precision: int
    quantity_precision: int
    min_quantity: Decimal
    min_notional: Decimal


class Change(BaseModel):
    value: Decimal | None
    percent: Decimal | None


class Leverage(BaseModel):
    long: int
    short: int
    max_long: int
    max_short: int


class Market(BaseModel):
    contract: Contract
    last: Decimal
    time: datetime                    # время котировки (UTC)
    interval: str
    candle_change: Change             # last против open текущей свечи интервала
    day_change: Change                # за 24 ч (тикер)
    quote_volume: Decimal | None      # объём за 24 ч, USDT
    open_interest: Decimal | None     # USDT
    funding_rate_percent: Decimal | None
    next_funding_time: datetime | None
    leverage: Leverage | None         # счёта по символу; нет ключа/сбой — None
    leverage_error: str | None
    details_error: str | None         # фандинг/ОИ не прочитаны — экран «—»


class Candle(BaseModel):
    time: int                         # unix, секунды — открытие свечи
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class Candles(BaseModel):
    symbol: str
    interval: str
    candles: list[Candle]


__all__ = [
    "Candle",
    "Candles",
    "Change",
    "Contract",
    "ErrorBody",
    "Flags",
    "Health",
    "Leverage",
    "Market",
    "Me",
    "Plan",
    "money",
    "price",
]
