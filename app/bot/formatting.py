"""Форматирование чисел для вывода в боте.

Разделение принципиальное: расчёты хранят Decimal полной точности
(деление, усреднение — без округления), округление происходит только
здесь, в момент показа пользователю. Раньше это было размазано по
местам вызова (RSI/ATR/объём квантовались перед fmt_num на экране
анализа, а цена, EMA и уровни — нет), из-за чего в бот утекали хвосты
вида «74077.859884955571169409993».

Все quantize() ниже задают rounding явно: контекст Decimal по
умолчанию — ROUND_HALF_EVEN (банковское округление до чётной цифры),
и без явного параметра 2.505 округлился бы в 2.50, а не в 2.51.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def fmt_num(value: Decimal | None) -> str:
    """Убирает незначащие нули: 2.0000 → 2, но 0.00001234 сохраняет.

    Формат :g для Decimal не годится — он оставляет хвост нулей
    (Decimal("2.0000") выводится как «2.0000»), из-за чего проценты
    и RR отображались как «2.0000%» и «1:2.0000».

    Только для целых и служебных мест: сама по себе не округляет,
    только приводит к канонической записи. Для цен/долей — fmt_price
    и fmt_ratio.
    """
    if value is None:
        return "—"
    normalized = value.normalize()
    _sign, _digits, exponent = normalized.as_tuple()
    # normalize() превращает Decimal("10.0000") в 1E+1 — возвращаем
    # обратно к обычной записи.
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def _fallback_price_precision(value: Decimal) -> int:
    """Знаков после запятой, когда price_precision символа недоступен.

    Порядок величины цены и разумная точность связаны: у цены за 70000
    два знака что-то значат, у цены за 0.03 — нет. Границы взяты по
    типичным парам (BTC ~10^4-10^5, альты ~1, мелкие альты <1).
    """
    magnitude = abs(value)
    if magnitude >= 1000:
        return 2
    if magnitude >= 1:
        return 4
    return 6


def fmt_price(value: Decimal | None, precision: int | None = None) -> str:
    """Цена/EMA/ATR/уровень — округление по price_precision символа.

    precision=None — работаем без SymbolInfo (публичный экран, символ
    не с биржи и т.п.): знаков после запятой столько, сколько разумно
    для порядка величины (см. _fallback_price_precision).
    """
    if value is None:
        return "—"
    digits = precision if precision is not None else _fallback_price_precision(value)
    step = Decimal(1).scaleb(-digits)
    rounded = value.quantize(step, rounding=ROUND_HALF_UP)
    return fmt_num(rounded)


def fmt_qty(value: Decimal | None, precision: int = 8) -> str:
    """Объём — точность по quantity_precision символа.

    Дефолт 8 знаков сохраняет прежнее поведение там, где SymbolInfo не
    под рукой (например, объём сделки из журнала): расчёт от риска даёт
    периодические дроби (200 / 60 = 3.333…), и показывать все 12 знаков
    хранимой точности бессмысленно — биржа всё равно округлит до шага
    лота.
    """
    if value is None:
        return "—"
    step = Decimal(1).scaleb(-precision)
    rounded = value.quantize(step, rounding=ROUND_HALF_UP)
    return fmt_num(rounded)


def fmt_ratio(value: Decimal | None) -> str:
    """RR, RSI, объём к среднему, win rate, profit factor — округление
    внутри функции (2 знака), а не по месту вызова.

    Раньше RSI/ATR/объём квантовались вручную перед каждым fmt_num, и
    цена/EMA/уровни остались без этого шага только потому, что для них
    такого вызова никто не добавил — округление не должно зависеть от
    того, вспомнил ли автор конкретного экрана его вставить.
    """
    if value is None:
        return "—"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return fmt_num(rounded)


def fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{sign}{rounded:f}"


def fmt_percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{sign}{rounded:f}%"


def fmt_amount(value: Decimal | None) -> str:
    """Денежная величина без знака: риск, маржа, размер позиции.

    Отличается от fmt_money тем, что не ставит «+»: плюс уместен у PnL,
    где знак несёт смысл, но «Сумма риска: +200» читается как прибыль.
    """
    if value is None:
        return "—"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:f}"
