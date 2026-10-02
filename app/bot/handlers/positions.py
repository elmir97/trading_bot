"""Экран «💼 Позиции» (этап 3): позиции с биржи и связь с журналом.

Позиции — с биржи, счёт из настроек (user.settings.active_exchange_mode),
как у остальных экранов биржи: get_positions + get_open_orders без символа —
два запроса. Стоп и тейк — из openOrders (app/execution/position_view.py).
Позиция без сделки в журнале — кнопка «📥 В журнал»: импорт исполнений этого
инструмента за IMPORT_DAYS дней (обычный импорт, отфильтрованный по
символу). Ниже — открытые сделки только из журнала (без позиции на бирже):
их закрывают как раньше, вводом цены выхода (app/bot/handlers/trades.py).

Только чтение биржи: ордеров экран не отправляет. Действия с позицией —
этап 4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.exchange import ExchangeCB, _describe, _market_cache
from app.bot.keyboards.main import MenuCallback
from app.bot.keyboards.trade import TradeCB
from app.bot.messaging import edit_or_replace
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.database.models.trade import Trade
from app.database.models.user import User
from app.database.repositories.trade import TradeRepository
from app.exchanges.base import ExchangeAuthError, ExchangeError
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

router = Router(name="positions")
logger = get_logger(__name__)

IMPORT_DAYS = 30


class PositionsCB:
    REFRESH = "pos:refresh"
    IMPORT = "pos:imp:"   # + SYMBOL


def positions_keyboard(views: list[PositionView], journal: list[Trade]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for view in views:
        if view.trade is None:
            p = view.position
            builder.button(
                text=f"📥 В журнал: {p.symbol.replace('-USDT', '')} {p.side.value}",
                callback_data=f"{PositionsCB.IMPORT}{p.symbol}",
            )
    for trade in journal:
        builder.button(
            text=f"{trade.symbol} {trade.side.value} #{trade.id}",
            callback_data=f"{TradeCB.CLOSE}{trade.id}",
        )
    builder.button(text="🔄 Обновить", callback_data=PositionsCB.REFRESH)
    builder.button(text="◀️ В меню", callback_data=MenuCallback.MAIN)
    builder.adjust(1)
    return builder.as_markup()


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
    views = build_views(positions, orders, open_trades)
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
    await _show(event, session, user, settings, cipher)


@router.callback_query(F.data.startswith(PositionsCB.IMPORT))
async def import_position(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    settings: Settings,
    cipher: SecretCipher,
) -> None:
    """Импорт исполнений инструмента за IMPORT_DAYS дней — сделка журнала для
    позиции. Сделку создаёт пользователь кнопкой; reconciler сам их не
    создаёт (позиция без сделки — только уведомление)."""
    symbol = str(callback.data).removeprefix(PositionsCB.IMPORT)
    await callback.answer("Импортирую…")
    try:
        client = await ExchangeFactory(settings, cipher).for_user(
            session, user.id, mode=user.settings.active_exchange_mode
        )
    except ExchangeAuthError as exc:
        await _show(callback, session, user, settings, cipher, note=_describe(exc))
        return
    end = datetime.now(UTC)
    try:
        result = await HistoryImporter(client, TradeRepository(session), user.id).import_period(
            end - timedelta(days=IMPORT_DAYS), end, symbol=symbol
        )
        note = f"📥 {symbol}: " + (
            f"сделок создано — {result.trades_created}"
            if not result.errors
            else result.render()
        )
        if not result.errors and result.trades_created == 0:
            note += f" (за {IMPORT_DAYS} дней исполнений не найдено — попробуй «Импорт истории»)"
    except ExchangeError as exc:
        logger.warning("Импорт по символу не удался", extra={"user_id": user.id})
        note = _describe(exc)
    finally:
        await client.close()
    await _show(callback, session, user, settings, cipher, note=note)
