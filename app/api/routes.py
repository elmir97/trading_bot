"""Эндпоинты Mini App (docs/miniapp-plan.md §3)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from fastapi import APIRouter

from app.api.deps import ApiError, CurrentUser, Session, State
from app.api.market import MarketReader, change_from
from app.api.schemas import (
    Candle,
    Candles,
    Change,
    Contract,
    Flags,
    Health,
    Leverage,
    Market,
    Me,
    Plan,
    money,
    price,
)
from app.core.logging import get_logger
from app.exchanges.base import ExchangeError, SymbolInfo
from app.execution.opening.service import (
    LIMIT_EXPIRY_CHOICES,
    desired_margin_type,
    plan_limits,
)
from app.market.contracts import match_contracts

logger = get_logger(__name__)

_FUNDING_STEP = Decimal("0.0001")

router = APIRouter(prefix="/api")

Interval = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
SYMBOLS_LIMIT = 20


def _unavailable(exc: ExchangeError) -> ApiError:
    return ApiError(503, "EXCHANGE_UNAVAILABLE",
                    f"Биржа не ответила ({type(exc).__name__}) — повтори.")


def _contract(info: SymbolInfo) -> Contract:
    return Contract(
        symbol=info.symbol, price_precision=info.price_precision,
        quantity_precision=info.quantity_precision, min_quantity=info.min_quantity,
        min_notional=info.min_notional,
    )


async def _known(reader: MarketReader, symbol: str) -> SymbolInfo:
    try:
        info = await reader.symbol(symbol)
    except ExchangeError as exc:
        raise _unavailable(exc) from None
    if info is None:
        raise ApiError(404, "SYMBOL_NOT_FOUND", f"Контракта {symbol} на BingX нет.")
    return info


@router.get("/health")
async def health() -> Health:
    """Без авторизации и без данных пользователя — проверка деплоя."""
    return Health(ok=True)


@router.get("/me")
async def me(user: CurrentUser, state: State, session: Session) -> Me:
    settings = state.settings
    mode = settings.bingx_allowed_exchange_mode
    limits = await plan_limits(session, user.id)
    equity = available = None
    balance_error: str | None = None
    creds = await state.factory.get_credentials(session, user.id, mode=mode)
    if creds is None:
        balance_error = "Ключ BingX не подключён — подключи в настройках бота."
    else:
        client = await state.factory.for_user(session, user.id, mode=mode)
        try:
            balance = await client.get_balance()
            equity, available = balance.equity, balance.available
        except ExchangeError as exc:
            logger.warning("Mini App: баланс не прочитан",
                           extra={"user_id": user.id, "error": type(exc).__name__})
            balance_error = f"Биржа не ответила ({type(exc).__name__}) — обнови."
        finally:
            await client.close()
    return Me(
        account_mode=mode.value,
        equity=money(equity),
        available=money(available),
        balance_error=balance_error,
        margin_type=desired_margin_type(user).value,
        plan=Plan(
            risk_percent=money(limits.risk_percent), max_leverage=limits.max_leverage,
            min_rr=money(limits.min_rr), allowed_symbols=list(limits.allowed_symbols),
        ),
        flags=Flags(dry_run=settings.exec_open_dry_run,
                    live_allowed=settings.exec_open_allow_live),
        limit_expiry_minutes=list(LIMIT_EXPIRY_CHOICES),
        limit_expiry_default=settings.exec_open_limit_expiry_minutes,
        card_ttl_seconds=settings.exec_open_card_ttl_seconds,
    )


@router.get("/symbols")
async def symbols(user: CurrentUser, state: State, q: str = "") -> list[Contract]:
    """До 20 контрактов USDT-M: точное совпадение — одно; иначе по началу и вхождению."""
    async with MarketReader(state) as reader:
        try:
            found = match_contracts(await reader.symbols(), q, SYMBOLS_LIMIT)
        except ExchangeError as exc:
            raise _unavailable(exc) from None
    return [_contract(s) for s in found]


@router.get("/market/{symbol}")
async def market(
    symbol: str, user: CurrentUser, state: State, session: Session, interval: Interval = "5m",
) -> Market:
    async with MarketReader(state) as reader:
        info = await _known(reader, symbol)
        p = info.price_precision
        try:
            ticker = await reader.ticker(symbol)
            candles = await reader.candles(symbol, interval)
        except ExchangeError as exc:
            raise _unavailable(exc) from None
        last = ticker.last_price
        candle_delta, candle_pct = change_from(last, candles[-1].open if candles else None)

        premium = oi = None
        details_error: str | None = None
        try:
            premium = await reader.premium(symbol)
            oi = await reader.open_interest(symbol)
        except ExchangeError as exc:
            logger.warning("Mini App: детали рынка не прочитаны",
                           extra={"symbol": symbol, "error": type(exc).__name__})
            details_error = "Фандинг и открытый интерес сейчас недоступны."

        leverage = None
        leverage_error: str | None = None
        try:
            lev = await reader.leverage(session, user.id, symbol)
        except ExchangeError as exc:
            logger.warning("Mini App: плечо не прочитано",
                           extra={"user_id": user.id, "symbol": symbol,
                                  "error": type(exc).__name__})
            leverage_error = f"Плечо не прочитано ({type(exc).__name__})."
        else:
            if lev is None:
                leverage_error = "Ключ BingX не подключён — подключи в настройках бота."
            else:
                leverage = Leverage(long=lev.long_leverage, short=lev.short_leverage,
                                    max_long=lev.max_long_leverage,
                                    max_short=lev.max_short_leverage)
    return Market(
        contract=_contract(info), last=last, time=ticker.timestamp, interval=interval,
        candle_change=Change(value=price(candle_delta, p), percent=money(candle_pct)),
        day_change=Change(value=price(ticker.price_change, p),
                          percent=money(ticker.price_change_percent)),
        quote_volume=money(ticker.quote_volume),
        open_interest=money(oi.value_usdt) if oi else None,
        funding_rate_percent=(
            (premium.last_funding_rate * 100).quantize(_FUNDING_STEP) if premium else None
        ),
        next_funding_time=premium.next_funding_time if premium else None,
        leverage=leverage, leverage_error=leverage_error, details_error=details_error,
    )


@router.get("/market/{symbol}/candles")
async def candles(
    symbol: str, user: CurrentUser, state: State, interval: Interval = "5m",
) -> Candles:
    """≤ 200 свечей, последняя — незакрытая (график обновляет её от цены)."""
    async with MarketReader(state) as reader:
        info = await _known(reader, symbol)
        try:
            rows = await reader.candles(symbol, interval)
        except ExchangeError as exc:
            raise _unavailable(exc) from None
    step = Decimal(1).scaleb(-info.price_precision)

    def px(value: Decimal) -> Decimal:
        return value.quantize(step, rounding=ROUND_HALF_UP)

    return Candles(symbol=symbol, interval=interval, candles=[
        Candle(time=int(k.open_time.timestamp()), open=px(k.open), high=px(k.high),
               low=px(k.low), close=px(k.close))
        for k in rows
    ])


__all__ = ["router"]
