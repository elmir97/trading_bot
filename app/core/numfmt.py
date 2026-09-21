"""Нейтральное форматирование цен и чисел.

Вынесено из app.bot.formatting: гварды исполнения (app/execution) и воркеры
не должны зависеть от слоя бота, а показывать цену без хвоста в 12 знаков
хранимой точности им нужно так же, как экранам. app.bot.formatting
реэкспортирует эти функции — существующие импорты не меняются.
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
