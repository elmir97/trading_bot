"""Объём от риска на экранах мастера сделки и калькулятора (03.10.2026).

lot_step — шаг лота BingX по символу (публичный список контрактов, общий
кэш экранов). Недоступен (символа нет на бирже, сбой сети) — None, объём
округляется вниз до 8 знаков: журнал ручной, отказывать из-за биржи нельзя.

sizing_lines — строки расчёта: объём с пометкой, как он округлён, и риск
округлённого объёма (он не больше риска по плану).
"""

from __future__ import annotations

from decimal import Decimal

from app.bot.formatting import fmt_amount, fmt_num, fmt_pct, fmt_qty
from app.bot.handlers.exchange import _market_cache
from app.core.config import Settings
from app.core.logging import get_logger
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.calculations import DEFAULT_QUANTITY_STEP, PositionSizing

logger = get_logger(__name__)


async def lot_step(settings: Settings, symbol: str) -> Decimal | None:
    try:
        client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
        try:
            info = await MarketDataService(client, _market_cache).get_symbol_info(symbol)
        finally:
            await client.close()
    except Exception:
        logger.warning("Шаг лота не получен — объём до 8 знаков", extra={"symbol": symbol})
        return None
    if info is None:
        return None
    return Decimal(1).scaleb(-info.quantity_precision)


def _digits(step: Decimal) -> int:
    exponent = step.normalize().as_tuple().exponent
    return max(-exponent, 0) if isinstance(exponent, int) else 8


def sizing_lines(sizing: PositionSizing, balance: Decimal) -> list[str]:
    step = sizing.quantity_step or DEFAULT_QUANTITY_STEP
    if sizing.quantity_step is None:
        how = "вниз до 8 знаков, шаг лота биржи неизвестен"
    else:
        how = f"вниз до шага лота BingX {fmt_num(sizing.quantity_step)}"
    return [
        f"Сумма риска по плану: {fmt_amount(sizing.risk_amount)} USDT",
        f"Дистанция до стопа: {fmt_num(sizing.stop_distance_percent)}%",
        f"Объём: {fmt_qty(sizing.quantity, _digits(step))} ({how})",
        f"Риск по объёму: {fmt_amount(sizing.risk_actual)} USDT "
        f"({fmt_pct(sizing.risk_actual / balance * 100)})",
        f"Размер позиции: {fmt_amount(sizing.position_value)} USDT",
    ]
