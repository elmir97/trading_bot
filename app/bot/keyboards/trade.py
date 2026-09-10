"""Клавиатуры и форматирование сделок."""

from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database.models.mistake import MistakeType
from app.database.models.strategy import Strategy
from app.database.models.trade import Trade
from app.trading.enums import TradeSide, TradeStatus


class TradeCB:
    SIDE = "trade:side:"
    SYMBOL = "trade:symbol:"
    STRATEGY = "trade:strategy:"
    TIMEFRAME = "trade:tf:"
    QTY_MODE = "trade:qtymode:"
    SKIP = "trade:skip"
    CONFIRM = "trade:confirm"
    FORCE_CONFIRM = "trade:force_confirm"
    CLOSE = "trade:close:"
    DETAILS = "trade:details:"
    ANNOTATE = "trade:annotate:"
    MISTAKE = "trade:mistake:"
    MISTAKES_DONE = "trade:mistakes_done"
    PAGE = "trade:page:"
    BACK = "trade:back"
    CLOSE_BACK = "trade:close_back"


def side_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🟢 LONG", callback_data=f"{TradeCB.SIDE}{TradeSide.LONG.value}"
        ),
        InlineKeyboardButton(
            text="🔴 SHORT", callback_data=f"{TradeCB.SIDE}{TradeSide.SHORT.value}"
        ),
    )
    return builder.as_markup()


def symbols_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    """Инструменты из торгового плана — по три в ряд.

    Ввод текстом остаётся доступен: план может не покрывать всё, а
    запрещать ввод редкого символа значит выталкивать сделку из журнала.
    """
    builder = InlineKeyboardBuilder()
    for symbol in symbols:
        builder.button(text=symbol.replace("-USDT", ""), callback_data=f"{TradeCB.SYMBOL}{symbol}")
    builder.adjust(3)
    return builder.as_markup()


def strategies_keyboard(strategies: list[Strategy]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for strategy in strategies:
        builder.button(
            text=strategy.name, callback_data=f"{TradeCB.STRATEGY}{strategy.id}"
        )
    builder.button(text="Без стратегии", callback_data=f"{TradeCB.STRATEGY}0")
    builder.adjust(1)
    return builder.as_markup()


def timeframe_keyboard(timeframes: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for tf in timeframes or ["1h", "4h"]:
        builder.button(text=tf.upper(), callback_data=f"{TradeCB.TIMEFRAME}{tf}")
    builder.button(text="Пропустить", callback_data=TradeCB.SKIP)
    builder.adjust(3)
    return builder.as_markup()


def quantity_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🧮 Рассчитать от риска", callback_data=f"{TradeCB.QTY_MODE}auto"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✍️ Ввести объём", callback_data=f"{TradeCB.QTY_MODE}manual"
        )
    )
    return builder.as_markup()


def skip_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Пропустить", callback_data=TradeCB.SKIP)
    )
    return builder.as_markup()


def open_trades_keyboard(trades: list[Trade]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for trade in trades:
        builder.button(
            text=f"{trade.symbol} {trade.side.value}",
            callback_data=f"{TradeCB.CLOSE}{trade.id}",
        )
    builder.button(text="◀️ В меню", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def mistakes_keyboard(
    mistakes: list[MistakeType], selected: set[int]
) -> InlineKeyboardMarkup:
    """Мультивыбор: галочка показывает уже отмеченные пункты."""
    builder = InlineKeyboardBuilder()
    for mistake in mistakes:
        mark = "✅ " if mistake.id in selected else ""
        builder.button(
            text=f"{mark}{mistake.title}",
            callback_data=f"{TradeCB.MISTAKE}{mistake.id}",
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="Готово", callback_data=TradeCB.MISTAKES_DONE)
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------


def fmt_num(value: Decimal | None) -> str:
    """Убирает незначащие нули: 2.0000 → 2, но 0.00001234 сохраняет.

    Формат :g для Decimal не годится — он оставляет хвост нулей
    (Decimal("2.0000") выводится как «2.0000»), из-за чего проценты
    и RR отображались как «2.0000%» и «1:2.0000».
    """
    if value is None:
        return "—"
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    # normalize() превращает Decimal("10.0000") в 1E+1 — возвращаем
    # обратно к обычной записи.
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


# Исторический псевдоним: цены форматируются тем же правилом.
fmt_price = fmt_num


def fmt_qty(value: Decimal | None) -> str:
    """Объём с точностью 8 знаков — как на бирже.

    Расчёт от риска даёт периодические дроби (200 / 60 = 3.333…), и
    показывать все 12 знаков хранимой точности бессмысленно: биржа всё
    равно округлит до шага лота.
    """
    if value is None:
        return "—"
    return fmt_num(value.quantize(Decimal("0.00000001")))


def fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value.quantize(Decimal('0.01')):f}"


def fmt_percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value.quantize(Decimal('0.01')):f}%"


