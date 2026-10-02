"""Экран «Анализ рынка» — техническая картина по выбранной монете (этап 2).

Без направления сделки и без советов входа: тренд H1/H4/D1 (цена против
EMA50/200), структура, RSI, ATR %, объём, ближайшие уровни, funding и open
interest (app/analysis/market_overview.py) и график выбранного таймфрейма.
По флагу — пересказ цифр моделью (app/analysis/market_summary.py).

Данные — один снимок на монету (пять публичных запросов), кэш
OVERVIEW_TTL_SECONDS: переключение таймфрейма графика и повторный тап на
биржу не ходят.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
from datetime import UTC, datetime

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
from app.analysis.engine import AnalysisEngine, context_from_candles
from app.analysis.market_overview import (
    MIN_CANDLES,
    OVERVIEW_TIMEFRAMES,
    MarketSnapshot,
    build_overview,
    render_overview,
)
from app.analysis.market_summary import MarketSummaryService
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

# Таймфреймы графика — те же, что у обзора.
MARKET_TIMEFRAMES = OVERVIEW_TIMEFRAMES
DEFAULT_TIMEFRAME = "4h"
OVERVIEW_TTL_SECONDS = 60

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

# Лимит подписи к фото в Telegram — 1024 символа (len с HTML-тегами — с запасом).
CAPTION_LIMIT = 1024


def compose_caption(screen_text: str, summary: str | None) -> str:
    """Цифры, пересказ модели (если есть и влезает) и пометка."""
    plain_caption = f"{screen_text}\n\n{DISCLAIMER}"
    if not summary:
        return plain_caption
    with_summary = (
        f"{screen_text}\n\n<i>Кратко (пересказ цифр выше): {html.escape(summary)}</i>"
        f"\n\n{DISCLAIMER}"
    )
    return with_summary if len(with_summary) <= CAPTION_LIMIT else plain_caption


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
        "Покажу техническую картину по H1, H4 и D1: тренд относительно EMA50/200, "
        "структуру, RSI, ATR, объём, ближайшие уровни, funding и open interest — "
        "и график.\n\n"
        f"{DISCLAIMER}\n\n"
        "Выбери инструмент:",
        _symbols_keyboard(symbols, AnalysisCB.MARKET).as_markup(),
    )


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
        timeframe = DEFAULT_TIMEFRAME
    return symbol, timeframe


async def _snapshot(engine: AnalysisEngine, symbol: str) -> MarketSnapshot:
    """Снимок монеты из общего кэша рынка: повторный тап и смена таймфрейма
    графика в пределах OVERVIEW_TTL_SECONDS на биржу не ходят."""
    snapshot: MarketSnapshot = await _market_cache.get_or_fetch(
        f"overview:{symbol}", OVERVIEW_TTL_SECONDS, lambda: engine.market_snapshot(symbol)
    )
    return snapshot


async def _show_market_screen(
    callback: CallbackQuery,
    user: User,
    settings: Settings,
    symbol: str,
    timeframe: str,
    market_summary: MarketSummaryService | None = None,
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
            snapshot = await _snapshot(engine, symbol)
            symbol_info = await engine.get_symbol_info(symbol)
        except Exception as exc:
            logger.exception("Анализ рынка не удался", extra={"symbol": symbol})
            await _reply(callback, _failure_text(exc), back_to(MenuCallback.ANALYSIS))
            return
        finally:
            await client.close()  # type: ignore[attr-defined]

        keyboard = market_keyboard(symbol, timeframe)
        overview = build_overview(
            symbol, snapshot.candles, snapshot.premium, snapshot.open_interest
        )
        if overview is None:
            await edit_or_replace(
                message, "Недостаточно рыночных данных для анализа.", keyboard
            )
            return

        price_precision = symbol_info.price_precision if symbol_info else None
        screen_text = render_overview(overview, price_precision, datetime.now(UTC))
        summary = (
            await market_summary.summarize(user.id, screen_text)
            if market_summary is not None
            else None
        )
        caption = compose_caption(screen_text, summary)

        photo: bytes | None = None
        candles = snapshot.candles.get(timeframe) or []
        if len(candles) >= MIN_CANDLES:
            await _set_status(message, "🖼 Рисую график…")
            await _chat_action(callback, message)
            context = context_from_candles(symbol, timeframe, candles)
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
async def show_market(
    callback: CallbackQuery,
    user: User,
    settings: Settings,
    market_summary: MarketSummaryService | None = None,
) -> None:
    symbol = str(callback.data).removeprefix(AnalysisCB.MARKET)
    await _show_market_screen(
        callback, user, settings, symbol, DEFAULT_TIMEFRAME, market_summary
    )


@router.callback_query(F.data.startswith(AnalysisCB.MARKET_TF))
async def switch_market_timeframe(
    callback: CallbackQuery,
    user: User,
    settings: Settings,
    market_summary: MarketSummaryService | None = None,
) -> None:
    symbol, timeframe = _parse_symbol_tf(str(callback.data), AnalysisCB.MARKET_TF)
    await _show_market_screen(callback, user, settings, symbol, timeframe, market_summary)


@router.callback_query(F.data == AnalysisCB.NOOP)
async def market_noop(callback: CallbackQuery) -> None:
    await callback.answer()
