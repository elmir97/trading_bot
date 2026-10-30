"""Read-time net R. No exchange calls and no writes to historical aggregates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.trading.enums import FillSide, TradeSide

ZERO = Decimal(0)


class Execution(Protocol):
    fill_side: FillSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    executed_at: datetime


@dataclass(frozen=True)
class RealizedResult:
    value: Decimal | None
    reason: str | None = None


def realized_result(
    side: TradeSide,
    fills: Sequence[Execution],
    initial_stop: Decimal | None,
    confirmed: bool,
) -> RealizedResult:
    """Return R of the closed quantity, allocating only its share of entry fee.

    Multiple entry rows are not proof of scaling in, but their original basis
    cannot be established by this minimal contract. A missing value is not zero.
    """
    if not confirmed:
        return RealizedResult(None, "Вход не подтверждён")
    entries = [f for f in fills if f.fill_side is FillSide.ENTRY]
    exits = [f for f in fills if f.fill_side is FillSide.EXIT]
    if len(entries) != 1:
        return RealizedResult(None, "Недостаточно данных для определения базы риска")
    entry = entries[0]
    if initial_stop is None:
        return RealizedResult(None, "Начальный стоп неизвестен")
    unit = (entry.price - initial_stop) * side.direction
    if unit <= ZERO:
        return RealizedResult(None, "Начальный стоп не задаёт убыточный риск")
    if not exits:
        return RealizedResult(None, "Нет подтверждённых выходов")
    if entry.executed_at is None or any(
        f.executed_at is None or f.executed_at < entry.executed_at for f in exits
    ):
        return RealizedResult(None, "Недостаточно данных о порядке исполнений")
    if any(f.fee is None for f in fills):
        return RealizedResult(None, "Комиссия неизвестна")
    if any(f.price <= ZERO or f.quantity <= ZERO for f in fills):
        return RealizedResult(None, "Некорректные исполнения")
    closed = sum((f.quantity for f in exits), ZERO)
    if closed > entry.quantity:
        return RealizedResult(None, "Закрытый объём превышает вход")
    gross = sum(((f.price - entry.price) * f.quantity * side.direction for f in exits), ZERO)
    entry_fee = entry.fee * closed / entry.quantity
    exit_fee = sum((f.fee for f in exits), ZERO)
    return RealizedResult((gross - entry_fee - exit_fee) / (unit * closed))