def fmt_amount(value: Decimal | None) -> str:
    """Денежная величина без знака: риск, маржа, размер позиции.

    Отличается от fmt_money тем, что не ставит «+»: плюс уместен у PnL,
    где знак несёт смысл, но «Сумма риска: +200» читается как прибыль.
    """
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01')):f}"


def plural_trades(count: int) -> str:
    """Согласование числительного: 1 сделка, 2 сделки, 5 сделок."""
    if 11 <= count % 100 <= 14:
        return "сделок"
    last = count % 10
    if last == 1:
        return "сделка"
    if last in (2, 3, 4):
        return "сделки"
    return "сделок"


def trade_line(trade: Trade) -> str:
    """Однострочное представление для списков."""
    icon = "🟢" if trade.side is TradeSide.LONG else "🔴"
    if trade.status is TradeStatus.OPEN:
        return f"{icon} {trade.symbol} · вход {fmt_price(trade.entry_price)} · открыта"
    result_icon = "✅" if (trade.pnl or 0) > 0 else "❌"
    return (
        f"{result_icon} {trade.symbol} {trade.side.value} · "
        f"{fmt_money(trade.pnl)} USDT ({fmt_percent(trade.pnl_percent)})"
    )


def trade_card(trade: Trade) -> str:
    """Подробная карточка сделки."""
    lines = [
        f"<b>{trade.symbol} — {trade.side.label}</b>",
        "",
        f"Вход: {fmt_price(trade.entry_price)}",
    ]
    if trade.exit_price is not None:
        lines.append(f"Выход: {fmt_price(trade.exit_price)}")
    lines.append(f"Объём: {fmt_qty(trade.quantity)}")
    if trade.leverage > 1:
        lines.append(f"Плечо: {trade.leverage}x")
    if trade.stop_loss is not None:
        lines.append(f"Стоп: {fmt_price(trade.stop_loss)}")
    if trade.take_profit is not None:
        lines.append(f"Цель: {fmt_price(trade.take_profit)}")

    if trade.status is TradeStatus.CLOSED:
        lines += [
            "",
            f"<b>PnL: {fmt_money(trade.pnl)} USDT ({fmt_percent(trade.pnl_percent)})</b>",
        ]
        if trade.risk_reward is not None:
            lines.append(f"Результат: {fmt_num(trade.risk_reward)}R")
    else:
        lines.append("")
        lines.append("Статус: открыта")
        if trade.risk_percent is not None:
            lines.append(f"Риск: {fmt_num(trade.risk_percent)}%")
        if trade.risk_reward is not None:
            lines.append(f"Плановый RR: 1:{fmt_num(trade.risk_reward)}")

    if trade.fees:
        lines.append(f"Комиссии: {fmt_price(trade.fees)}")
    if trade.strategy is not None:
        lines.append(f"Стратегия: {trade.strategy.name}")
    if trade.timeframe:
        lines.append(f"Таймфрейм: {trade.timeframe.upper()}")
    if trade.entry_reason:
        lines.append(f"\nПричина входа: {trade.entry_reason}")
    if trade.exit_reason:
        lines.append(f"Причина выхода: {trade.exit_reason}")
    if trade.mistakes:
        titles = ", ".join(m.mistake_type.title for m in trade.mistakes)
        lines.append(f"\n⚠️ Ошибки: {titles}")

    return "\n".join(lines)
