"""Поиск точек входа и анализ рынка.

Сигнал показывается целиком: не только вердикт, но и все проверенные
условия. Пользователь должен понимать, почему бот говорит «ждём», —
иначе он либо перестанет доверять системе, либо начнёт верить ей
слепо, и оба исхода плохи.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import AnalysisEngine
from app.analysis.signals import MarketContext, Signal
from app.bot.handlers.exchange import _describe, _market_cache
from app.bot.keyboards.main import MenuCallback, back_to, nav_row
from app.bot.keyboards.trade import fmt_num
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.trading.enums import SignalDirection

router = Router(name="analysis")
logger = get_logger(__name__)


class AnalysisCB:
    SYMBOL = "an:sym:"
    TIMEFRAME = "an:tf:"
    SCAN = "an:scan:"
    MARKET = "an:market:"


async def _reply(event: Message | CallbackQuery, text: str, keyboard=None) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


async def _plan_symbols(session: AsyncSession, user_id: int) -> list[str]:
    plan = await UserRepository(session).get_trading_plan(user_id)
    return (plan.allowed_symbols if plan else []) or ["BTC-USDT", "ETH-USDT"]


async def _plan_timeframes(session: AsyncSession, user_id: int) -> list[str]:
    plan = await UserRepository(session).get_trading_plan(user_id)
    return (plan.allowed_timeframes if plan else []) or ["1h", "4h"]


def _symbols_keyboard(symbols: list[str], prefix: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for symbol in symbols:
        builder.button(
            text=symbol.replace("-USDT", ""), callback_data=f"{prefix}{symbol}"
        )
    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    )
    return builder


# ---------------------------------------------------------------------------
# Отрисовка сигнала
# ---------------------------------------------------------------------------


def render_signal(signal: Signal) -> str:
    """Текст сигнала.

    Формулировки намеренно осторожные: «сценарий актуален при
    выполнении условий», а не «цена вырастет». Система не может знать
    исход сделки, и говорить иначе значит вводить в заблуждение.
    """
    if signal.direction is SignalDirection.WAIT:
        lines = [
            f"⏸ <b>{signal.symbol} · {signal.timeframe.upper()}</b>",
            "",
            "<b>Входа сейчас нет.</b>",
            "",
            signal.note,
        ]
        if signal.conditions:
            lines += ["", "<b>Что проверено:</b>"]
            lines += [
                f"{'✅' if c.passed else '⬜'} {c.name}: {c.detail}"
                for c in signal.conditions
            ]
        return "\n".join(lines)

    icon = "🟢" if signal.direction is SignalDirection.LONG else "🔴"
    lines = [
        f"{icon} <b>{signal.symbol} — {signal.direction.value}</b>",
        f"<i>{signal.setup} · {signal.timeframe.upper()}</i>",
        "",
        "<b>Зона входа</b>",
        f"{fmt_num(signal.entry_zone_low)} – {fmt_num(signal.entry_zone_high)}",
        "",
        f"<b>Стоп-лосс:</b> {fmt_num(signal.stop_loss)}",
        f"<b>Цель:</b> {fmt_num(signal.take_profit_1)}",
        f"<b>RR:</b> 1:{fmt_num(signal.risk_reward)}",
        f"<b>Качество сетапа:</b> {signal.confidence}/10",
        "",
        f"<b>Подтверждение:</b> {signal.confirmation}",
        f"<b>Инвалидация:</b> {signal.invalidation}",
    ]

    if signal.note:
        lines.append(f"<i>{signal.note}</i>")

    lines += ["", "<b>Условия сетапа:</b>"]
    lines += [f"✅ {c.name}: {c.detail}" for c in signal.passed_conditions]

    lines += [
        "",
        "<i>Сценарий актуален при выполнении условий. Размер позиции "
        "рассчитай через «Риск» — от своего депозита и лимита риска.</i>",
    ]
    return "\n".join(lines)


def render_market(context: MarketContext) -> str:
    """Сводка по рынку без торговых рекомендаций."""
    lines = [
        f"<b>{context.symbol} · {context.timeframe.upper()}</b>",
        "",
        f"Цена: {fmt_num(context.price)}",
        "",
        "<b>Тренд</b>",
    ]

    if context.ema200 is not None:
        side = "выше" if context.above_ema200 else "ниже"
        lines.append(f"Цена {side} EMA200 ({fmt_num(context.ema200)})")
    if context.ema50 is not None:
        lines.append(f"EMA50: {fmt_num(context.ema50)}")
    if context.ema20 is not None:
        lines.append(f"EMA20: {fmt_num(context.ema20)}")

    lines += ["", f"Структура: {context.structure.value}"]
    if context.higher_structure is not None:
        lines.append(
            f"Старший ТФ ({context.higher_timeframe}): "
            f"{context.higher_structure.value}"
        )

    lines.append("")
    if context.rsi is not None:
        state = ""
        if context.rsi >= 70:
            state = " — зона перекупленности"
        elif context.rsi <= 30:
            state = " — зона перепроданности"
        lines.append(f"RSI: {fmt_num(context.rsi.quantize(Decimal('0.1')))}{state}")

    if context.atr is not None:
        lines.append(f"ATR: {fmt_num(context.atr.quantize(Decimal('0.0001')))}")
    if context.volume_ratio is not None:
        lines.append(
            f"Объём к среднему: {fmt_num(context.volume_ratio.quantize(Decimal('0.01')))}"
        )

    if context.levels:
        lines += ["", "<b>Ближайшие уровни</b>"]
        nearby = sorted(
            context.levels, key=lambda level: level.distance_to(context.price)
        )[:5]
        for level in sorted(nearby, key=lambda level: level.price, reverse=True):
            kind = "сопротивление" if level.is_resistance else "поддержка"
            lines.append(
                f"{fmt_num(level.price)} · {kind} · "
                f"касаний {level.touches}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Поиск входа
# ---------------------------------------------------------------------------


async def _engine(settings: Settings) -> tuple[AnalysisEngine, object]:
    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    return AnalysisEngine(MarketDataService(client, _market_cache)), client


@router.callback_query(F.data == MenuCallback.FIND_ENTRY)
@router.message(Command("signal"))
async def ask_signal_symbol(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    symbols = await _plan_symbols(session, user.id)
    builder = _symbols_keyboard(symbols, AnalysisCB.SYMBOL)
    builder.row(
        InlineKeyboardButton(
            text="🔎 Просканировать все", callback_data=f"{AnalysisCB.SCAN}ask"
        )
    )
    await _reply(
        event,
        "<b>Поиск точки входа</b>\n\n"
        "Проверю условия твоей методологии: положение относительно "
        "EMA200, структуру рынка, пробой уровня с ретестом или откат "
        "к EMA50, подтверждающий паттерн и RR.\n\n"
        "Выбери инструмент:",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith(AnalysisCB.SYMBOL))
async def ask_signal_timeframe(
    callback: CallbackQuery, session: AsyncSession, user: User
) -> None:
    symbol = str(callback.data).removeprefix(AnalysisCB.SYMBOL)
    timeframes = await _plan_timeframes(session, user.id)

    builder = InlineKeyboardBuilder()
    for tf in timeframes:
        builder.button(
            text=tf.upper(), callback_data=f"{AnalysisCB.TIMEFRAME}{symbol}:{tf}"
        )
    builder.adjust(3)
    builder.row(*nav_row(MenuCallback.FIND_ENTRY))

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"<b>{symbol}</b>\n\nТаймфрейм:", reply_markup=builder.as_markup()
        )
    await callback.answer()


@router.callback_query(F.data.startswith(AnalysisCB.TIMEFRAME))
async def run_signal(callback: CallbackQuery, settings: Settings) -> None:
    payload = str(callback.data).removeprefix(AnalysisCB.TIMEFRAME)
    symbol, _, timeframe = payload.partition(":")

    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"⏳ Анализирую {symbol} на {timeframe}…")
    await callback.answer()

    engine, client = await _engine(settings)
    try:
        signal = await engine.analyze(symbol, timeframe)
        text = render_signal(signal)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Анализ не удался", extra={"symbol": symbol})
        text = _describe(exc)
    finally:
        await client.close()  # type: ignore[attr-defined]

    await _reply(
        callback, text, back_to(f"{AnalysisCB.SYMBOL}{symbol}", with_menu=True)
    )


@router.callback_query(F.data.startswith(AnalysisCB.SCAN))
async def run_scan(
    callback: CallbackQuery, session: AsyncSession, user: User, settings: Settings
) -> None:
    symbols = await _plan_symbols(session, user.id)
    timeframes = await _plan_timeframes(session, user.id)
    timeframe = timeframes[-1] if timeframes else "4h"

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"⏳ Сканирую {len(symbols)} инструментов на {timeframe}…\n\n"
            f"<i>Это займёт до минуты.</i>"
        )
    await callback.answer()

    engine, client = await _engine(settings)
    try:
        signals = await engine.scan(symbols, timeframe)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Сканирование не удалось")
        await _reply(callback, _describe(exc), back_to(MenuCallback.FIND_ENTRY))
        return
    finally:
        await client.close()  # type: ignore[attr-defined]

    actionable = [s for s in signals if s.is_actionable]

    lines = [f"<b>Сканирование · {timeframe.upper()}</b>", ""]
    if actionable:
        lines.append(f"Найдено сетапов: {len(actionable)}")
        lines.append("")
        for signal in actionable:
            icon = "🟢" if signal.direction is SignalDirection.LONG else "🔴"
            lines.append(
                f"{icon} <b>{signal.symbol}</b> — {signal.setup}\n"
                f"Вход {fmt_num(signal.entry_zone_low)}–"
                f"{fmt_num(signal.entry_zone_high)} · "
                f"стоп {fmt_num(signal.stop_loss)} · "
                f"RR 1:{fmt_num(signal.risk_reward)} · "
                f"{signal.confidence}/10"
            )
    else:
        # Отсутствие сетапов — нормальный результат, а не сбой.
        # Методология прямо предупреждает: количество сигналов не
        # является целью системы.
        lines += [
            "Готовых сетапов нет.",
            "",
            "<i>Это нормально: условия методологии выполняются далеко "
            "не каждый день. Проверь позже или посмотри отдельные "
            "инструменты — там видно, каких условий не хватает.</i>",
        ]

    await _reply(callback, "\n".join(lines), back_to(MenuCallback.FIND_ENTRY))


# ---------------------------------------------------------------------------
# Анализ рынка
# ---------------------------------------------------------------------------


@router.callback_query(F.data == MenuCallback.ANALYSIS)
@router.message(Command("analysis"))
async def ask_market_symbol(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    symbols = await _plan_symbols(session, user.id)
    await _reply(
        event,
        "<b>Анализ рынка</b>\n\n"
        "Покажу тренд, структуру, индикаторы и ближайшие уровни "
        "без торговых рекомендаций.\n\n"
        "Выбери инструмент:",
        _symbols_keyboard(symbols, AnalysisCB.MARKET).as_markup(),
    )


@router.callback_query(F.data.startswith(AnalysisCB.MARKET))
async def show_market(
    callback: CallbackQuery, session: AsyncSession, user: User, settings: Settings
) -> None:
    symbol = str(callback.data).removeprefix(AnalysisCB.MARKET)
    timeframes = await _plan_timeframes(session, user.id)
    timeframe = timeframes[-1] if timeframes else "4h"

    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"⏳ Собираю данные по {symbol}…")
    await callback.answer()

    engine, client = await _engine(settings)
    try:
        context = await engine.build_context(symbol, timeframe)
        text = (
            render_market(context)
            if context is not None
            else "Недостаточно рыночных данных для анализа."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Анализ рынка не удался", extra={"symbol": symbol})
        text = _describe(exc)
    finally:
        await client.close()  # type: ignore[attr-defined]

    await _reply(callback, text, back_to(MenuCallback.ANALYSIS))
