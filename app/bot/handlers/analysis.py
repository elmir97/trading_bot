"""Экран «Анализ рынка» — техническая картина по выбранной монете.

02.10.2026: сигналы удалены — экран больше не ищет вход и не выносит
вердикт LONG/SHORT/WAIT. Только описание рынка (тренд, структура, RSI, ATR,
объём, уровни) и график. Полный экран (H1/H4/D1, funding, open interest) —
этап 2.
"""

from __future__ import annotations

import asyncio
import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.charting import render_analysis_chart
from app.analysis.context import MarketContext
from app.analysis.engine import AnalysisEngine
from app.analysis.structure import level_role
from app.bot.formatting import fmt_price, fmt_ratio
from app.bot.handlers.exchange import _describe, _market_cache
from app.bot.keyboards.main import MenuCallback, back_to, nav_row
from app.bot.messaging import edit_or_replace
from app.core.config import Settings
from app.core.logging import get_logger
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.exchanges.base import ExchangeError
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory

router = Router(name="analysis")
logger = get_logger(__name__)

# Таймфреймы экрана (бывший SCAN_TIMEFRAMES сканера).
MARKET_TIMEFRAMES = ("1h", "4h")

DISCLAIMER = "<i>ℹ️ Информация, не торговая рекомендация.</i>"


class AnalysisCB:
    MARKET = "an:market:"
    MARKET_TF = "an:mtf:"      # + SYMBOL:tf — переключение таймфрейма графика
    NOOP = "an:noop"           # кнопка текущего таймфрейма


async def _reply(event: Message | CallbackQuery, text: str, keyboard=None) -> None:  # type: ignore[no-untyped-def]
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await edit_or_replace(event.message, text, keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


async def _plan_symbols(session: AsyncSession, user_id: int) -> list[str]:
    plan = await UserRepository(session).get_trading_plan(user_id)
    return (plan.allowed_symbols if plan else []) or ["BTC-USDT", "ETH-USDT"]


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
# Текст экрана
# ---------------------------------------------------------------------------


def render_market(context: MarketContext, price_precision: int | None = None) -> str:
    """Сводка по рынку без торговых рекомендаций.

    price_precision — из SymbolInfo.price_precision биржи; None, если
    инструмент не удалось сопоставить.
    """
    lines = [
        f"<b>{context.symbol} · {context.timeframe.upper()}</b>",
        "",
        f"Цена: {fmt_price(context.price, price_precision)}",
        "",
        "<b>Тренд</b>",
    ]

    if context.ema200 is not None:
        side = "выше" if context.above_ema200 else "ниже"
        lines.append(f"Цена {side} EMA200 ({fmt_price(context.ema200, price_precision)})")
    if context.ema50 is not None:
        lines.append(f"EMA50: {fmt_price(context.ema50, price_precision)}")
    if context.ema20 is not None:
        lines.append(f"EMA20: {fmt_price(context.ema20, price_precision)}")

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
        lines.append(f"RSI: {fmt_ratio(context.rsi)}{state}")

    if context.atr is not None:
        lines.append(f"ATR: {fmt_price(context.atr, price_precision)}")
    if context.volume_ratio is not None:
        lines.append(f"Объём к среднему: {fmt_ratio(context.volume_ratio)}")

    if context.levels:
        lines += ["", "<b>Ближайшие уровни</b>"]
        nearby = sorted(
            context.levels, key=lambda level: level.distance_to(context.price)
        )[:5]
        for level in sorted(nearby, key=lambda level: level.price, reverse=True):
            role = level_role(level, context.price)
            kind = "сопротивление" if role == "resistance" else "поддержка"
            lines.append(
                f"{fmt_price(level.price, price_precision)} · {kind} · "
                f"касаний {level.touches}"
            )

    return "\n".join(lines)


def render_market_caption(context: MarketContext, price_precision: int | None = None) -> str:
    """Подпись к графику: сводка по рынку и пометка, что это не рекомендация."""
    return f"{render_market(context, price_precision)}\n\n{DISCLAIMER}"


# ---------------------------------------------------------------------------
# Экран
# ---------------------------------------------------------------------------


async def _engine(settings: Settings) -> tuple[AnalysisEngine, object]:
    client = ExchangeFactory(settings, None).public_client()  # type: ignore[arg-type]
    return AnalysisEngine(MarketDataService(client, _market_cache)), client


@router.callback_query(F.data == MenuCallback.ANALYSIS)
@router.message(Command("analysis"))
async def ask_market_symbol(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    symbols = await _plan_symbols(session, user.id)
    await _reply(
        event,
        "<b>Анализ рынка</b>\n\n"
        "Покажу график и техническую картину: тренд относительно EMA, "
        "структуру, RSI, ATR, объём и ближайшие уровни.\n\n"
        f"{DISCLAIMER}\n\n"
        "Выбери инструмент:",
        _symbols_keyboard(symbols, AnalysisCB.MARKET).as_markup(),
    )


# Лимит подписи к фото в Telegram — 1024 символа.
CAPTION_LIMIT = 1024

# (user_id, symbol) → идёт расчёт. Повторный тап по той же кнопке, пока
# рисуется график, не должен запускать второй рендер.
_in_flight: set[tuple[int, str]] = set()


def _failure_text(exc: Exception) -> str:
    """Сбой биржи объясняем, всё остальное — нейтрально (подробности в логе)."""
    if isinstance(exc, ExchangeError):
        return _describe(exc)
    return "⚠️ Не удалось построить анализ. Попробуй позже."


def market_keyboard(symbol: str, selected: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"• {tf.upper()}" if tf == selected else tf.upper(),
                callback_data=AnalysisCB.NOOP
                if tf == selected
                else f"{AnalysisCB.MARKET_TF}{symbol}:{tf}",
            )
            for tf in MARKET_TIMEFRAMES
        ]
    )
    builder.row(*nav_row(MenuCallback.ANALYSIS, with_menu=True))
    return builder.as_markup()


