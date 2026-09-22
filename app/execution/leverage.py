"""Решение «менять ли плечо перед входом» (раздел 16 ТЗ, шаг 15.5.1).

Чистая функция, без сети и БД — тот же принцип, что и sizing.py: получает
уже готовый LeverageInfo (кто его раздобыл у биржи — забота вызывающего
кода), возвращает bool. Сама отправка POST set_leverage по этому решению —
не в этом шаге (см. docs/execution-stage-15.md, раздел 16, шаг 15.5.2:
здесь только решающая функция, вызов условного POST будет там же, где
появится сама отправка ордера).
"""

from __future__ import annotations

from app.exchanges.base import LeverageInfo
from app.trading.enums import TradeSide


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
