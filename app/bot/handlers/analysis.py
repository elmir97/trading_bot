"""Поиск точек входа и анализ рынка.

Сигнал показывается целиком: не только вердикт, но и все проверенные
условия. Пользователь должен понимать, почему бот говорит «ждём», —
иначе он либо перестанет доверять системе, либо начнёт верить ей
слепо, и оба исхода плохи.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from dataclasses import dataclass
from decimal import Decimal

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
from app.analysis.classify import SCAN_TIMEFRAMES, classify_signal
from app.analysis.engine import AnalysisEngine
from app.analysis.signals import MarketContext, Signal, wait_signal
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
from app.trading.enums import SignalDirection, SignalLevel
from app.workers.notifier import notification_enabled

router = Router(name="analysis")
logger = get_logger(__name__)


class AnalysisCB:
    SYMBOL = "an:sym:"
    TIMEFRAME = "an:tf:"
    SCAN = "an:scan:"
    MARKET = "an:market:"
    MARKET_TF = "an:mtf:"      # + SYMBOL:tf — переключение таймфрейма графика
    MARKET_REF = "an:mref:"    # + SYMBOL:tf — полная справка отдельным сообщением
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


def render_signal(signal: Signal, price_precision: int | None = None) -> str:
    """Текст сигнала.

    Формулировки намеренно осторожные: «сценарий актуален при
    выполнении условий», а не «цена вырастет». Система не может знать
    исход сделки, и говорить иначе значит вводить в заблуждение.

    price_precision — из SymbolInfo.price_precision биржи; None, если
    инструмент не удалось сопоставить (тогда fmt_price сам выбирает
    точность по порядку величины).
    """
    if signal.direction is SignalDirection.WAIT:
        lines = [
            f"⏸ <b>{signal.symbol} · {signal.timeframe.upper()}</b>",
            "",
            "<b>Входа сейчас нет.</b>",
            "",
            _prices(signal.note, price_precision),
        ]
        if signal.conditions:
            lines += ["", "<b>Что проверено:</b>"]
            lines += [
                f"{'✅' if c.passed else '⬜'} {c.name}: {_prices(c.detail, price_precision)}"
                for c in signal.conditions
            ]
        return "\n".join(lines)

    icon = "🟢" if signal.direction is SignalDirection.LONG else "🔴"
    lines = [
        f"{icon} <b>{signal.symbol} — {signal.direction.value}</b>",
        f"<i>{signal.setup} · {signal.timeframe.upper()}</i>",
        "",
        "<b>Зона входа</b>",
        f"{fmt_price(signal.entry_zone_low, price_precision)} – "
        f"{fmt_price(signal.entry_zone_high, price_precision)}",
        "",
        f"<b>Стоп-лосс:</b> {fmt_price(signal.stop_loss, price_precision)}",
        f"<b>Цель:</b> {fmt_price(signal.take_profit_1, price_precision)}",
        f"<b>RR:</b> 1:{fmt_ratio(signal.risk_reward)}",
        f"<b>Качество сетапа:</b> {signal.confidence}/10",
        "",
        f"<b>Подтверждение:</b> {signal.confirmation}",
        f"<b>Инвалидация:</b> {_prices(signal.invalidation, price_precision)}",
    ]

    if signal.note:
        lines.append(f"<i>{_prices(signal.note, price_precision)}</i>")

    lines += ["", "<b>Условия сетапа:</b>"]
    lines += [
        f"✅ {c.name}: {_prices(c.detail, price_precision)}" for c in signal.passed_conditions
    ]

    lines += [
        "",
        "<i>Сценарий актуален при выполнении условий. Размер позиции "
        "рассчитай через «Риск» — от своего депозита и лимита риска.</i>",
    ]
    return "\n".join(lines)


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
            kind = "сопротивление" if level.is_resistance else "поддержка"
            lines.append(
                f"{fmt_price(level.price, price_precision)} · {kind} · "
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
        symbol_info = await engine.get_symbol_info(symbol)
        text = render_signal(
            signal, symbol_info.price_precision if symbol_info else None
        )
    except Exception as exc:
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
        # Один запрос списка инструментов на всё сканирование (кэш общий,
        # ключ один на биржу), а не по запросу на каждый найденный сетап:
        # словарь строится локально, дальше — только обращения к памяти.
        try:
            symbol_precisions = {
                info.symbol: info.price_precision for info in await engine.get_symbols()
            }
        except Exception:
            logger.warning("Не удалось получить точность инструментов для сканирования")
            symbol_precisions = {}
    except Exception as exc:
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
            precision = symbol_precisions.get(signal.symbol)
            lines.append(
                f"{icon} <b>{signal.symbol}</b> — {signal.setup}\n"
                f"Вход {fmt_price(signal.entry_zone_low, precision)}–"
                f"{fmt_price(signal.entry_zone_high, precision)} · "
                f"стоп {fmt_price(signal.stop_loss, precision)} · "
                f"RR 1:{fmt_ratio(signal.risk_reward)} · "
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
        "Покажу график и вердикт по детекторам сканера (H1 и H4): "
        "сетап найден, формируется или чего не хватает. Тренд, RSI/ATR "
        "и уровни — по кнопке «Справка».\n\n"
        "Выбери инструмент:",
        _symbols_keyboard(symbols, AnalysisCB.MARKET).as_markup(),
    )


# Лимит подписи к фото в Telegram — 1024 символа; берём с запасом.
CAPTION_LIMIT = 1024
_CLIP_DETAIL = 200

# (user_id, symbol) → идёт расчёт. Повторный тап по той же кнопке, пока
# рисуется график, не должен запускать второй рендер.
_in_flight: set[tuple[int, str]] = set()


@dataclass(frozen=True, slots=True)
class TimeframeResult:
    timeframe: str
    context: MarketContext | None
    signal: Signal


async def analyze_timeframes(
    engine: AnalysisEngine, symbol: str
) -> dict[str, TimeframeResult]:
    """Те же build_context + evaluate, что и у сканера, по его таймфреймам.

    Ничего не пишется в БД: разовый расчёт не имеет signal_id, TTL и дедупа
    (см. docstring render_verdict).
    """
    results: dict[str, TimeframeResult] = {}
    for timeframe in SCAN_TIMEFRAMES:
        context = await engine.build_context(symbol, timeframe)
        signal = (
            engine.evaluate(context)
            if context is not None
            else wait_signal(
                symbol, timeframe, "Недостаточно рыночных данных для анализа."
            )
        )
        results[timeframe] = TimeframeResult(timeframe, context, signal)
    return results


def _failure_text(exc: Exception) -> str:
    """Сбой биржи объясняем, всё остальное — нейтрально (подробности в логе)."""
    if isinstance(exc, ExchangeError):
        return _describe(exc)
    return "⚠️ Не удалось построить анализ. Попробуй позже."


def _clip(text: str, limit: int = _CLIP_DETAIL) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


# Детекторы (app/analysis/setups.py) вшивают цены в текст условий как {x:.4f}:
# «Расстояние до EMA50: 102.7574», «EMA50 (2529.4926)». Такие хвосты Telegram
# принимает за номера и подсвечивает ссылками. Тексты детекторов читает и
# сканер (signal.note входит в fingerprint FORMING), поэтому источник не
# трогаем, а на выводе экрана прогоняем через fmt_price.
_RAW_PRICE = re.compile(r"(?<![\d.])\d+\.\d{4}(?!\d)")


def _prices(text: str, precision: int | None) -> str:
    """Числа с четырьмя знаками из текста детектора → fmt_price по точности символа."""
    return _RAW_PRICE.sub(lambda m: fmt_price(Decimal(m.group()), precision), text)


def _missing(signal: Signal, precision: int | None = None) -> str:
    """Какого условия не хватает: невыполненное условие, а если условий
    нет (мало истории и т.п.) — пояснение детектора."""
    failed = signal.failed_conditions
    if failed:
        text = f"{failed[0].name} — {failed[0].detail}"
        return _esc(_clip(_prices(text, precision)))
    return _esc(_clip(_prices(signal.note or "условия не выполнены", precision)))


def _verdict_line(signal: Signal, precision: int | None = None) -> str:
    """Короткий вердикт одного таймфрейма."""
    if signal.is_actionable:
        icon = "🟢" if signal.direction is SignalDirection.LONG else "🔴"
        return f"{icon} {signal.direction.value} · сетап найден ({_esc(signal.setup)})"
    if classify_signal(signal) is SignalLevel.FORMING:
        return "🌱 формируется · не хватает подтверждающей свечи"
    failed = signal.failed_conditions
    reason = (
        _esc(failed[0].name)
        if failed
        else _esc(_clip(_prices(signal.note or "нет данных", precision), 80))
    )
    return f"⏸ WAIT · не хватает: {reason}"


def render_verdict(
    symbol: str,
    results: dict[str, TimeframeResult],
    selected: str,
    price_precision: int | None = None,
    ready_notifications: bool = True,
) -> str:
    """Подпись к графику: вердикт по каждому ТФ и детали выбранного.

    Формулировки намеренно не совпадают с уведомлениями сканера: здесь нет
    слов «READY» и «Сетап готов». У сканера сигнал имеет signal_id, TTL,
    дедуп и запись в signals, а под ним стоит кнопка входа. Разовый расчёт
    по кнопке ничего из этого не имеет — поэтому и кнопки входа нет.
    """
    lines = [f"<b>{_esc(symbol)}</b> · график {selected.upper()}", ""]
    for timeframe, result in results.items():
        lines.append(
            f"<b>{timeframe.upper()}:</b> {_verdict_line(result.signal, price_precision)}"
        )

    signal = results[selected].signal
    lines.append("")
    if signal.is_actionable:
        lines += [
            f"Вход: {fmt_price(signal.entry_zone_low, price_precision)} – "
            f"{fmt_price(signal.entry_zone_high, price_precision)}",
            f"Стоп: {fmt_price(signal.stop_loss, price_precision)} · "
            f"Цель: {fmt_price(signal.take_profit_1, price_precision)}",
            f"RR 1:{fmt_ratio(signal.risk_reward)} · Качество: {signal.confidence}/10",
        ]
        if signal.invalidation:
            lines.append(f"<i>{_esc(_clip(_prices(signal.invalidation, price_precision)))}</i>")
    else:
        lines.append(f"<b>Не хватает:</b> {_missing(signal, price_precision)}")
        if signal.note:
            lines.append(f"<i>{_esc(_clip(_prices(signal.note, price_precision)))}</i>")

    lines += [
        "",
        "<i>Разовый расчёт по кнопке, не сигнал сканера. Карточка входа "
        "приходит только от сканера.</i>",
    ]
    if not ready_notifications:
        lines.append(
            "<i>Уведомления о готовых сетапах у тебя выключены — карточка "
            "входа не придёт.</i>"
        )
    return "\n".join(lines)


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
            for tf in SCAN_TIMEFRAMES
        ]
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 Справка",
            callback_data=f"{AnalysisCB.MARKET_REF}{symbol}:{selected}",
        )
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
    if timeframe not in SCAN_TIMEFRAMES:
        timeframe = SCAN_TIMEFRAMES[-1]
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
            results = await analyze_timeframes(engine, symbol)
            symbol_info = await engine.get_symbol_info(symbol)
        except Exception as exc:
            logger.exception("Анализ рынка не удался", extra={"symbol": symbol})
            await _reply(callback, _failure_text(exc), back_to(MenuCallback.ANALYSIS))
            return
        finally:
            await client.close()  # type: ignore[attr-defined]

        price_precision = symbol_info.price_precision if symbol_info else None
        caption = render_verdict(
            symbol,
            results,
            timeframe,
            price_precision,
            notification_enabled(user.settings, "setup_ready"),
        )
        keyboard = market_keyboard(symbol, timeframe)

        chosen = results[timeframe]
        photo: bytes | None = None
        if chosen.context is not None:
            await _set_status(message, "🖼 Рисую график…")
            await _chat_action(callback, message)
            # В отдельном потоке: matplotlib синхронный и тяжёлый, цикл
            # событий бота не должен вставать на время рендера.
            photo = await asyncio.to_thread(
                render_analysis_chart, chosen.context, chosen.signal, price_precision
            )

        await _deliver(message, photo, caption, keyboard)
    finally:
        _in_flight.discard(key)


async def _deliver(
    message: Message, photo: bytes | None, caption: str, keyboard: InlineKeyboardMarkup
) -> None:
    """Показывает результат. График — не критичный путь: не построился —
    вердикт всё равно уходит, текстом (как и в уведомлениях сканера)."""
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
    await _show_market_screen(callback, user, settings, symbol, SCAN_TIMEFRAMES[-1])


@router.callback_query(F.data.startswith(AnalysisCB.MARKET_TF))
async def switch_market_timeframe(
    callback: CallbackQuery, user: User, settings: Settings
) -> None:
    symbol, timeframe = _parse_symbol_tf(str(callback.data), AnalysisCB.MARKET_TF)
    await _show_market_screen(callback, user, settings, symbol, timeframe)


@router.callback_query(F.data == AnalysisCB.NOOP)
async def market_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith(AnalysisCB.MARKET_REF))
async def show_market_reference(callback: CallbackQuery, settings: Settings) -> None:
    """Прежняя справка (тренд, структура, RSI/ATR, уровни) — отдельным
    сообщением, чтобы не выталкивать график: в подпись к фото она не влезает."""
    symbol, timeframe = _parse_symbol_tf(str(callback.data), AnalysisCB.MARKET_REF)
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    engine, client = await _engine(settings)
    try:
        context = await engine.build_context(symbol, timeframe)
        if context is None:
            text = "Недостаточно рыночных данных для анализа."
        else:
            info = await engine.get_symbol_info(symbol)
            text = render_market(context, info.price_precision if info else None)
    except Exception as exc:
        logger.exception("Справка по рынку не удалась", extra={"symbol": symbol})
        text = _failure_text(exc)
    finally:
        await client.close()  # type: ignore[attr-defined]

    await callback.message.answer(text)
