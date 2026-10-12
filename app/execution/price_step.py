"""Шаг цены — округление уровней перед расчётом и перед POST (очередь A.4).

BingX отдаёт точность цены контракта (pricePrecision), отдельного tick size
нет: шаг = 10^-pricePrecision. Цену не на шаге биржа молча округляет сама
(Т1, 08.10: тейк 1.44002 → 1.4400), и в базе остаётся не то, что стоит.

**Правило (решение владельца 12.10): округление никогда не увеличивает риск
в $ сверх выбранного.** Объём считается после округления (открытие) — стоп
дальше от входа: объём от риска уменьшится сам. Объём фиксирован (этап 4) —
стоп ближе к цене. Тейк — всегда ближе, лимит — в выгодную сторону.

Для открытия это одно направление на все три уровня: LONG — вниз (стоп ниже
входа — дальше, тейк выше — ближе, лимит — дешевле), SHORT — вверх.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.trading.enums import TradeSide

ZERO = Decimal(0)


def price_step(price_precision: int) -> Decimal:
    return Decimal(1).scaleb(-price_precision)


def on_step(value: Decimal, step: Decimal) -> bool:
    return value % step == ZERO


def _down_for_long(side: TradeSide) -> str:
    return ROUND_FLOOR if side is TradeSide.LONG else ROUND_CEILING


def _up_for_long(side: TradeSide) -> str:
    return ROUND_CEILING if side is TradeSide.LONG else ROUND_FLOOR


def opening_level(value: Decimal, side: TradeSide, step: Decimal) -> Decimal:
    """Стоп, тейк и лимит открытия: LONG вниз, SHORT вверх."""
    return value.quantize(step, rounding=_down_for_long(side))


def take_closer(value: Decimal, side: TradeSide, step: Decimal) -> Decimal:
    """Тейк ближе к цене: LONG вниз, SHORT вверх."""
    return value.quantize(step, rounding=_down_for_long(side))


def stop_closer(value: Decimal, side: TradeSide, step: Decimal) -> Decimal:
    """Стоп при фиксированном объёме (этап 4) — ближе к цене: LONG вверх, SHORT вниз."""
    return value.quantize(step, rounding=_up_for_long(side))
