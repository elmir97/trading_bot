"""Решение «менять ли плечо перед входом» (раздел 16 ТЗ, шаг 15.5.1).

Чистая функция, без сети и БД — тот же принцип, что и sizing.py: получает
уже готовый LeverageInfo (кто его раздобыл у биржи — забота вызывающего
кода), возвращает bool. Сама отправка POST set_leverage по этому решению —
не в этом шаге (см. docs/execution-stage-15.md, раздел 16, шаг 15.5.2:
здесь только решающая функция, вызов условного POST будет там же, где
появится сама отправка ордера).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from app.exchanges.base import LeverageInfo
from app.trading.enums import TradeSide


def entry_leverage(
    *,
    entry_price: Decimal,
    stop_loss: Decimal,
    max_leverage: int,
    liq_buffer: Decimal,
    maint_margin_rate: Decimal,
) -> int:
    """Плечо входа от стопа (решение 28.09): ликвидация должна быть дальше
    стопа с запасом liq_buffer.

    При изолированной марже ликвидация отстоит от входа примерно на
    1/плечо − поддерживающая маржа. Условие 1/L − mmr ≥ буфер × стоп даёт
    L ≤ 1 / (стоп × буфер + mmr). Результат — floor, не выше
    plan.max_leverage (потолок, не фактическое значение), не ниже 1.
    stop — доля от цены входа: |вход − стоп| / вход.

    #110 XRP (стоп 13.84%) → 4x; LINK от цены «Да» (6.06%) → 10x (потолок)."""
    if entry_price <= 0:
        raise ValueError(f"Цена входа должна быть положительной: {entry_price}")
    if max_leverage < 1:
        raise ValueError(f"Потолок плеча плана должен быть ≥ 1: {max_leverage}")
    stop_fraction = abs(entry_price - stop_loss) / entry_price
    denominator = stop_fraction * liq_buffer + maint_margin_rate
    if denominator <= 0:
        return max_leverage
    by_stop = int((Decimal(1) / denominator).to_integral_value(rounding=ROUND_FLOOR))
    return max(1, min(max_leverage, by_stop))


def leverage_needs_update(
    current: LeverageInfo, desired: int, side: TradeSide
) -> bool:
    """Сравнивает ПО НУЖНОЙ СТОРОНЕ — longLeverage для LONG, shortLeverage
    для SHORT, не любое из двух. В хедж-режиме плечо LONG и SHORT
    независимо: совпадение одной стороны с желаемым ничего не говорит
    про другую, а set_leverage() всегда выставляет плечо только той
    стороне, что ему передали (раздел 16, проверено на живом /trade/
    leverage: сейчас на демо longLeverage=20, shortLeverage=20 при
    maxLongLeverage=maxShortLeverage=150 — числа разные по природе, не
    по факту, их не предполагается путать)."""
    current_leverage = (
        current.long_leverage if side is TradeSide.LONG else current.short_leverage
    )
    return current_leverage != desired
