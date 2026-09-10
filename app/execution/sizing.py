"""Расчёт объёма позиции под ограничения биржи (этап 15.3, раздел 6 ТЗ).

Чистая функция на Decimal, без сети и БД. Формулу риска (риск% депозита
÷ дистанция до стопа) считает trading/calculations.calculate_position_size —
здесь не дублируем её, только то, что специфично для реальной отправки на
биржу: округление объёма вниз до шага лота символа и проверка минимумов
контракта (quantityPrecision/tradeMinQuantity/min_notional).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.exchanges.base import SymbolInfo
from app.execution.models import ExecutionRefusal, ExecutionRefusalCode
from app.trading.calculations import (
    MONEY_PRECISION,
    CalculationError,
    calculate_position_size,
)
from app.trading.enums import TradeSide

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: Decimal
    notional: Decimal
    margin: Decimal
    risk_amount: Decimal


def _round_down_to_step(value: Decimal, precision: int) -> Decimal:
    """Округление вниз до шага лота. precision=3 → шаг 0.001, precision=0 → 1."""
    step = Decimal(1).scaleb(-precision)
    return value.quantize(step, rounding=ROUND_DOWN)


def calculate_size(
    *,
    account_balance: Decimal,
    risk_percent: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    side: TradeSide,
    leverage: int,
    symbol_info: SymbolInfo,
) -> SizingResult | ExecutionRefusal:
    if leverage < 1:
        raise CalculationError("Плечо не может быть меньше 1")

    try:
        raw = calculate_position_size(
            account_balance=account_balance,
            risk_percent=risk_percent,
            entry_price=entry_price,
            stop_loss=stop_loss,
            side=side,
        )
    except CalculationError as exc:
        # Нулевая/отрицательная дистанция до стопа или стоп по неверную
        # сторону — по идее уже отсеяно guard-ом INVALID_LEVELS (раздел 7,
        # п.10), но sizing обязан сам не упасть, если его вызовут в обход
        # guard-ов (например, из будущего service.py напрямую).
        return ExecutionRefusal(ExecutionRefusalCode.INVALID_LEVELS, str(exc))

    # calculate_position_size уже квантует к 1e-12 — на порядки точнее
    # любого реального quantityPrecision биржи, так что это округление не
    # может само по себе перевернуть исход следующего округления вниз.
    quantity = _round_down_to_step(raw.quantity, symbol_info.quantity_precision)

    if quantity <= ZERO:
        return ExecutionRefusal(
            ExecutionRefusalCode.SIZE_TOO_SMALL,
            "Расчётный объём округлился до нуля.",
        )
    if quantity < symbol_info.min_quantity:
        return ExecutionRefusal(
            ExecutionRefusalCode.SIZE_TOO_SMALL,
            f"Объём {quantity:g} меньше минимального лота {symbol_info.min_quantity:g}.",
        )

    notional = (quantity * entry_price).quantize(MONEY_PRECISION)
    if notional < symbol_info.min_notional:
        return ExecutionRefusal(
            ExecutionRefusalCode.SIZE_TOO_SMALL,
            f"Нотионал {notional:g} ниже минимального {symbol_info.min_notional:g}.",
        )

    margin = (notional / Decimal(leverage)).quantize(MONEY_PRECISION)
    if margin > account_balance:
        return ExecutionRefusal(
            ExecutionRefusalCode.INSUFFICIENT_MARGIN,
            f"Нужна маржа {margin:g}, доступно {account_balance:g}.",
        )

    return SizingResult(
        quantity=quantity,
        notional=notional,
        margin=margin,
        risk_amount=raw.risk_amount,
    )
