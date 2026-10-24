"""Экран «💼 Позиции» (этап 3): позиции с биржи и связь с журналом.

Позиции — с биржи, счёт из настроек (user.settings.active_exchange_mode),
как у остальных экранов биржи: get_positions + get_open_orders без символа —
два запроса. Стоп и тейк — из openOrders (app/execution/position_view.py).
Позиция без сделки в журнале — кнопка «📥 В журнал»: в журнал заносится только
ТЕКУЩАЯ позиция — исполнения её входа за IMPORT_DAYS дней
(HistoryImporter.import_open_position); история целиком — /import. Ниже —
открытые сделки только из журнала (без позиции на бирже): их закрывают как
раньше, вводом цены выхода (app/bot/handlers/trades.py).

Сам экран биржу только читает. На позицию — одна кнопка «⚙️ XRP LONG»: экран
действий этой позиции (стоп в безубыток, стоп, тейк, 25%/50%, закрыть всё),
кнопки ведут в карточки подтверждения (app/bot/handlers/position_actions.py,
этап 4).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import ExchangeCB, _describe, _market_cache
from app.bot.handlers.position_actions import SIDE_BY_CODE, SIDE_CODE, action_buttons
from app.bot.keyboards.main import MenuCallback
from app.bot.keyboards.positions import (
    IMPORT_ORPHAN,
    ORPHAN_REFUSED,
    orphan_done_keyboard,
    orphan_keyboard,
)
from app.bot.keyboards.trade import TradeCB
from app.bot.messaging import edit_or_replace
from app.bot.opening_result import POSITION_ACTIONS
from app.core.config import Settings
from app.core.locks import LockBusyError, RedisLock, position_lock_key
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeAuthError, ExchangeClient, ExchangeError, Position
from app.execution.opening.recovery import opening_in_flight
from app.execution.position_view import (
    PositionView,
    build_views,
    journal_only,
    render_journal_trade,
    render_position,
)
from app.market.data import MarketDataService
from app.services.exchange_factory import ExchangeFactory
from app.services.import_service import HistoryImporter
from app.trading.enums import TradeSide
from app.trading.risk import tz_offset_for

router = Router(name="positions")
logger = get_logger(__name__)

IMPORT_DAYS = 30
# Лок позиции на время «📥 В журнал»: история исполнений за IMPORT_DAYS — до
# десятка GET; лок снимается сразу по завершении.
IMPORT_LOCK_TTL_SECONDS = 120
ALERT_LIMIT = 200   # всплывашка answerCallbackQuery — до 200 символов


class PositionsCB:
    REFRESH = "pos:refresh"
    IMPORT = "pos:imp:"    # + SYMBOL:L|S
    ACTIONS = POSITION_ACTIONS   # + SYMBOL:L|S


def _short(symbol: str) -> str:
    return symbol.replace("-USDT", "")


def _position_ref(symbol: str, side: TradeSide) -> str:
    return f"{symbol}:{SIDE_CODE[side]}"


def _parse_ref(data: str, prefix: str) -> tuple[str, TradeSide] | None:
    symbol, _, code = data.removeprefix(prefix).rpartition(":")
    if not symbol or code not in SIDE_BY_CODE:
        return None
    return symbol, SIDE_BY_CODE[code]


def positions_keyboard(views: list[PositionView], journal: list[Trade]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for view in views:
        # Одна строка на позицию: действия — на своём экране (подписи
        # полностью, по две в ряд), здесь только вход в него и «В журнал».
        p = view.position
        ref = _position_ref(p.symbol, p.side)
        row = [InlineKeyboardButton(
            text=f"⚙️ {_short(p.symbol)} {p.side.value}",
            callback_data=f"{PositionsCB.ACTIONS}{ref}",
        )]
        if view.trade is None:
            row.append(InlineKeyboardButton(
                text=f"📥 В журнал: {_short(p.symbol)} {p.side.value}",
                callback_data=f"{PositionsCB.IMPORT}{ref}",
            ))
        builder.row(*row)
    for trade in journal:
        builder.row(InlineKeyboardButton(
            text=f"{trade.symbol} {trade.side.value} #{trade.id}",
            callback_data=f"{TradeCB.CLOSE}{trade.id}",
        ))
    builder.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=PositionsCB.REFRESH),
        InlineKeyboardButton(text="◀️ В меню", callback_data=MenuCallback.MAIN),
    )
    return builder.as_markup()


def actions_keyboard(symbol: str, side: TradeSide) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(text=text, callback_data=data)
        for text, data in action_buttons(symbol, side)
    ]
    for i in range(0, len(buttons), 2):
        builder.row(*buttons[i:i + 2])
    builder.row(InlineKeyboardButton(text="◀️ К позициям", callback_data=PositionsCB.REFRESH))
    return builder.as_markup()


def render_actions(view: PositionView, precision: int | None) -> str:
    p = view.position
    return "\n".join([
        f"<b>⚙️ Действия · {p.symbol} {p.side.value}</b>",
        "",
        render_position(view, precision),
        "",
        "<i>Каждое действие — карточка с расчётом и «Да»/«Нет», без «Да» на биржу "
        "ничего не уходит.</i>",
    ])


def render_screen(
    *,
    mode_label: str,
    views: list[PositionView] | None,
    exchange_note: str | None,
    journal: list[Trade],
    precision: dict[str, int],
    note: str | None = None,
) -> str:
    lines = [f"<b>💼 Позиции · {mode_label}</b>"]
    if note:
        lines += ["", note]
    lines.append("")
    if exchange_note is not None:
        lines.append(exchange_note)
    elif not views:
        lines.append("На бирже открытых позиций нет.")
    else:
        for view in views:
            lines += [render_position(view, precision.get(view.position.symbol)), ""]
        if lines[-1] == "":
            lines.pop()
    if journal:
        lines += ["", "<b>Только в журнале</b>"]
        lines += [render_journal_trade(t) for t in journal]
        lines += ["", "<i>Сделку журнала закрывают вводом цены выхода — кнопка ниже.</i>"]
    return "\n".join(lines)


async def _entry_balance(
    client: ExchangeClient, position: Position, user_id: int
) -> Decimal | None:
    """Баланс на входе для «📥 В журнал» (03.10.2026, блокер live Б1): equity
    счёта минус нереализованный PnL этой позиции — баланс, каким он был без
    неё. Без баланса сделка не входит в процент лимитов убытка (способ
    «от баланса на входе каждой сделки»). Не получен — None и WARNING;
    сделку всё равно заносим, с пометкой в ответе."""
    try:
        equity = (await client.get_balance()).equity
    except ExchangeError:
        logger.warning(
            "В журнал: баланс счёта не получен — сделка без баланса на входе",
            extra={"user_id": user_id, "symbol": position.symbol},
        )
        return None
    return equity - position.unrealized_pnl


async def _load(
    session: AsyncSession, user: User, settings: Settings, cipher: SecretCipher
) -> tuple[list[PositionView] | None, str | None, list[Trade], dict[str, int]]:
    """(позиции, причина, почему их нет, сделки только из журнала, точность цен)."""
    open_trades = await TradeRepository(session).list_open(user.id)
    try:
        client = await ExchangeFactory(settings, cipher).for_user(
            session, user.id, mode=user.settings.active_exchange_mode
        )
    except ExchangeAuthError:
        return None, "🔑 Ключи биржи не подключены — показан только журнал.", open_trades, {}
    try:
        positions = await client.get_positions()
        orders = await client.get_open_orders() if positions else []
        precision: dict[str, int] = {}
        if positions:
            infos = await MarketDataService(client, _market_cache).get_symbols()
            precision = {i.symbol: i.price_precision for i in infos}
    except ExchangeError as exc:
        logger.warning("Позиции с биржи не получены", extra={"user_id": user.id})
        return None, _describe(exc), open_trades, {}
    finally:
        await client.close()
    views = build_views(
        positions, orders, open_trades, account_mode=user.settings.active_exchange_mode
    )
    return views, None, journal_only(open_trades, views), precision


async def _show(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    note: str | None = None,
) -> None:
    views, exchange_note, journal, precision = await _load(session, user, settings, cipher)
    if exchange_note is not None:
        # Без биржи все открытые сделки — «только журнал».
        journal = await TradeRepository(session).list_open(user.id)
    text = render_screen(
        mode_label=user.settings.active_exchange_mode.label,
        views=views,
        exchange_note=exchange_note,
        journal=journal,
        precision=precision,
        note=note,
    )
    keyboard = positions_keyboard(views or [], journal)
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await edit_or_replace(event.message, text, keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(
    F.data.in_({MenuCallback.OPEN_POSITIONS, ExchangeCB.POSITIONS, PositionsCB.REFRESH})
)
@router.message(Command("open"))
async def show_positions(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    # Откуда экран: команда /open или кнопка (данные кнопки) — экран без заметки
    # приходит только этими путями (разбор 17.10).
    trigger = "/open" if isinstance(event, Message) else (event.data or "")
    logger.info("Экран позиций", extra={"trigger": trigger})
    await _show(event, session, user, settings, cipher)


@router.callback_query(F.data.startswith(PositionsCB.ACTIONS))
async def show_actions(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    ref = _parse_ref(str(callback.data), PositionsCB.ACTIONS)
    if ref is None:
        await callback.answer("Кнопка устарела — открой «Позиции» заново.", show_alert=True)
        return
    await open_actions(callback, session, user, settings, cipher, *ref)


async def open_actions(
    event: Message | CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    symbol: str,
    side: TradeSide,
) -> None:
    """Экран действий одной позиции — с биржи заново: позиция могла закрыться.
    Кнопка «⚙️» (правит сообщение) и ссылка из Mini App /start pos-… (новое)."""
    views, exchange_note, _, precision = await _load(session, user, settings, cipher)
    view = next(
        (v for v in views or [] if v.position.symbol == symbol and v.position.side == side),
        None,
    )
    if view is None:
        note = exchange_note or f"Позиции {symbol} {side.value} на бирже уже нет."
        await _show(event, session, user, settings, cipher, note=note)
        return
    text = render_actions(view, precision.get(symbol))
    keyboard = actions_keyboard(symbol, side)
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await edit_or_replace(event.message, text, keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)


@dataclass(frozen=True, slots=True)
class Imported:
    """Итог «📥 В журнал»: note — строка для экрана «Позиции»; trade_id — сделка
    создана; warning — оговорка к созданной; refused_early — отказ до работы
    (лок, открытие в полёте): всплывашка, экран не перерисовывать."""

    note: str
    trade_id: int | None = None
    warning: str | None = None
    refused_early: bool = False


async def import_open_position(
    session: AsyncSession, user: User, settings: Settings, cipher: SecretCipher, redis: Any,
    symbol: str, side: TradeSide,
) -> Imported:
    """Общая для экрана «Позиции» и сообщения сверки ORPHAN_POSITION (п.3,
    24.10): текущая позиция в журнал под локом позиции (тот же exec:lock, что
    у действий этапа 4 — сверка пропускает цикл, второе нажатие получает
    отказ): двойное нажатие — один импорт."""
    label = f"{symbol} {side.value}"
    if await opening_in_flight(session, user.id, symbol, side):
        # 05.10.2026: позиция открывается из бота (лимит исполнен частично,
        # идёт защита) — сделку запишет само открытие, иначе будет дубль.
        return Imported(
            f"{label} сейчас открывается из бота — сделку запишет само открытие.",
            refused_early=True,
        )
    try:
        async with RedisLock(redis, position_lock_key(user.id, symbol, side.value),
                             IMPORT_LOCK_TTL_SECONDS):
            return await _import_locked(session, user, settings, cipher, symbol, side)
    except LockBusyError:
        return Imported(f"{label}: с позицией уже идёт действие — подожди и повтори.",
                        refused_early=True)


async def _import_locked(
    session: AsyncSession, user: User, settings: Settings, cipher: SecretCipher,
    symbol: str, side: TradeSide,
) -> Imported:
    label = f"{symbol} {side.value}"
    try:
        client = await ExchangeFactory(settings, cipher).for_user(
            session, user.id, mode=user.settings.active_exchange_mode
        )
    except ExchangeAuthError as exc:
        return Imported(_describe(exc))
    end = datetime.now(UTC)
    warning = None
    trade_id = None
    try:
        position = next(
            (p for p in await client.get_positions() if p.symbol == symbol and p.side == side),
            None,
        )
        if position is None:
            note = f"📥 {label}: позиции на бирже уже нет — в журнал ничего не занесено."
        else:
            balance = await _entry_balance(client, position, user.id)
            outcome = await HistoryImporter(
                client, TradeRepository(session), user.id,
                account_mode=user.settings.active_exchange_mode,
                journal_cutoff=user.settings.journal_cutoff_at,
                tz_offset=tz_offset_for(user.settings.timezone),
            ).import_open_position(
                end - timedelta(days=IMPORT_DAYS), end,
                symbol=symbol, side=side, quantity=position.quantity,
                position_id=position.position_id, account_balance=balance,
            )
            if outcome.trade is not None:
                trade_id = outcome.trade.id
                note = f"📥 {label}: в журнале — сделка #{trade_id}."
                if balance is None:
                    warning = (
                        "⚠️ Баланс счёта не получен — сделка не войдёт в процент "
                        "лимитов убытка."
                    )
                    note += f" {warning}"
            else:
                note = f"📥 {label}: {outcome.refusal} Историю целиком — /import."
    except ExchangeError as exc:
        logger.warning("Позиция в журнал не занесена", extra={"user_id": user.id})
        note = _describe(exc)
    finally:
        await client.close()
    return Imported(note, trade_id, warning)


@router.callback_query(F.data.startswith(PositionsCB.IMPORT))
async def import_position(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    redis: Any,
) -> None:
    """В журнал — только текущая открытая позиция (исполнения её входа за
    IMPORT_DAYS дней), без закрытых сделок символа: история — /import.
    Сделку создаёт пользователь кнопкой; reconciler сам их не создаёт
    (позиция без сделки — только уведомление с этой же кнопкой)."""
    ref = _parse_ref(str(callback.data), PositionsCB.IMPORT)
    if ref is None:
        await callback.answer("Кнопка устарела — открой «Позиции» заново.", show_alert=True)
        return
    result = await import_open_position(session, user, settings, cipher, redis, *ref)
    if result.refused_early:
        await callback.answer(result.note, show_alert=True)
        return
    await _answer_late(callback)
    await _show(callback, session, user, settings, cipher, note=result.note)


async def _answer_late(
    callback: CallbackQuery, text: str | None = None, *, alert: bool = False
) -> bool:
    """Ответ на нажатие после запросов к бирже: нажатие могло устареть
    (Telegram отвечает ошибкой), всплывашка — не длиннее ALERT_LIMIT.
    False — не ответили, итог нужно показать иначе."""
    if text is not None and len(text) > ALERT_LIMIT:
        text = None
    try:
        await callback.answer(text, show_alert=alert and text is not None)
    except TelegramAPIError:
        return False
    return text is not None or not alert


@router.callback_query(F.data.startswith(IMPORT_ORPHAN))
async def import_orphan_position(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
    redis: Any,
) -> None:
    """п.3 (24.10): «📥 В журнал» в сообщении сверки «сделки в журнале нет».
    Итог дописывается к тексту сообщения: удалось — кнопка импорта убрана,
    «💼 Позиции» остаётся; отказ — причина, кнопки как были (прежняя причина
    заменяется). Не отредактировалось — итог новым сообщением."""
    ref = _parse_ref(str(callback.data), IMPORT_ORPHAN)
    if ref is None:
        await callback.answer("Кнопка устарела — открой «Позиции».", show_alert=True)
        return
    result = await import_open_position(session, user, settings, cipher, redis, *ref)
    await _answer_late(callback)
    if result.trade_id is None:
        line = f"{ORPHAN_REFUSED}{html.escape(result.note.removeprefix('📥 '))}"
        keyboard = orphan_keyboard(*ref)
    else:
        line = f"📥 В журнале — сделка #{result.trade_id}."
        if result.warning:
            line += f" {result.warning}"
        keyboard = orphan_done_keyboard()
    message = callback.message
    if isinstance(message, Message):
        original = message.html_text.split(f"\n\n{ORPHAN_REFUSED}", 1)[0]
        try:
            await message.edit_text(f"{original}\n\n{line}", reply_markup=keyboard)
            return
        except TelegramAPIError:
            logger.warning("Сообщение сверки не отредактировано — итог новым сообщением",
                           extra={"user_id": user.id, "trade_id": result.trade_id})
    await callback.bot.send_message(  # type: ignore[union-attr]
        callback.from_user.id, f"{ref[0]} {ref[1].value}: {line}", reply_markup=keyboard,
    )