async def _set_status(message: Message, text: str) -> None:
    """Обновляет плейсхолдер «идёт работа»; сбой статуса не критичен."""
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=None)
        else:
            await message.edit_text(text)
    except TelegramAPIError:
        logger.debug("Не удалось обновить статус", exc_info=True)


async def _chat_action(callback: CallbackQuery, message: Message) -> None:
    if callback.bot is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await callback.bot.send_chat_action(message.chat.id, "upload_photo")


def _parse_symbol_tf(data: str, prefix: str) -> tuple[str, str]:
    symbol, _, timeframe = data.removeprefix(prefix).partition(":")
    if timeframe not in MARKET_TIMEFRAMES:
        timeframe = MARKET_TIMEFRAMES[-1]
    return symbol, timeframe


async def _show_market_screen(
    callback: CallbackQuery, user: User, settings: Settings, symbol: str, timeframe: str
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return

    key = (user.id, symbol)
    if key in _in_flight:
        await callback.answer("Уже считаю…")
        return
    _in_flight.add(key)
    try:
        await callback.answer()
        await _set_status(message, f"⏳ Загружаю данные по {symbol}…")

        engine, client = await _engine(settings)
        try:
            context = await engine.build_context(symbol, timeframe)
            symbol_info = await engine.get_symbol_info(symbol)
        except Exception as exc:
            logger.exception("Анализ рынка не удался", extra={"symbol": symbol})
            await _reply(callback, _failure_text(exc), back_to(MenuCallback.ANALYSIS))
            return
        finally:
            await client.close()  # type: ignore[attr-defined]

        keyboard = market_keyboard(symbol, timeframe)
        if context is None:
            await edit_or_replace(
                message, "Недостаточно рыночных данных для анализа.", keyboard
            )
            return

        price_precision = symbol_info.price_precision if symbol_info else None
        caption = render_market_caption(context, price_precision)
        await _set_status(message, "🖼 Рисую график…")
        await _chat_action(callback, message)
        # В отдельном потоке: matplotlib синхронный и тяжёлый, цикл
        # событий бота не должен вставать на время рендера.
        photo = await asyncio.to_thread(render_analysis_chart, context, price_precision)

        await _deliver(message, photo, caption, keyboard)
    finally:
        _in_flight.discard(key)


async def _deliver(
    message: Message, photo: bytes | None, caption: str, keyboard: InlineKeyboardMarkup
) -> None:
    """Показывает результат. График — не критичный путь: не построился —
    сводка всё равно уходит, текстом."""
    if photo is not None and len(caption) <= CAPTION_LIMIT:
        media = BufferedInputFile(photo, filename="analysis.png")
        try:
            if message.photo:
                await message.edit_media(
                    InputMediaPhoto(media=media, caption=caption), reply_markup=keyboard
                )
            else:
                await message.answer_photo(media, caption=caption, reply_markup=keyboard)
                await message.delete()
            return
        except TelegramAPIError:
            logger.exception("Не удалось отправить график анализа, шлём текстом")
    await edit_or_replace(message, caption, keyboard)


@router.callback_query(F.data.startswith(AnalysisCB.MARKET))
async def show_market(callback: CallbackQuery, user: User, settings: Settings) -> None:
    symbol = str(callback.data).removeprefix(AnalysisCB.MARKET)
    await _show_market_screen(callback, user, settings, symbol, MARKET_TIMEFRAMES[-1])


@router.callback_query(F.data.startswith(AnalysisCB.MARKET_TF))
async def switch_market_timeframe(
    callback: CallbackQuery, user: User, settings: Settings
) -> None:
    symbol, timeframe = _parse_symbol_tf(str(callback.data), AnalysisCB.MARKET_TF)
    await _show_market_screen(callback, user, settings, symbol, timeframe)


@router.callback_query(F.data == AnalysisCB.NOOP)
async def market_noop(callback: CallbackQuery) -> None:
    await callback.answer()
