"""Финансовые расчёты.

Чистые функции: на вход Decimal, на выход Decimal. Ни базы, ни сети, ни
времени — поэтому их можно исчерпывающе покрыть тестами, а тесты и есть
единственная защита от тихой ошибки в деньгах.

Ключевые правила, зафиксированные здесь:

1. Плечо НЕ входит в формулу PnL.
       pnl = (exit - entry) * quantity * direction - fees
   где quantity — объём в базовом активе (BTC, ETH), а не в USDT.
   Плечо определяет лишь размер маржи и потому влияет на PnL % от
   вложенных средств, но не на сумму в долларах. Умножение PnL на плечо
   даёт цифры, расходящиеся с биржей в разы.

2. Все входные величины приводятся к Decimal явно. Float, попавший в
   расчёт из JSON биржи, накапливает ошибку и разрушает сверку с биржей.

3. Деление всегда с проверкой нуля: нулевая дистанция до стопа означает
   ошибку данных, а не бесконечный RR.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.trading.enums import FillSide, TradeSide

# Точность округления результатов. Промежуточные вычисления не округляем:
# округление на каждом шаге накапливает смещение.
MONEY_PRECISION = Decimal("0.00000001")
PERCENT_PRECISION = Decimal("0.0001")
RR_PRECISION = Decimal("0.01")

ZERO = Decimal(0)


class CalculationError(ValueError):
    """Некорректные входные данные для расчёта."""


def to_decimal(value: object, field: str = "значение") -> Decimal:
    """Безопасное приведение к Decimal.

    float конвертируется через str, иначе Decimal(0.1) даст
    0.1000000000000000055511151231257827, и эта грязь попадёт в базу.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip().replace(",", "."))
        except InvalidOperation as exc:
            raise CalculationError(f"Не удалось разобрать {field}: {value!r}") from exc
    raise CalculationError(f"Неподдерживаемый тип для {field}: {type(value).__name__}")


def _quantize(value: Decimal, precision: Decimal) -> Decimal:
    return value.quantize(precision)


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------


def calculate_pnl(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    side: TradeSide,
    fees: Decimal = ZERO,
) -> Decimal:
    """Реализованный PnL в валюте котировки (USDT).

    Плечо намеренно отсутствует в сигнатуре: оно не влияет на результат.
    """
    if quantity < ZERO:
        raise CalculationError("Объём не может быть отрицательным")
    if entry_price <= ZERO or exit_price <= ZERO:
        raise CalculationError("Цены должны быть положительными")

    gross = (exit_price - entry_price) * quantity * side.direction
    return _quantize(gross - fees, MONEY_PRECISION)


def calculate_pnl_percent(
    *,
    pnl: Decimal,
    entry_price: Decimal,
    quantity: Decimal,
    leverage: int = 1,
) -> Decimal:
    """PnL в процентах от вложенной маржи.

    Здесь плечо участвует — и только здесь. Позиция на $1000 с плечом 10
    требует $100 маржи, поэтому те же $50 прибыли дают 5% к позиции,
    но 50% к вложенным средствам.
    """
    if leverage < 1:
        raise CalculationError("Плечо не может быть меньше 1")
    position_value = entry_price * quantity
    if position_value <= ZERO:
        raise CalculationError("Размер позиции равен нулю")

    margin = position_value / Decimal(leverage)
    return _quantize(pnl / margin * Decimal(100), PERCENT_PRECISION)


def calculate_pnl_percent_of_balance(
    *, pnl: Decimal, account_balance: Decimal
) -> Decimal:
    """PnL как процент от депозита — та метрика, которой меряется просадка."""
    if account_balance <= ZERO:
        raise CalculationError("Баланс должен быть положительным")
    return _quantize(pnl / account_balance * Decimal(100), PERCENT_PRECISION)


# ---------------------------------------------------------------------------
# Риск и Risk/Reward
# ---------------------------------------------------------------------------


def stop_distance(
    *, entry_price: Decimal, stop_loss: Decimal, side: TradeSide
) -> Decimal:
    """Расстояние до стопа. Всегда положительное.

    Стоп с неправильной стороны от входа — не «отрицательный риск», а
    ошибка ввода: для лонга стоп обязан быть ниже цены входа.
    """
    distance = (entry_price - stop_loss) * side.direction
    if distance <= ZERO:
        expected = "ниже" if side is TradeSide.LONG else "выше"
        raise CalculationError(
            f"Для {side.value} стоп-лосс должен быть {expected} цены входа"
        )
    return distance


def calculate_risk_amount(
    *,
    entry_price: Decimal,
    stop_loss: Decimal,
    quantity: Decimal,
    side: TradeSide,
) -> Decimal:
    """Денежный риск: сколько будет потеряно при срабатывании стопа."""
    distance = stop_distance(entry_price=entry_price, stop_loss=stop_loss, side=side)
    return _quantize(distance * quantity, MONEY_PRECISION)


def calculate_risk_reward(
    *,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    side: TradeSide,
) -> Decimal:
    """Плановое соотношение риск/прибыль до входа в сделку.

    Возвращает число вида 2.5, что означает 1:2.5.
    """
    risk = stop_distance(entry_price=entry_price, stop_loss=stop_loss, side=side)
    reward = (take_profit - entry_price) * side.direction
    if reward <= ZERO:
        expected = "выше" if side is TradeSide.LONG else "ниже"
        raise CalculationError(
            f"Для {side.value} тейк-профит должен быть {expected} цены входа"
        )
    return _quantize(reward / risk, RR_PRECISION)


