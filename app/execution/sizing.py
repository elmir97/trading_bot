"""Расчёт объёма позиции под ограничения биржи (этап 15.3, раздел 6 ТЗ).

Чистая функция на Decimal, без сети и БД. Формулу риска (риск% депозита
÷ дистанция до стопа) считает trading/calculations.calculate_position_size —
здесь не дублируем её, только то, что специфично для реальной отправки на
биржу: округление объёма вниз до шага лота символа и проверка минимумов
контракта (quantityPrecision/tradeMinQuantity/min_notional).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal

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


def round_levels_toward_entry(
    *, stop_loss: Decimal, take_profit: Decimal, side: TradeSide, price_precision: int
) -> tuple[Decimal, Decimal]:
    """Шаг 15.5.3: стоп и тейк — к шагу цены символа (pricePrecision) ДО
    сборки ордера, чтобы на биржу ушло ровно то значение, по которому потом
    ищем свой условник в openOrders (иначе биржа округлит по-своему, поиск
    по цене промахнётся и спасение поставит второй стоп).

    Направление — к цене входа, раздел 6 ТЗ: риск не больше заявленного.
    LONG: стоп ниже входа → вверх, тейк выше входа → вниз. SHORT: стоп выше
    входа → вниз, тейк ниже входа → вверх. Тейк тоже к входу — цель не
    завышается. Объём считается уже от округлённого стопа (см. evaluate())."""
    step = Decimal(1).scaleb(-price_precision)
    if side is TradeSide.LONG:
        return (
            stop_loss.quantize(step, rounding=ROUND_CEILING),
            take_profit.quantize(step, rounding=ROUND_FLOOR),
        )
    return (
        stop_loss.quantize(step, rounding=ROUND_FLOOR),
        take_profit.quantize(step, rounding=ROUND_CEILING),
    )


def calculate_size(
    *,
    account_balance: Decimal,
    available_margin: Decimal,
    risk_percent: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    side: TradeSide,
    leverage: int,
    symbol_info: SymbolInfo,
    fee_rate: Decimal,
) -> SizingResult | ExecutionRefusal:
    """fee_rate — taker-комиссия на ногу (Settings.exec_taker_fee_rate):
    объём = риск / (|вход − стоп| + fee_rate × (вход + стоп)), чтобы убыток
    на стопе вместе с комиссией входа и выхода не превышал риск по плану
    (живьём #40 SOL: без комиссии убыток на стопе 1933 при риске 1767).
    Обязательный параметр — молчаливый расчёт без комиссии был бы багом.

    account_balance — equity, база риска (риск% депозита). available_margin —
    свободная маржа биржи (availableMargin): с ней, а не с equity, сравнивается
    маржа входа плюс комиссия входа (fee_rate × нотионал). Хвост 26.09: при
    занятой марже объём от equity проходил гвард, и отказывала уже биржа."""
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

    # Комиссия входа и выхода по стопу на единицу объёма — к дистанции.
    # Квантование к 1e-12, как у calculate_position_size: на порядки точнее
    # любого quantityPrecision биржи, исход округления вниз не переворачивает.
    per_unit_loss = raw.stop_distance + fee_rate * (entry_price + stop_loss)
    quantity = _round_down_to_step(
        (raw.risk_amount / per_unit_loss).quantize(Decimal("0.000000000001")),
        symbol_info.quantity_precision,
    )

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
    # Комиссия входа списывается из той же свободной маржи. Вверх — чтобы
    # на границе отказал гвард, а не биржа.
    entry_fee = (fee_rate * notional).quantize(MONEY_PRECISION, rounding=ROUND_CEILING)
    if margin + entry_fee > available_margin:
        return ExecutionRefusal(
            ExecutionRefusalCode.INSUFFICIENT_MARGIN,
            f"Нужна маржа {margin:g} + комиссия входа {entry_fee:g}, "
            f"свободно {available_margin:g}.",
        )

    return SizingResult(
        quantity=quantity,
        notional=notional,
        margin=margin,
        risk_amount=raw.risk_amount,
    )