def calculate_realized_rr(
    *,
    entry_price: Decimal,
    stop_loss: Decimal,
    exit_price: Decimal,
    side: TradeSide,
) -> Decimal:
    """Фактический результат в единицах риска (R).

    Именно эта метрика сравнима между сделками разного размера: +2R на
    BTC и +2R на DOGE — одинаковый результат по качеству исполнения.
    Отрицательное значение означает убыток; -1R — стоп отработал ровно.
    """
    risk = stop_distance(entry_price=entry_price, stop_loss=stop_loss, side=side)
    result = (exit_price - entry_price) * side.direction
    return _quantize(result / risk, RR_PRECISION)


def calculate_position_size(
    *,
    account_balance: Decimal,
    risk_percent: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    side: TradeSide,
) -> PositionSizing:
    """Размер позиции от риска, а не от «сколько хочется взять».

    Цепочка: баланс → риск % → сумма риска → дистанция до стопа → объём.
    Именно короткий стоп на ретесте позволяет взять больший объём при том
    же риске в процентах — это ядро методологии.
    """
    if account_balance <= ZERO:
        raise CalculationError("Баланс должен быть положительным")
    if risk_percent <= ZERO or risk_percent > Decimal(100):
        raise CalculationError("Риск должен быть в диапазоне (0; 100]")

    risk_amount = account_balance * risk_percent / Decimal(100)
    distance = stop_distance(entry_price=entry_price, stop_loss=stop_loss, side=side)
    quantity = risk_amount / distance
    position_value = quantity * entry_price

    return PositionSizing(
        quantity=_quantize(quantity, Decimal("0.000000000001")),
        risk_amount=_quantize(risk_amount, MONEY_PRECISION),
        stop_distance=distance,
        stop_distance_percent=_quantize(
            distance / entry_price * Decimal(100), PERCENT_PRECISION
        ),
        position_value=_quantize(position_value, MONEY_PRECISION),
    )


@dataclass(frozen=True, slots=True)
class PositionSizing:
    quantity: Decimal
    risk_amount: Decimal
    stop_distance: Decimal
    stop_distance_percent: Decimal
    position_value: Decimal

    def required_margin(self, leverage: int) -> Decimal:
        if leverage < 1:
            raise CalculationError("Плечо не может быть меньше 1")
        return _quantize(self.position_value / Decimal(leverage), MONEY_PRECISION)


# ---------------------------------------------------------------------------
# Агрегация исполнений
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillData:
    """Минимальное представление исполнения для расчётов.

    Отвязано от модели БД намеренно: те же функции используются при
    импорте с биржи, когда объектов TradeFill ещё не существует.
    """

    fill_side: FillSide
    price: Decimal
    quantity: Decimal
    fee: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class TradeAggregate:
    entry_price: Decimal | None
    exit_price: Decimal | None
    entry_quantity: Decimal
    exit_quantity: Decimal
    open_quantity: Decimal
    total_fees: Decimal

    @property
    def is_fully_closed(self) -> bool:
        return self.entry_quantity > ZERO and self.open_quantity == ZERO


def weighted_average_price(fills: Sequence[FillData]) -> Decimal | None:
    """Средневзвешенная цена по объёму.

    Среднее арифметическое здесь было бы ошибкой: вход 0.1 BTC по 100000
    и 0.9 BTC по 90000 даёт среднюю 91000, а не 95000.
    """
    total_qty = sum((f.quantity for f in fills), ZERO)
    if total_qty <= ZERO:
        return None
    total_value = sum((f.price * f.quantity for f in fills), ZERO)
    return total_value / total_qty


def aggregate_fills(fills: Iterable[FillData]) -> TradeAggregate:
    """Сворачивает исполнения в агрегаты сделки.

    Вызывается после каждого добавления fill: поля Trade всегда остаются
    производными от исполнений, а не независимым источником данных.
    """
    fills = list(fills)
    entries = [f for f in fills if f.fill_side is FillSide.ENTRY]
    exits = [f for f in fills if f.fill_side is FillSide.EXIT]

    entry_qty = sum((f.quantity for f in entries), ZERO)
    exit_qty = sum((f.quantity for f in exits), ZERO)

    if exit_qty > entry_qty:
        raise CalculationError(
            f"Закрыто больше, чем открыто: {exit_qty} > {entry_qty}"
        )

    return TradeAggregate(
        entry_price=weighted_average_price(entries),
        exit_price=weighted_average_price(exits),
        entry_quantity=entry_qty,
        exit_quantity=exit_qty,
        open_quantity=entry_qty - exit_qty,
        total_fees=sum((f.fee for f in fills), ZERO),
    )


def realized_pnl_from_fills(fills: Iterable[FillData], side: TradeSide) -> Decimal:
    """PnL по закрытой части позиции.

    Считается только на закрытый объём: при частичной фиксации нельзя
    приписывать результат ещё открытой части.
    """
    agg = aggregate_fills(fills)
    if agg.entry_price is None or agg.exit_price is None or agg.exit_quantity <= ZERO:
        return ZERO

    return calculate_pnl(
        entry_price=agg.entry_price,
        exit_price=agg.exit_price,
        quantity=agg.exit_quantity,
        side=side,
        fees=agg.total_fees,
    )
